"""Точка входа: определение типа страницы DocDoc (региональный поддомен)."""

from __future__ import annotations

from urllib.parse import urlparse

from app.parsers.docdoc.clinic import parse_clinic_page
from app.parsers.docdoc.doctors import parse_doctor_page
from app.parsers.docdoc.fetch import fetch_html
from app.parsers.docdoc.htmlutil import normalize_saved_html
from app.parsers.docdoc.main_page import extract_service_urls_from_main, infer_city_slug_from_host
from app.parsers.docdoc.service import parse_service_page


def parse_docdoc_html(
    raw_html: str,
    page_url: str,
    *,
    unwrap_view_source: bool = True,
) -> dict:
    """
    Парсинг HTML СберЗдоровье / DocDoc (например irk.docdoc.ru).

    raw_html: ответ «Сохранить как» или view-source (табличная вёрстка).
    page_url: исходный URL страницы (нужен для метаданных и путей).

    Возвращает dict с page_kind: main | service | clinic | unknown.
    """
    html = normalize_saved_html(raw_html) if unwrap_view_source else raw_html
    path = (urlparse(page_url).path or "/").rstrip("/") or "/"

    if path == "/" or path == "":
        urls = extract_service_urls_from_main(html, base_url=page_url)
        return {
            "page_kind": "main",
            "ok": True,
            "page_url": page_url,
            "city_slug": infer_city_slug_from_host(page_url),
            "service_urls": urls,
            "service_url_count": len(urls),
        }

    if path.startswith("/service/"):
        return parse_service_page(html, page_url)

    if path.startswith("/clinic/"):
        return parse_clinic_page(html, page_url)

    if path.startswith("/doctor/"):
        base = f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}/"
        return parse_doctor_page(html, page_url, base)

    return {
        "page_kind": "unknown",
        "ok": False,
        "page_url": page_url,
        "path": path,
    }


def parse_docdoc_url(page_url: str, **fetch_kwargs) -> dict:
    """Скачать страницу в сеть (Playwright) и распарсить."""
    html = fetch_html(page_url, **fetch_kwargs)
    return parse_docdoc_html(html, page_url)


def parse_docdoc_url_full_reviews(service_page_url: str, **fetch_kwargs) -> dict:
    """Страница услуги: метаданные + максимум отзывов через браузер."""
    from app.parsers.docdoc.fetch import fetch_html, fetch_service_reviews_full
    from app.parsers.docdoc.reviews_fetch import attach_reviews_to_service_parsed

    reviews_map = fetch_service_reviews_full([service_page_url], **fetch_kwargs)
    html = fetch_html(service_page_url, **fetch_kwargs)
    parsed = parse_docdoc_html(html, service_page_url)
    if parsed.get("ok"):
        parsed = attach_reviews_to_service_parsed(parsed, reviews_map.get(service_page_url, []))
    return parsed
