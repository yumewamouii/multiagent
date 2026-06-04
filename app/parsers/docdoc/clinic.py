"""Страница клиники: Vue-атрибуты (catalog) + SSR-отзывы на #reviews."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from app.parsers.docdoc.clinic_reviews_api import api_context_from_clinic_html, clinic_meta_from_html
from app.parsers.docdoc.clinic_reviews_fetch import parse_ssr_clinic_reviews
from app.parsers.docdoc.htmlutil import extract_next_data, normalize_saved_html, parse_vue_attr_json


def _clinic_alias_from_url(page_url: str) -> str | None:
    path = (urlparse(page_url).path or "").strip("/")
    if path.startswith("clinic/"):
        return path.split("/")[1] or None
    return None


def _flatten_med_services(med: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(med, dict):
        return []
    rows: list[dict[str, Any]] = []
    for did, block in med.items():
        if not isinstance(block, dict):
            continue
        direction_name = block.get("name")
        speciality_id = block.get("specialityId")
        title = block.get("title")
        prices = block.get("prices")
        if not isinstance(prices, list):
            continue
        for p in prices:
            if not isinstance(p, dict):
                continue
            rows.append(
                {
                    "direction_key": did,
                    "direction_name": direction_name,
                    "speciality_id": speciality_id,
                    "direction_page_title": title,
                    "service_id": p.get("id"),
                    "service_name": p.get("name"),
                    "price": p.get("price"),
                    "special_price": p.get("specialPrice"),
                    "final_price": p.get("finalPrice"),
                    "description": p.get("description"),
                }
            )
    return rows


def parse_clinic_page(html: str, page_url: str) -> dict[str, Any]:
    alias = _clinic_alias_from_url(page_url)

    nd = extract_next_data(html)
    next_ok = bool(nd)

    med = parse_vue_attr_json(html, "med-services", end_before_colon=True)
    clinics_mini = parse_vue_attr_json(html, "clinics", end_before_colon=False)
    reviews_tags = parse_vue_attr_json(html, "reviews-tags", end_before_colon=False)

    clinics_list = clinics_mini if isinstance(clinics_mini, list) else []
    first_clinic = clinics_list[0] if clinics_list and isinstance(clinics_list[0], dict) else {}

    catalog = _flatten_med_services(med if isinstance(med, dict) else None)

    html_norm = normalize_saved_html(html)
    meta = clinic_meta_from_html(html_norm, page_url)
    ssr_reviews = parse_ssr_clinic_reviews(html_norm, page_url, meta)
    api_ctx = api_context_from_clinic_html(html_norm, page_url)
    reviews_count_total = api_ctx.reviews_count_total if api_ctx else None
    if reviews_count_total is None:
        m = re.search(r':total-reviews-count="(\d+)"', html_norm)
        if m:
            try:
                reviews_count_total = int(m.group(1))
            except ValueError:
                reviews_count_total = None

    return {
        "page_kind": "clinic",
        "ok": True,
        "page_url": page_url,
        "city_slug": (urlparse(page_url).netloc or "").split(".")[0] or None,
        "clinic_alias": alias,
        "clinic": {
            "id": first_clinic.get("id"),
            "name": first_clinic.get("name"),
            "rating": first_clinic.get("rating"),
            "full_address": first_clinic.get("fullAddress"),
        },
        "clinics_cards": [
            {
                "id": c.get("id"),
                "name": c.get("name"),
                "rating": c.get("rating"),
                "full_address": c.get("fullAddress"),
            }
            for c in clinics_list
            if isinstance(c, dict)
        ],
        "service_catalog": catalog,
        "reviews_aggregate": reviews_tags if isinstance(reviews_tags, dict) else None,
        "reviews": ssr_reviews,
        "reviews_count_total": reviews_count_total,
        "reviews_loaded_count": len(ssr_reviews),
        "reviews_incomplete": (
            reviews_count_total is not None and len(ssr_reviews) < int(reviews_count_total)
        ),
        "has_next_data": next_ok,
        "note": (
            "SSR-отзывы на вкладке #reviews; полный набор — через Playwright "
            "(`/clinics/moreReviews` + «Показать ещё»)."
        ),
    }
