"""Врачи: bestDoctors на странице услуги и профиль /doctor/...."""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin, urlparse

from app.parsers.docdoc.htmlutil import extract_next_data, html_to_plain


def normalize_best_doctors(
    raw: list[Any] | None,
    *,
    base_url: str,
    service_id: int | None,
    service_name: str,
    parent_service_name: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for d in raw:
        if not isinstance(d, dict):
            continue
        profile_path = d.get("url") or ""
        profile_url = urljoin(base_url, profile_path) if profile_path else ""
        out.append(
            {
                "doctor_id": d.get("id"),
                "name": d.get("name"),
                "profile_url": profile_url,
                "profile_path": profile_path,
                "image_url": d.get("image"),
                "price": d.get("price"),
                "special_price": d.get("specialPrice"),
                "special_price_percent": d.get("specialPricePercent"),
                "total_rating": d.get("totalRating"),
                "reviews_count": d.get("reviewsCount"),
                "station": d.get("station"),
                "address": d.get("address"),
                "service_id": service_id,
                "service_name": service_name,
                "parent_service_name": parent_service_name,
            }
        )
    return out


def _doctor_alias_from_url(page_url: str) -> str | None:
    path = (urlparse(page_url).path or "").strip("/")
    if path.startswith("doctor/"):
        return path.split("/", 1)[1].split("?")[0] or None
    return None


def parse_doctor_page(html: str, page_url: str, base_url: str) -> dict[str, Any]:
    nd = extract_next_data(html)
    if not nd:
        return {"page_kind": "doctor", "ok": False, "error": "no_next_data", "page_url": page_url}

    pp = nd.get("props", {}).get("pageProps", {})
    ps = pp.get("preloadedState")
    if not isinstance(ps, dict):
        return {"page_kind": "doctor", "ok": False, "error": "no_preloaded_state", "page_url": page_url}

    # разные версии стора
    dp = ps.get("doctorPage") or ps.get("doctor") or {}
    if not isinstance(dp, dict):
        dp = {}

    info = dp.get("doctorInfo") or dp.get("doctor") or dp
    if not isinstance(info, dict):
        info = {}

    reviews_raw = dp.get("reviews") or info.get("reviews") or []
    reviews: list[dict[str, Any]] = []
    if isinstance(reviews_raw, list):
        for r in reviews_raw:
            if not isinstance(r, dict):
                continue
            rating = r.get("rating") if isinstance(r.get("rating"), dict) else {}
            reviews.append(
                {
                    "review_id": r.get("id"),
                    "created": r.get("created"),
                    "text": (r.get("text") or "").strip(),
                    "answer": (r.get("answer") or "").strip(),
                    "rating_value": rating.get("value") if rating else r.get("rating"),
                    "rating_label": rating.get("label") if rating else None,
                    "patient_public_name": r.get("publicName"),
                    "clinic_name": (r.get("clinic") or {}).get("name") if isinstance(r.get("clinic"), dict) else None,
                    "service_name": (r.get("service") or {}).get("name") if isinstance(r.get("service"), dict) else None,
                    "doctor_id": info.get("id"),
                    "doctor_name": info.get("name") or info.get("fullName"),
                    "source_page_url": page_url,
                }
            )

    name = info.get("name") or info.get("fullName") or ""
    return {
        "page_kind": "doctor",
        "ok": True,
        "page_url": page_url,
        "doctor_alias": _doctor_alias_from_url(page_url),
        "doctor": {
            "id": info.get("id"),
            "name": name,
            "speciality": info.get("speciality") or info.get("specialityName"),
            "experience": info.get("experience"),
            "description_plain": html_to_plain(info.get("description") or info.get("about")),
            "image_url": info.get("image") or info.get("photo"),
            "total_rating": info.get("totalRating") or info.get("rating"),
            "reviews_count": info.get("reviewsCount") or dp.get("reviewsCount"),
            "clinic_name": info.get("clinicName"),
        },
        "reviews": reviews,
        "reviews_count_total": dp.get("reviewsCount") or len(reviews),
    }
