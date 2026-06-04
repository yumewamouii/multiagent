"""Пагинация отзывов услуги через XHR `/review/service/more` (как на фронте DocDoc)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from app.parsers.docdoc.htmlutil import extract_next_data, normalize_saved_html
from app.parsers.docdoc.service import (
    _normalize_review,
    build_clinic_alias_city_map,
    review_belongs_to_city,
    target_city_name,
)

log = logging.getLogger(__name__)

REVIEWS_MORE_PATH = "/review/service/more"
DEFAULT_PAGE_SIZE = 5


@dataclass(frozen=True)
class ServiceReviewsApiContext:
    api_base_url: str
    service_id: int
    reviews_offset: int = 0
    reviews_sort: int = 0
    reviews_filter: int = 0
    reviews_count_total: int | None = None


def _page_size() -> int:
    raw = os.getenv("DOCDOC_REVIEWS_API_PAGE_SIZE", str(DEFAULT_PAGE_SIZE)).strip()
    try:
        n = int(raw)
        return max(1, n)
    except ValueError:
        return DEFAULT_PAGE_SIZE


def _max_pages() -> int:
    raw = os.getenv("DOCDOC_REVIEWS_API_MAX_PAGES", "200").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 200


def api_context_from_preloaded_state(ps: dict[str, Any]) -> ServiceReviewsApiContext | None:
    if not isinstance(ps, dict):
        return None
    dl = ps.get("defaultLayout")
    sp = ps.get("servicePage")
    if not isinstance(dl, dict) or not isinstance(sp, dict):
        return None
    api_base = dl.get("apiBaseUrl")
    if not api_base or not isinstance(api_base, str):
        return None
    service_id = sp.get("serviceId")
    if service_id is None:
        info = sp.get("serviceInfo")
        if isinstance(info, dict):
            service_id = info.get("id")
    if service_id is None:
        return None
    try:
        sid = int(service_id)
    except (TypeError, ValueError):
        return None
    total = sp.get("reviewsCount")
    try:
        total_int = int(total) if total is not None else None
    except (TypeError, ValueError):
        total_int = None
    initial = sp.get("reviews") if isinstance(sp.get("reviews"), list) else []
    offset = sp.get("reviewsOffset")
    try:
        offset_int = int(offset) if offset is not None else len(initial)
    except (TypeError, ValueError):
        offset_int = len(initial)
    return ServiceReviewsApiContext(
        api_base_url=api_base.rstrip("/"),
        service_id=sid,
        reviews_offset=max(0, offset_int),
        reviews_sort=int(sp.get("reviewsSort") or 0),
        reviews_filter=int(sp.get("reviewsFilter") or 0),
        reviews_count_total=total_int,
    )


def api_context_from_html(html: str) -> ServiceReviewsApiContext | None:
    html = normalize_saved_html(html)
    nd = extract_next_data(html)
    if not nd:
        return None
    ps = nd.get("props", {}).get("pageProps", {}).get("preloadedState")
    if not isinstance(ps, dict):
        return None
    return api_context_from_preloaded_state(ps)


def build_reviews_more_url(
    ctx: ServiceReviewsApiContext,
    *,
    offset: int | None = None,
) -> str:
    off = ctx.reviews_offset if offset is None else offset
    q = urlencode(
        {
            "serviceId": ctx.service_id,
            "offset": off,
            "filter": ctx.reviews_filter,
            "sort": ctx.reviews_sort,
        }
    )
    return f"{ctx.api_base_url}{REVIEWS_MORE_PATH}?{q}"


def parse_reviews_more_payload(data: Any) -> tuple[list[dict[str, Any]], int | None, str | None]:
    """
    Разбор ответа `/review/service/more`.
    Возвращает (сырые отзывы, total_count, error_message).
    """
    if not isinstance(data, dict):
        return [], None, "not_a_dict"
    if data.get("success") is False:
        msg = data.get("message")
        return [], None, str(msg) if msg is not None else "success_false"
    reviews = data.get("reviews")
    if not isinstance(reviews, list):
        return [], None, None
    total = data.get("total_count")
    if total is None:
        total = data.get("reviewsCount") or data.get("totalCount")
    try:
        total_int = int(total) if total is not None else None
    except (TypeError, ValueError):
        total_int = None
    raw = [r for r in reviews if isinstance(r, dict)]
    return raw, total_int, None


def normalize_api_reviews(
    raw_reviews: list[dict[str, Any]],
    *,
    service_url: str,
    service_id: int | None,
    service_name: str,
    parent_service_name: str,
    category_direction: str | None,
    page_size: int | None = None,
    target_city: str | None = None,
    alias_map: dict[str, bool] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """
    Возвращает (нормализованные отзывы, число_отброшенных_других_городов).
    target_city + alias_map: отзывы клиник из других городов отбрасываются.
    """
    limit = page_size if page_size is not None else _page_size()
    out: list[dict[str, Any]] = []
    dropped = 0
    for r in raw_reviews[:limit]:
        if (target_city or alias_map) and not review_belongs_to_city(
            r, target_city, alias_map=alias_map
        ):
            dropped += 1
            continue
        out.append(
            _normalize_review(
                r,
                service_id=service_id,
                service_name=service_name,
                parent_service_name=parent_service_name,
                service_url=service_url,
                category_direction=category_direction,
            )
        )
    return out, dropped


def service_meta_from_html(
    html: str, service_url: str
) -> tuple[int | None, str, str, str | None, str | None, dict[str, bool]]:
    """
    Возвращает (service_id, service_name, parent_service_name, direction,
    target_city, alias_map).
    alias_map: clinic.alias → True/False (наша/чужая клиника).
    """
    html = normalize_saved_html(html)
    nd = extract_next_data(html)
    if not nd:
        return None, "", "", None, None, {}
    ps = nd.get("props", {}).get("pageProps", {}).get("preloadedState")
    if not isinstance(ps, dict):
        return None, "", "", None, None, {}
    sp = ps.get("servicePage")
    if not isinstance(sp, dict):
        return None, "", "", None, None, {}
    info = sp.get("serviceInfo") if isinstance(sp.get("serviceInfo"), dict) else {}
    service_id = info.get("id") or sp.get("serviceId")
    try:
        sid = int(service_id) if service_id is not None else None
    except (TypeError, ValueError):
        sid = None
    crumbs = sp.get("breadcrumbs")
    direction = None
    if isinstance(crumbs, list) and crumbs:
        direction = str((crumbs[0] or {}).get("name") or "") or None
    target_city = target_city_name(ps, service_url)
    return (
        sid,
        str(info.get("name") or ""),
        str(info.get("parentServiceName") or ""),
        direction,
        target_city,
        build_clinic_alias_city_map(ps, target_city),
    )


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


def fetch_reviews_more_page(
    page: Any,
    ctx: ServiceReviewsApiContext,
    *,
    offset: int,
) -> tuple[list[dict[str, Any]], int | None, str | None]:
    """Один запрос к API из контекста браузера (cookies + anti-bot)."""
    url = build_reviews_more_url(ctx, offset=offset)
    try:
        result = page.evaluate(_FETCH_MORE_JS, {"url": url})
    except Exception as exc:
        return [], None, str(exc)
    if not isinstance(result, dict) or not result.get("ok"):
        return [], None, "fetch_not_json"
    data = result.get("data")
    return parse_reviews_more_payload(data)


class ReviewMoreResponseCollector:
    """Перехват ответов `/review/service/more` при кликах «Показать ещё»."""

    def __init__(self) -> None:
        self._raw_chunks: list[list[dict[str, Any]]] = []
        self._totals: list[int | None] = []

    @property
    def raw_chunks(self) -> list[list[dict[str, Any]]]:
        return self._raw_chunks

    def attach(self, page: Any) -> None:
        def _on_response(resp: Any) -> None:
            if "/review/service/more" not in (resp.url or ""):
                return
            try:
                if resp.status != 200:
                    return
                ct = (resp.headers.get("content-type") or "").lower()
                if "json" not in ct:
                    return
                raw, total, err = parse_reviews_more_payload(resp.json())
                if err:
                    log.debug("review more intercept error: %s", err)
                    return
                if raw:
                    self._raw_chunks.append(raw)
                    self._totals.append(total)
            except Exception:
                return

        page.on("response", _on_response)

    def reset(self) -> None:
        self._raw_chunks.clear()
        self._totals.clear()


def paginate_service_reviews_via_api(
    page: Any,
    html: str,
    service_url: str,
    *,
    initial_normalized: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """
    Догружает отзывы через API, пока offset < total_count. Сразу отбрасывает
    отзывы из чужих городов (для irk.docdoc.ru — оставляет только Иркутск).
    """
    ctx = api_context_from_html(html)
    if ctx is None:
        return []
    sid, service_name, parent_name, direction, target_city, alias_map = service_meta_from_html(
        html, service_url
    )
    page_size = _page_size()
    merged: dict[Any, dict[str, Any]] = {}
    for r in initial_normalized or []:
        rid = r.get("review_id")
        merged[rid if rid is not None else (r.get("text"), r.get("created"))] = r

    total = ctx.reviews_count_total
    # offset = how many we've consumed of the API stream, not how many we kept;
    # API возвращает все города, а наш `merged` — только наш город.
    offset = ctx.reviews_offset
    if total is not None and offset >= total:
        return list(merged.values())

    pages = 0
    consecutive_empty = 0
    while pages < _max_pages():
        raw, api_total, err = fetch_reviews_more_page(page, ctx, offset=offset)
        if err:
            log.debug("reviews API offset=%s: %s", offset, err)
            break
        if api_total is not None:
            total = api_total
        if not raw:
            break
        batch, dropped = normalize_api_reviews(
            raw,
            service_url=service_url,
            service_id=sid or ctx.service_id,
            service_name=service_name,
            parent_service_name=parent_name,
            category_direction=direction,
            page_size=page_size,
            target_city=target_city,
            alias_map=alias_map,
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
