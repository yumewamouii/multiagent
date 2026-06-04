"""Полная выгрузка отзывов услуги: основная страница, /order/reviews, кнопка «Показать ещё»."""

from __future__ import annotations

import logging
import os
import re
from typing import Any
from urllib.parse import urlparse

from app.parsers.docdoc.htmlutil import extract_next_data, normalize_saved_html
from app.parsers.docdoc.playwright_page import goto_and_settle, safe_page_content
from app.parsers.docdoc.reviews_api import (
    ReviewMoreResponseCollector,
    normalize_api_reviews,
    paginate_service_reviews_via_api,
    service_meta_from_html,
)
from app.parsers.docdoc.service import parse_service_page

log = logging.getLogger(__name__)

REVIEWS_PATH_SUFFIX = "/order/reviews/direction/desc"


def service_reviews_url(service_page_url: str) -> str:
    path = (urlparse(service_page_url).path or "").rstrip("/")
    if path.endswith("/order/reviews/direction/desc"):
        return service_page_url
    return f"{service_page_url.rstrip('/')}{REVIEWS_PATH_SUFFIX}"


def _reviews_from_next(html: str, page_url: str) -> tuple[list[dict[str, Any]], int | None]:
    html = normalize_saved_html(html)
    nd = extract_next_data(html)
    if not nd:
        return [], None
    pp = nd.get("props", {}).get("pageProps", {})
    ps = pp.get("preloadedState")
    if not isinstance(ps, dict):
        return [], None
    sp = ps.get("servicePage")
    if not isinstance(sp, dict):
        return [], None
    parsed = parse_service_page(html, page_url)
    if not parsed.get("ok"):
        return [], sp.get("reviewsCount")
    return list(parsed.get("reviews") or []), sp.get("reviewsCount")


def merge_reviews_by_id(chunks: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    by_id: dict[Any, dict[str, Any]] = {}
    for part in chunks:
        for r in part:
            if not isinstance(r, dict):
                continue
            rid = r.get("review_id")
            key = rid if rid is not None else (r.get("text"), r.get("created"))
            by_id[key] = r
    return list(by_id.values())


def collect_service_reviews_from_html(
    main_html: str,
    service_url: str,
    reviews_html: str | None = None,
) -> list[dict[str, Any]]:
    """Слияние отзывов с основной страницы и страницы /order/reviews/."""
    chunks: list[list[dict[str, Any]]] = []
    main_list, _ = _reviews_from_next(main_html, service_url)
    chunks.append(main_list)
    if reviews_html:
        rev_list, _ = _reviews_from_next(reviews_html, service_reviews_url(service_url))
        chunks.append(rev_list)
    return merge_reviews_by_id(chunks)


def _reviews_api_enabled() -> bool:
    return os.getenv("DOCDOC_REVIEWS_USE_API", "true").lower() in ("1", "true", "yes")


def _normalize_intercepted_chunks(
    collector: ReviewMoreResponseCollector,
    html: str,
    service_url: str,
) -> list[dict[str, Any]]:
    if not collector.raw_chunks:
        return []
    sid, service_name, parent_name, direction, target_city, alias_map = service_meta_from_html(
        html, service_url
    )
    out: list[dict[str, Any]] = []
    for raw in collector.raw_chunks:
        batch, _ = normalize_api_reviews(
            raw,
            service_url=service_url,
            service_id=sid,
            service_name=service_name,
            parent_service_name=parent_name,
            category_direction=direction,
            target_city=target_city,
            alias_map=alias_map,
        )
        out.extend(batch)
    return out


def expand_reviews_in_browser(page: Any, service_url: str, max_clicks: int = 30) -> None:
    """
    На уже открытой Playwright-странице кликает «Показать ещё» в блоке отзывов.
    """
    selectors = [
        "button:has-text('Показать ещё')",
        "button:has-text('Показать еще')",
        "a:has-text('Показать ещё')",
        "a:has-text('Показать еще')",
        "[class*='reviews'] button:has-text('ещ')",
    ]
    for _ in range(max_clicks):
        clicked = False
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=1500):
                    loc.click()
                    page.wait_for_timeout(1500)
                    try:
                        page.wait_for_load_state("networkidle", timeout=3000)
                    except Exception:
                        pass
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            break


def collect_service_reviews_with_page(
    page: Any,
    service_url: str,
    *,
    main_html: str | None = None,
    max_more_clicks: int = 30,
) -> list[dict[str, Any]]:
    """
    Полный сбор: SSR + JSON API `/review/service/more` + клики «ещё» + /order/reviews.
    page уже на service_url или будет перенаправлен.
    """
    chunks: list[list[dict[str, Any]]] = []
    collector = ReviewMoreResponseCollector()
    collector.attach(page)
    use_api = _reviews_api_enabled()

    goto_and_settle(page, service_url)
    main_html = safe_page_content(page)
    main_list, _ = _reviews_from_next(main_html, service_url)
    chunks.append(main_list)

    if use_api:
        api_merged = paginate_service_reviews_via_api(
            page,
            main_html,
            service_url,
            initial_normalized=main_list,
        )
        if len(api_merged) > len(main_list):
            chunks.append(api_merged)

    collector.reset()
    expand_reviews_in_browser(page, service_url, max_clicks=max_more_clicks)
    post_click_html = safe_page_content(page)
    chunks.append(_reviews_from_next(post_click_html, service_url)[0])
    chunks.append(_normalize_intercepted_chunks(collector, post_click_html, service_url))

    rev_url = service_reviews_url(service_url)
    goto_and_settle(page, rev_url)
    rev_html = safe_page_content(page)
    rev_list, _ = _reviews_from_next(rev_html, rev_url)
    chunks.append(rev_list)

    if use_api:
        api_rev = paginate_service_reviews_via_api(
            page,
            rev_html,
            service_url,
            initial_normalized=rev_list,
        )
        if len(api_rev) > len(rev_list):
            chunks.append(api_rev)

    collector.reset()
    expand_reviews_in_browser(page, rev_url, max_clicks=max_more_clicks)
    rev_post_html = safe_page_content(page)
    chunks.append(_reviews_from_next(rev_post_html, rev_url)[0])
    chunks.append(_normalize_intercepted_chunks(collector, rev_post_html, service_url))

    return merge_reviews_by_id(chunks)


def attach_reviews_to_service_parsed(
    parsed: dict[str, Any],
    all_reviews: list[dict[str, Any]],
) -> dict[str, Any]:
    out = dict(parsed)
    out["reviews"] = all_reviews
    out["reviews_loaded_count"] = len(all_reviews)
    total = out.get("reviews_count_total")
    if total is not None and len(all_reviews) < int(total):
        out["reviews_incomplete"] = True
    else:
        out["reviews_incomplete"] = False
    return out
