"""Полная выгрузка отзывов клиники: SSR + `/clinics/moreReviews` + «Показать ещё»."""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from app.parsers.docdoc.clinic_reviews_api import (
    ClinicReviewMoreResponseCollector,
    clinic_meta_from_html,
    clinic_reviews_page_url,
    normalize_clinic_api_reviews,
    paginate_clinic_reviews_via_api,
)
from app.parsers.docdoc.htmlutil import html_to_plain, normalize_saved_html
from app.parsers.docdoc.playwright_page import goto_and_settle, safe_page_content
from app.parsers.docdoc.reviews_fetch import expand_reviews_in_browser, merge_reviews_by_id

log = logging.getLogger(__name__)

_REVIEW_ITEM_RE = re.compile(
    r'<div class="reviews__item[^"]*"[^>]*itemtype="http://schema.org/Review"[^>]*>(.*?)</div>\s*</div>\s*(?='
    r'<div class="reviews__item|<div slot=|<adaptive-reviews|<button|</div>\s*</div>\s*</div>)',
    re.DOTALL | re.IGNORECASE,
)


def _clean_doctor_href(raw: str | None) -> str | None:
    if not raw:
        return None
    if "html-attribute-value" in raw:
        m = re.search(r'href="([^"]+)"', raw)
        if m:
            raw = m.group(1)
    raw = raw.strip()
    if raw.startswith("/"):
        return raw
    if raw.startswith("http"):
        from urllib.parse import urlparse

        return urlparse(raw).path or raw
    return raw


def _doctor_name_from_block(block: str) -> tuple[str | None, str | None]:
    m = re.search(
        r'class="review_doctor".*?<a[^>]*href="([^"]*)"[^>]*>([^<]+)',
        block,
        re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return None, None
    href = _clean_doctor_href(m.group(1))
    name = html_to_plain(m.group(2))
    return name or None, href


def _answer_from_block(block: str) -> str:
    m = re.search(
        r'class="reviews__answer"[^>]*>.*?class="reviews-text__container">\s*(.*?)\s*</div>',
        block,
        re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return ""
    return html_to_plain(m.group(1))


def parse_ssr_clinic_reviews(
    html: str,
    clinic_url: str,
    meta: tuple[int | None, str, str | None, str | None] | None = None,
) -> list[dict[str, Any]]:
    """Разбор SSR-отзывов (schema.org/Review) со вкладки #reviews."""
    html = normalize_saved_html(html)
    if meta is None:
        meta = clinic_meta_from_html(html, clinic_url)
    _cid, clinic_name, clinic_alias, _target_city = meta
    source_url = clinic_reviews_page_url(clinic_url)
    out: list[dict[str, Any]] = []
    for block in _REVIEW_ITEM_RE.findall(html):
        rating_m = re.search(r'itemprop="ratingValue"\s+content="([^"]+)"', block)
        body_m = re.search(
            r'itemprop="reviewBody"[^>]*>.*?class="reviews-text__container">\s*(.*?)\s*</div>',
            block,
            re.DOTALL,
        )
        author_m = re.search(r'itemprop="author"[^>]*>([^<]+)', block)
        date_m = re.search(r'itemprop="datePublished"\s+content="([^"]+)"', block)
        label_m = re.search(r'class="reviews__grade">\s*([^<]+)', block)
        text = html_to_plain(body_m.group(1)) if body_m else ""
        if not text.strip():
            continue
        rating_val = None
        if rating_m:
            try:
                rating_val = float(rating_m.group(1))
            except ValueError:
                rating_val = None
        doctor_name, _doctor_href = _doctor_name_from_block(block)
        out.append(
            {
                "review_id": None,
                "created": date_m.group(1).strip() if date_m else None,
                "text": text,
                "answer": _answer_from_block(block),
                "rating_clinic": rating_val,
                "rating_label": html_to_plain(label_m.group(1)) if label_m else None,
                "rating_value": rating_val,
                "patient_public_name": html_to_plain(author_m.group(1)) if author_m else None,
                "clinic_name": clinic_name,
                "clinic_alias": clinic_alias,
                "clinic_city": None,
                "doctor_id": None,
                "doctor_name": doctor_name,
                "service_id": None,
                "service_name": "",
                "parent_service_name": "",
                "category_direction_title": None,
                "source_page_url": source_url,
            }
        )
    return out


def _normalize_intercepted_clinic_chunks(
    collector: ClinicReviewMoreResponseCollector,
    html: str,
    clinic_url: str,
) -> list[dict[str, Any]]:
    if not collector.raw_chunks:
        return []
    cid, clinic_name, clinic_alias, target_city = clinic_meta_from_html(html, clinic_url)
    out: list[dict[str, Any]] = []
    for raw in collector.raw_chunks:
        batch, _ = normalize_clinic_api_reviews(
            raw,
            clinic_url=clinic_url,
            clinic_id=cid,
            clinic_name=clinic_name,
            clinic_alias=clinic_alias,
            target_city=target_city,
        )
        out.extend(batch)
    return out


def _reviews_api_enabled() -> bool:
    return os.getenv("DOCDOC_CLINIC_REVIEWS_USE_API", "true").lower() in ("1", "true", "yes")


def collect_clinic_reviews_with_page(
    page: Any,
    clinic_url: str,
    *,
    max_more_clicks: int = 30,
) -> list[dict[str, Any]]:
    """
    Полный сбор отзывов клиники: SSR + JSON API + клики «ещё».
    page будет открыт на clinic_url#reviews.
    """
    chunks: list[list[dict[str, Any]]] = []
    collector = ClinicReviewMoreResponseCollector()
    collector.attach(page)
    use_api = _reviews_api_enabled()

    rev_url = clinic_reviews_page_url(clinic_url)
    goto_and_settle(page, rev_url)
    html = safe_page_content(page)
    meta = clinic_meta_from_html(html, clinic_url)
    ssr = parse_ssr_clinic_reviews(html, clinic_url, meta)
    chunks.append(ssr)

    if use_api:
        api_merged = paginate_clinic_reviews_via_api(
            page,
            html,
            clinic_url,
            initial_normalized=ssr,
        )
        if len(api_merged) > len(ssr):
            chunks.append(api_merged)

    collector.reset()
    expand_reviews_in_browser(page, rev_url, max_clicks=max_more_clicks)
    html2 = safe_page_content(page)
    chunks.append(parse_ssr_clinic_reviews(html2, clinic_url, meta))
    chunks.append(_normalize_intercepted_clinic_chunks(collector, html2, clinic_url))

    return merge_reviews_by_id(chunks)


def attach_reviews_to_clinic_parsed(
    parsed: dict[str, Any],
    all_reviews: list[dict[str, Any]],
) -> dict[str, Any]:
    out = dict(parsed)
    out["reviews"] = all_reviews
    out["reviews_loaded_count"] = len(all_reviews)
    agg = out.get("reviews_aggregate") or {}
    total = None
    if isinstance(agg, dict):
        total = agg.get("recommendCount")
    if total is None:
        clinic = out.get("clinic") or {}
        # иногда total есть только в adaptive-reviews-container — не всегда в parsed
        pass
    if total is not None:
        try:
            if len(all_reviews) < int(total):
                out["reviews_incomplete"] = True
            else:
                out["reviews_incomplete"] = False
        except (TypeError, ValueError):
            out["reviews_incomplete"] = False
    else:
        out["reviews_incomplete"] = False
    return out
