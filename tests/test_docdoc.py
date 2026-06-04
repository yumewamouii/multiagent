from pathlib import Path

import pytest

from unittest.mock import patch

from app.parsers.docdoc import parse_docdoc_html
from app.parsers.docdoc.crawl import crawl_docdoc
from app.parsers.docdoc.fetch import normalize_base_url
from app.parsers.docdoc.reviews_fetch import collect_service_reviews_from_html

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def main_html():
    return (ROOT / "main_page.html").read_text(encoding="utf-8", errors="replace")


@pytest.fixture
def service_html():
    return (ROOT / "service.html").read_text(encoding="utf-8", errors="replace")


@pytest.fixture
def clinic_html():
    return (ROOT / "clinic.html").read_text(encoding="utf-8", errors="replace")


def test_category_hub_paths():
    from app.parsers.docdoc.main_page import extract_category_hub_paths

    html = (
        '<a href="/service/stomatologiya">'
        '<a href="/service/stomatologiya/implant">'
        '<a href="/service/urologiya">'
    )
    hubs = extract_category_hub_paths(html)
    assert "/service/stomatologiya" in hubs
    assert "/service/urologiya" in hubs
    assert "/service/stomatologiya/implant" not in hubs


def test_main_collects_service_urls(main_html):
    out = parse_docdoc_html(main_html, "https://irk.docdoc.ru/")
    assert out["page_kind"] == "main"
    assert out["service_url_count"] > 50
    assert any("/service/narkologiya/" in u for u in out["service_urls"])


def test_service_reviews_and_doctors(service_html):
    out = parse_docdoc_html(service_html, "https://irk.docdoc.ru/service/narkologiya/vyvedenie-iz-zapoya-na-domu")
    assert out["ok"] is True
    assert out["page_kind"] == "service"
    assert out["service"]["parent_service_name"]
    assert isinstance(out["reviews"], list)
    assert len(out["reviews"]) >= 1
    assert isinstance(out.get("doctors"), list)
    assert len(out["doctors"]) >= 1
    assert out["doctors"][0].get("doctor_id")
    assert out["doctors"][0].get("name")
    sample = out["reviews"][0]
    assert sample.get("parent_service_name")
    assert sample.get("service_name")
    assert sample.get("source_page_url", "").startswith("https://")


def test_clinic_catalog(clinic_html):
    out = parse_docdoc_html(clinic_html, "https://irk.docdoc.ru/clinic/clean_clinic_1")
    assert out["page_kind"] == "clinic"
    assert out["clinic_alias"] == "clean_clinic_1"
    assert len(out["service_catalog"]) >= 1
    row = out["service_catalog"][0]
    assert row.get("direction_name")
    assert row.get("service_id")
    assert row.get("service_name")


def test_normalize_base_url():
    assert normalize_base_url("https://irk.docdoc.ru") == "https://irk.docdoc.ru/"


def test_crawl_docdoc_offline(main_html, service_html):
    def fake_fetch(url, **kwargs):
        if url.rstrip("/") in ("https://irk.docdoc.ru", "https://irk.docdoc.ru/"):
            return main_html
        if "/service/" in url:
            return service_html
        raise AssertionError(f"unexpected url {url}")

    def fake_batch(urls, **kwargs):
        return {u: fake_fetch(u) for u in urls}

    def fake_dual(service_urls, **kwargs):
        from app.parsers.docdoc.reviews_fetch import service_reviews_url

        out: dict[str, tuple[str, str]] = {}
        for su in service_urls:
            out[su] = (fake_fetch(su), service_html)
        return out

    with patch("app.parsers.docdoc.crawl.fetch_html", side_effect=fake_fetch):
        with patch("app.parsers.docdoc.crawl.fetch_html_batch", side_effect=fake_batch):
            with patch("app.parsers.docdoc.crawl.fetch_service_pages_dual", side_effect=fake_dual):
                out = crawl_docdoc(
                    "https://irk.docdoc.ru/",
                    max_services=1,
                    max_clinics=0,
                    fetch_clinics=False,
                    full_reviews=False,
                    dual_review_pages=True,
                    discover_category_hubs=False,
                )

    assert out["ok"] is True
    assert out["stats"]["services_fetched"] == 1
    merged = collect_service_reviews_from_html(service_html, "https://irk.docdoc.ru/service/narkologiya/vyvedenie-iz-zapoya-na-domu", service_html)
    assert len(merged) >= 1
