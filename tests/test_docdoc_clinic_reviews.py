from pathlib import Path

import pytest

from app.parsers.docdoc.clinic import parse_clinic_page
from app.parsers.docdoc.clinic_reviews_api import (
    api_context_from_clinic_html,
    build_clinic_reviews_more_url,
    paginate_clinic_reviews_via_api,
)
from app.parsers.docdoc.clinic_reviews_fetch import parse_ssr_clinic_reviews
from app.parsers.docdoc.htmlutil import normalize_saved_html
from app.services.docdoc_ingest import iter_reviews_deduped

ROOT = Path(__file__).resolve().parent.parent
CLINIC_URL = "https://irk.docdoc.ru/clinic/stomatologicheskaya_klinika_stoma_Dental"


@pytest.fixture
def example_clinic_html():
    return (ROOT / "example.html").read_text(encoding="utf-8", errors="replace")


def test_api_context_from_example_html(example_clinic_html):
    html = normalize_saved_html(example_clinic_html)
    ctx = api_context_from_clinic_html(html, CLINIC_URL)
    assert ctx is not None
    assert ctx.clinic_id == 116466
    assert ctx.reviews_count_total == 9
    assert ctx.api_base_url == "https://irk.docdoc.ru"
    url = build_clinic_reviews_more_url(ctx, offset=3)
    assert "/clinics/moreReviews?" in url
    assert "clinicId=116466" in url
    assert "offset=3" in url


def test_parse_ssr_reviews_from_example(example_clinic_html):
    html = normalize_saved_html(example_clinic_html)
    reviews = parse_ssr_clinic_reviews(html, CLINIC_URL)
    assert len(reviews) == 3
    assert all(r.get("text") for r in reviews)
    assert reviews[0].get("clinic_alias") == "stomatologicheskaya_klinika_stoma_Dental"
    assert reviews[0].get("rating_value") is not None


def test_parse_clinic_page_includes_ssr_reviews(example_clinic_html):
    html = normalize_saved_html(example_clinic_html)
    out = parse_clinic_page(html, CLINIC_URL)
    assert out["ok"] is True
    assert out["clinic_alias"] == "stomatologicheskaya_klinika_stoma_Dental"
    assert len(out["reviews"]) == 3
    assert out["reviews_count_total"] == 9
    assert out["reviews_incomplete"] is True


def test_paginate_clinic_reviews_via_api_mock(example_clinic_html):
    html = normalize_saved_html(example_clinic_html)
    initial = parse_ssr_clinic_reviews(html, CLINIC_URL)
    page_payload = {
        "total_count": 9,
        "reviews": [
            {
                "id": 900001,
                "text": "extra clinic review",
                "created": "2025-05-01 10:00:00",
                "ratingClinic": 9,
                "publicName": "P",
                "clinic": {
                    "name": "Stoma",
                    "alias": "stomatologicheskaya_klinika_stoma_Dental",
                    "fullAddress": {"city": "Иркутск"},
                },
                "rating": {"label": "ok", "value": 9},
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
                return {"ok": True, "data": page_payload}
            return {"ok": True, "data": {"total_count": 9, "reviews": []}}

    out = paginate_clinic_reviews_via_api(
        FakePage(),
        html,
        CLINIC_URL,
        initial_normalized=initial,
    )
    assert len(out) >= 4
    assert any(r.get("review_id") == 900001 for r in out)


def test_iter_reviews_includes_clinics_with_synthetic_ids():
    crawl = {
        "clinics": [
            {
                "ok": True,
                "reviews": [
                    {
                        "review_id": None,
                        "text": "hello",
                        "created": "2025-01-01",
                        "clinic_alias": "x",
                    }
                ],
            }
        ],
        "services": [],
    }
    out = list(iter_reviews_deduped(crawl))
    assert len(out) == 1
    assert out[0]["review_id"] is not None
