"""Пагинация отзывов клиники через XHR `/clinics/moreReviews` (Vue-страница clinic)."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse

from app.parsers.docdoc.htmlutil import normalize_saved_html, parse_vue_attr_json
from app.parsers.docdoc.reviews_api import (
    _max_pages,
    _page_size,
    parse_reviews_more_payload,
)
from app.parsers.docdoc.service import (
    _CITY_SLUG_TO_NAME,
    _normalize_review,
    review_belongs_to_city,
)

log = logging.getLogger(__name__)

CLINIC_REVIEWS_MORE_PATH = "/clinics/moreReviews"


@dataclass(frozen=True)
class ClinicReviewsApiContext:
    api_base_url: str
    clinic_id: int
    reviews_path: str = CLINIC_REVIEWS_MORE_PATH
    reviews_offset: int = 0
    reviews_sort: int = 0
    reviews_filter: int = 0
    reviews_count_total: int | None = None


def _site_origin(page_url: str) -> str:
    parsed = urlparse(page_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _target_city_from_clinic_html(html: str, page_url: str) -> str | None:
    slug = (urlparse(page_url).netloc or "").split(".")[0]
    mapped = _CITY_SLUG_TO_NAME.get(slug or "")
    if mapped:
        return mapped
    clinics = parse_vue_attr_json(html, "clinics", end_before_colon=False)
    if isinstance(clinics, list) and clinics:
        addr = (clinics[0] or {}).get("fullAddress") or ""
        if isinstance(addr, str) and "Иркутск" in addr:
            return "Иркутск"
        if isinstance(addr, str) and "Москва" in addr:
            return "Москва"
    m = re.search(r'window\.frontRefs\s*=\s*\{[^}]*"city"\s*:\s*"([a-z]+)"', html)
    if m:
        return _CITY_SLUG_TO_NAME.get(m.group(1))
    return None


def _clinic_id_from_html(html: str) -> int | None:
    m = re.search(r'name="clinicId"[^>]*value="(\d+)"', html, re.I)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    m2 = re.search(
        r'<adaptive-reviews-container\b[^>]*:id="(\d+)"',
        html,
        re.I | re.DOTALL,
    )
    if m2:
        try:
            return int(m2.group(1))
        except ValueError:
            pass
    clinics = parse_vue_attr_json(html, "clinics", end_before_colon=False)
    if isinstance(clinics, list) and clinics:
        raw = (clinics[0] or {}).get("id")
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None
    return None


def api_context_from_clinic_html(html: str, page_url: str) -> ClinicReviewsApiContext | None:
    html = normalize_saved_html(html)
    cid = _clinic_id_from_html(html)
    if cid is None:
        return None
    total: int | None = None
    m_total = re.search(
        r'<adaptive-reviews-container\b[^>]*:total-count="(\d+)"',
        html,
        re.I | re.DOTALL,
    )
    if m_total:
        try:
            total = int(m_total.group(1))
        except ValueError:
            total = None
    if total is None:
        m2 = re.search(r':total-reviews-count="(\d+)"', html)
        if m2:
            try:
                total = int(m2.group(1))
            except ValueError:
                total = None
    ssr_count = len(re.findall(r'itemtype="http://schema.org/Review"', html))
    return ClinicReviewsApiContext(
        api_base_url=_site_origin(page_url).rstrip("/"),
        clinic_id=cid,
        reviews_offset=max(0, ssr_count),
        reviews_count_total=total,
    )


def clinic_meta_from_html(
    html: str, page_url: str
) -> tuple[int | None, str, str | None, str | None]:
    """(clinic_id, clinic_name, clinic_alias, target_city)."""
    html = normalize_saved_html(html)
    cid = _clinic_id_from_html(html)
    alias = None
    path = (urlparse(page_url).path or "").strip("/")
    if path.startswith("clinic/"):
        alias = path.split("/")[1] or None
    name = ""
    clinics = parse_vue_attr_json(html, "clinics", end_before_colon=False)
    if isinstance(clinics, list) and clinics and isinstance(clinics[0], dict):
        name = str(clinics[0].get("name") or "")
        if not alias:
            alias = clinics[0].get("alias")
    target_city = _target_city_from_clinic_html(html, page_url)
    return cid, name, alias if isinstance(alias, str) else None, target_city


def build_clinic_reviews_more_url(
    ctx: ClinicReviewsApiContext,
    *,
    offset: int | None = None,
) -> str:
    off = ctx.reviews_offset if offset is None else offset
    q = urlencode(
        {
            "clinicId": ctx.clinic_id,
            "offset": off,
            "filter": ctx.reviews_filter,
            "sort": ctx.reviews_sort,
        }
    )
    path = ctx.reviews_path if ctx.reviews_path.startswith("/") else f"/{ctx.reviews_path}"
    return f"{ctx.api_base_url}{path}?{q}"


def normalize_clinic_api_reviews(
    raw_reviews: list[dict[str, Any]],
    *,
    clinic_url: str,
    clinic_id: int | None,
    clinic_name: str,
    clinic_alias: str | None,
    target_city: str | None = None,
    page_size: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    limit = page_size if page_size is not None else _page_size()
    out: list[dict[str, Any]] = []
    dropped = 0
    reviews_url = clinic_reviews_page_url(clinic_url)
    for r in raw_reviews[:limit]:
        if target_city and not review_belongs_to_city(r, target_city):
            dropped += 1
            continue
        norm = _normalize_review(
            r,
            service_id=None,
            service_name="",
            parent_service_name="",
            service_url=reviews_url,
            category_direction=None,
        )
        if clinic_name and not norm.get("clinic_name"):
            norm["clinic_name"] = clinic_name
        if clinic_alias and not norm.get("clinic_alias"):
            norm["clinic_alias"] = clinic_alias
        if clinic_id is not None and norm.get("service_id") is None:
            pass
        out.append(norm)
    return out, dropped


def clinic_reviews_page_url(clinic_page_url: str) -> str:
    base = clinic_page_url.split("#")[0].rstrip("/")
    return f"{base}#reviews"


_FETCH_MORE_JS = """
async ({ url }) => {
  const r = await fetch(url, {
    credentials: "include",
    redirect: "manual",
    headers: { Accept: "application/json, text/plain, */*" },
  });
  const ct = r.headers.get("content-type") || "";
  if (!ct.includes("json")) {
    return { ok: false, status: r.status, contentType: ct };
  }
  return { ok: true, status: r.status, data: await r.json() };
}
"""


def fetch_clinic_reviews_more_page(
    page: Any,
    ctx: ClinicReviewsApiContext,
    *,
    offset: int,
) -> tuple[list[dict[str, Any]], int | None, str | None]:
    url = build_clinic_reviews_more_url(ctx, offset=offset)
    try:
        result = page.evaluate(_FETCH_MORE_JS, {"url": url})
    except Exception as exc:
        return [], None, str(exc)
    if not isinstance(result, dict) or not result.get("ok"):
        return [], None, "fetch_not_json"
    data = result.get("data")
    return parse_reviews_more_payload(data)


class ClinicReviewMoreResponseCollector:
    """Перехват `/clinics/moreReviews` при кликах «Показать ещё»."""

    def __init__(self) -> None:
        self._raw_chunks: list[list[dict[str, Any]]] = []

    @property
    def raw_chunks(self) -> list[list[dict[str, Any]]]:
        return self._raw_chunks

    def attach(self, page: Any) -> None:
        def _on_response(resp: Any) -> None:
            if "/clinics/moreReviews" not in (resp.url or ""):
                return
            try:
                if resp.status != 200:
                    return
                ct = (resp.headers.get("content-type") or "").lower()
                if "json" not in ct:
                    return
                raw, _, err = parse_reviews_more_payload(resp.json())
                if err or not raw:
                    return
                self._raw_chunks.append(raw)
            except Exception:
                return

        page.on("response", _on_response)

    def reset(self) -> None:
        self._raw_chunks.clear()


def paginate_clinic_reviews_via_api(
    page: Any,
    html: str,
    clinic_url: str,
    *,
    initial_normalized: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    ctx = api_context_from_clinic_html(html, clinic_url)
    if ctx is None:
        return list(initial_normalized or [])
    cid, clinic_name, clinic_alias, target_city = clinic_meta_from_html(html, clinic_url)
    page_size = _page_size()
    merged: dict[Any, dict[str, Any]] = {}
    for r in initial_normalized or []:
        rid = r.get("review_id")
        merged[rid if rid is not None else (r.get("text"), r.get("created"))] = r

    total = ctx.reviews_count_total
    offset = ctx.reviews_offset
    if total is not None and offset >= total:
        return list(merged.values())

    pages = 0
    consecutive_empty = 0
    while pages < _max_pages():
        raw, api_total, err = fetch_clinic_reviews_more_page(page, ctx, offset=offset)
        if err:
            log.debug("clinic reviews API offset=%s: %s", offset, err)
            break
        if api_total is not None:
            total = api_total
        if not raw:
            break
        batch, dropped = normalize_clinic_api_reviews(
            raw,
            clinic_url=clinic_url,
            clinic_id=cid or ctx.clinic_id,
            clinic_name=clinic_name,
            clinic_alias=clinic_alias,
            target_city=target_city,
            page_size=page_size,
        )
        for r in batch:
            rid = r.get("review_id")
            merged[rid if rid is not None else (r.get("text"), r.get("created"))] = r
        offset += len(raw[:page_size])
        pages += 1
        if not batch and dropped == 0:
            consecutive_empty += 1
            if consecutive_empty >= 2:
                break
        else:
            consecutive_empty = 0
        if total is not None and offset >= total:
            break
        if len(raw) < page_size:
            break

    return list(merged.values())
