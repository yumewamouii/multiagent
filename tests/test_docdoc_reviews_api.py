from pathlib import Path

import pytest

from app.parsers.docdoc.htmlutil import normalize_saved_html
from app.parsers.docdoc.reviews_api import (
    api_context_from_html,
    build_reviews_more_url,
    normalize_api_reviews,
    paginate_service_reviews_via_api,
    parse_reviews_more_payload,
)
from app.parsers.docdoc.service import parse_service_page

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def service_html():
    return (ROOT / "service.html").read_text(encoding="utf-8", errors="replace")


def test_api_context_from_service_html(service_html):
    ctx = api_context_from_html(service_html)
    assert ctx is not None
    assert ctx.service_id == 4878
    assert ctx.api_base_url.startswith("https://")
    assert ctx.reviews_count_total == 5


def test_build_reviews_more_url(service_html):
    ctx = api_context_from_html(service_html)
    assert ctx is not None
    url = build_reviews_more_url(ctx, offset=5)
    assert "/review/service/more?" in url
    assert "serviceId=4878" in url
    assert "offset=5" in url


def test_parse_reviews_more_payload_success():
    raw, total, err = parse_reviews_more_payload(
        {"total_count": 11, "reviews": [{"id": 1, "text": "a", "created": "2025-01-01"}]}
    )
    assert err is None
    assert total == 11
    assert len(raw) == 1


def test_parse_reviews_more_payload_error():
    raw, total, err = parse_reviews_more_payload({"success": False, "message": "fail"})
    assert raw == []
    assert total is None
    assert err == "fail"


def test_normalize_api_reviews_caps_page_size(service_html):
    html = normalize_saved_html(service_html)
    parsed = parse_service_page(html, "https://irk.docdoc.ru/service/narkologiya/vyvedenie-iz-zapoya-na-domu")
    sample = parsed["reviews"][0]
    raw_batch = [
        {
            "id": 9000 + i,
            "text": f"t{i}",
            "created": "2025-01-01",
            "ratingClinic": 10,
            "publicName": "X",
            "clinic": {"name": "C"},
            "rating": {"label": "ok", "value": 10},
            "doctor": {},
        }
        for i in range(8)
    ]
    out, dropped = normalize_api_reviews(
        raw_batch,
        service_url=sample["source_page_url"],
        service_id=sample["service_id"],
        service_name=sample["service_name"],
        parent_service_name=sample["parent_service_name"],
        category_direction=sample.get("category_direction_title"),
        page_size=5,
    )
    assert len(out) == 5
    assert dropped == 0


def test_paginate_service_reviews_via_api_mock_page(service_html):
    html = normalize_saved_html(service_html)
    parsed = parse_service_page(html, "https://irk.docdoc.ru/service/narkologiya/vyvedenie-iz-zapoya-na-domu")
    initial = list(parsed["reviews"][:2])
    page2 = {
        "total_count": 11,
        "reviews": [
            {
                "id": 800001,
                "text": "extra",
                "created": "2025-06-01",
                "ratingClinic": 10,
                "publicName": "P",
                "clinic": {
                    "name": "Cl",
                    "fullAddress": {"city": "Иркутск"},
                },
                "rating": {"label": "ok", "value": 10},
                "doctor": {},
            }
        ],
    }

    class FakePage:
        def __init__(self):
            self.calls = 0

        def evaluate(self, _js, _args):
            self.calls += 1
            if self.calls == 1:
                return {"ok": True, "data": page2}
            return {"ok": True, "data": {"total_count": 11, "reviews": []}}

    out = paginate_service_reviews_via_api(
        FakePage(),
        html,
        "https://irk.docdoc.ru/service/narkologiya/vyvedenie-iz-zapoya-na-domu",
        initial_normalized=initial,
    )
    assert len(out) >= 3
    assert any(r.get("review_id") == 800001 for r in out)


def test_paginate_drops_other_city_reviews(service_html):
    """irk.docdoc.ru подмешивает Москву → должно отфильтроваться."""
    html = normalize_saved_html(service_html)
    initial = []
    page1 = {
        "total_count": 4,
        "reviews": [
            {
                "id": 1,
                "text": "irk-1",
                "created": "2025-01-01",
                "rating": {"label": "ok", "value": 10},
                "clinic": {"name": "A", "fullAddress": {"city": "Иркутск"}},
                "doctor": {},
            },
            {
                "id": 2,
                "text": "msk-1",
                "created": "2025-01-02",
                "rating": {"label": "ok", "value": 10},
                "clinic": {"name": "B", "fullAddress": {"city": "Москва"}},
                "doctor": {},
            },
            {
                "id": 3,
                "text": "irk-2",
                "created": "2025-01-03",
                "rating": {"label": "ok", "value": 10},
                "clinic": {"name": "C", "fullAddress": {"city": "Иркутск"}},
                "doctor": {},
            },
            {
                "id": 4,
                "text": "msk-2",
                "created": "2025-01-04",
                "rating": {"label": "ok", "value": 10},
                "clinic": {"name": "D", "fullAddress": {"city": "Москва"}},
                "doctor": {},
            },
        ],
    }

    class FakePage:
        def __init__(self):
            self.calls = 0

        def evaluate(self, _js, _args):
            self.calls += 1
            if self.calls == 1:
                return {"ok": True, "data": page1}
            return {"ok": True, "data": {"total_count": 4, "reviews": []}}

    out = paginate_service_reviews_via_api(
        FakePage(),
        html,
        "https://irk.docdoc.ru/service/narkologiya/vyvedenie-iz-zapoya-na-domu",
        initial_normalized=initial,
    )
    cities = {r.get("clinic_city") for r in out}
    assert cities == {"Иркутск"}
    assert {r["review_id"] for r in out} == {1, 3}
