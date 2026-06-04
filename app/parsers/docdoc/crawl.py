"""Обход DocDoc: главная → услуги (+ все отзывы) → клиники → врачи."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

from app.parsers.docdoc.crawl_checkpoint import (
    default_checkpoint_path,
    finalize_crawl_state,
    load_crawl_checkpoint,
    new_crawl_state,
    save_crawl_checkpoint,
)
from app.parsers.docdoc.doctors import parse_doctor_page
from app.parsers.docdoc.fetch import (
    fetch_html,
    fetch_html_batch,
    fetch_clinic_reviews_full,
    fetch_service_pages_dual,
    fetch_service_reviews_full,
    normalize_base_url,
)
from app.parsers.docdoc.main_page import (
    discover_service_urls,
    extract_category_hub_paths,
)
from app.parsers.docdoc.parse import parse_docdoc_html
from app.parsers.docdoc.reviews_fetch import (
    attach_reviews_to_service_parsed,
    collect_service_reviews_from_html,
    merge_reviews_by_id,
)
from app.parsers.docdoc.clinic_reviews_fetch import attach_reviews_to_clinic_parsed

log = logging.getLogger(__name__)


def _clinic_url(base: str, alias: str | None) -> str | None:
    if not alias:
        return None
    return urljoin(base, f"clinic/{alias.strip('/')}")


def _collect_clinic_urls_from_service(service_data: dict[str, Any], base: str) -> list[str]:
    urls: list[str] = []
    for row in service_data.get("clinics") or []:
        if not isinstance(row, dict):
            continue
        u = _clinic_url(base, row.get("clinic_alias"))
        if u:
            urls.append(u)
    return urls


def crawl_docdoc(
    base_url: str = "https://irk.docdoc.ru/",
    *,
    max_services: int | None = 20,
    max_clinics: int | None = 10,
    max_doctor_profiles: int | None = 0,
    fetch_clinics: bool = True,
    full_reviews: bool = True,
    dual_review_pages: bool = True,
    discover_category_hubs: bool = True,
    headless: bool | None = None,
    checkpoint_path: Path | str | None = None,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> dict[str, Any]:
    """
    full_reviews: Playwright — «Показать ещё» + /order/reviews.
    discover_category_hubs: главная ~177 услуг; хабы направлений ~1100+.
    checkpoint_path: промежуточный JSON (обновляется по ходу, см. DOCDOC_CRAWL_CHECKPOINT).
    """
    base = normalize_base_url(base_url)
    ckpt_path = Path(checkpoint_path or default_checkpoint_path())

    def prog(phase: str, i: int, n: int) -> None:
        if on_progress:
            on_progress(phase, i, n)
        log.info("%s [%s/%s]", phase, i, n)

    def persist(state: dict[str, Any]) -> None:
        save_crawl_checkpoint(ckpt_path, state)

    fetch_kw = {"headless": headless}
    state = new_crawl_state(base_url=base)

    try:
        main_html = fetch_html(base, **fetch_kw)
        main_data = parse_docdoc_html(main_html, base)
        if main_data.get("page_kind") != "main" or not main_data.get("ok"):
            state["status"] = "failed"
            state["error"] = "main_page_parse_failed"
            state["main"] = main_data
            persist(state)
            return {"ok": False, "error": "main_page_parse_failed", "base_url": base, "main": main_data}

        state["city_slug"] = main_data.get("city_slug")
        discovery_stats: dict[str, int] = {}

        state["status"] = "discovering"
        persist(state)

        if discover_category_hubs:
            hub_paths = extract_category_hub_paths(main_html)
            hub_html_by_path: dict[str, str] = {}
            if hub_paths:
                hub_urls = [urljoin(base, p) for p in hub_paths]

                def _hub_chunk_done(_html: dict[str, str], done: int, total: int) -> None:
                    state["status"] = f"category_hub:{done}/{total}"
                    persist(state)

                hub_html_map = fetch_html_batch(
                    hub_urls,
                    on_progress=lambda i, n, u: prog("category_hub", i, n),
                    on_chunk_done=_hub_chunk_done,
                    **fetch_kw,
                )
                for path in hub_paths:
                    full_u = urljoin(base, path)
                    hub_html_by_path[path] = hub_html_map.get(full_u, "")
            service_urls, discovery_stats = discover_service_urls(main_html, base, hub_html_by_path)
        else:
            service_urls = list(main_data.get("service_urls") or [])
            discovery_stats = {"from_main": len(service_urls), "total_unique": len(service_urls)}

        if max_services is not None:
            service_urls = service_urls[: max(0, max_services)]

        state["service_urls"] = service_urls
        state["discovery"] = discovery_stats
        state["stats"] = {
            "services_discovered": discovery_stats.get("total_unique", len(service_urls)),
            "services_from_main": discovery_stats.get("from_main"),
            "services_from_hubs": discovery_stats.get("from_hubs"),
            "category_hubs_scanned": discovery_stats.get("hubs_scanned"),
            "service_urls_planned": len(service_urls),
        }
        state["status"] = "discovered"
        persist(state)

        reviews_by_url: dict[str, list[dict[str, Any]]] = dict(state.get("reviews_by_url") or {})
        dual_html: dict[str, tuple[str, str]] = {}

        if service_urls and full_reviews:
            state["status"] = "service_reviews"

            def _reviews_chunk_done(part: dict[str, list], done: int, total: int) -> None:
                reviews_by_url.update(part)
                state["reviews_by_url"] = reviews_by_url
                state["status"] = f"service_reviews:{done}/{total}"
                persist(state)

            reviews_by_url = fetch_service_reviews_full(
                service_urls,
                on_progress=lambda i, n, u: prog("service_reviews", i, n),
                on_chunk_done=_reviews_chunk_done,
                **fetch_kw,
            )
            state["reviews_by_url"] = reviews_by_url
            persist(state)
        elif service_urls and dual_review_pages:
            dual_html = fetch_service_pages_dual(
                service_urls,
                on_progress=lambda i, n, u: prog("service", i, n),
                **fetch_kw,
            )

        services_parsed: list[dict[str, Any]] = []
        all_doctors: list[dict[str, Any]] = []
        doctors_profiles: list[dict[str, Any]] = []
        clinic_urls_set: set[str] = set()
        doctor_urls_set: set[str] = set()

        parse_chunk = int(os.getenv("DOCDOC_PARSE_CHUNK_SIZE", "40"))
        state["status"] = "service_parse"
        persist(state)

        for start in range(0, len(service_urls), parse_chunk):
            chunk_urls = service_urls[start : start + parse_chunk]
            html_by_url = fetch_html_batch(
                chunk_urls,
                on_progress=lambda i, n, u: prog(
                    "service_parse", start + i, len(service_urls)
                ),
                **fetch_kw,
            )
            for url in chunk_urls:
                html = html_by_url.get(url, "")
                parsed = parse_docdoc_html(html, url)
                if not parsed.get("ok"):
                    continue

                if full_reviews and url in reviews_by_url:
                    parsed = attach_reviews_to_service_parsed(parsed, reviews_by_url[url])
                elif url in dual_html:
                    main_h, rev_h = dual_html[url]
                    merged = collect_service_reviews_from_html(main_h or html, url, rev_h)
                    parsed = attach_reviews_to_service_parsed(parsed, merged)

                services_parsed.append(parsed)
                for d in parsed.get("doctors") or []:
                    all_doctors.append(d)
                    if d.get("profile_url"):
                        doctor_urls_set.add(d["profile_url"])
                for cu in _collect_clinic_urls_from_service(parsed, base):
                    clinic_urls_set.add(cu)

            state["services_parsed"] = services_parsed
            state["doctors"] = all_doctors
            state["status"] = f"service_parse:{min(start + len(chunk_urls), len(service_urls))}/{len(service_urls)}"
            persist(state)

        clinic_urls = sorted(clinic_urls_set)
        if max_clinics is not None:
            clinic_urls = clinic_urls[: max(0, max_clinics)]

        clinics_parsed: list[dict[str, Any]] = []
        if fetch_clinics and clinic_urls:
            state["status"] = "clinics"
            persist(state)
            clinic_reviews_by_url: dict[str, list[dict[str, Any]]] = {}
            if full_reviews:
                def _clinic_rev_chunk_done(part: dict[str, list], done: int, total: int) -> None:
                    clinic_reviews_by_url.update(part)
                    state["status"] = f"clinic_reviews:{done}/{total}"
                    persist(state)

                clinic_reviews_by_url = fetch_clinic_reviews_full(
                    clinic_urls,
                    on_progress=lambda i, n, u: prog("clinic_reviews", i, n),
                    on_chunk_done=_clinic_rev_chunk_done,
                    **fetch_kw,
                )
            html_by_url = fetch_html_batch(
                clinic_urls,
                on_progress=lambda i, n, u: prog("clinic", i, n),
                **fetch_kw,
            )
            for url in clinic_urls:
                parsed = parse_docdoc_html(html_by_url.get(url, ""), url)
                if not parsed.get("ok"):
                    continue
                if full_reviews and url in clinic_reviews_by_url:
                    parsed = attach_reviews_to_clinic_parsed(parsed, clinic_reviews_by_url[url])
                elif not full_reviews:
                    pass
                clinics_parsed.append(parsed)
            state["clinics_parsed"] = clinics_parsed
            persist(state)

        doctor_urls = sorted(doctor_urls_set)
        if max_doctor_profiles is not None and max_doctor_profiles > 0:
            doctor_urls = doctor_urls[:max_doctor_profiles]
            if doctor_urls:
                state["status"] = "doctors"
                persist(state)
                html_by_url = fetch_html_batch(
                    doctor_urls,
                    on_progress=lambda i, n, u: prog("doctor", i, n),
                    **fetch_kw,
                )
                for url in doctor_urls:
                    dp = parse_doctor_page(html_by_url.get(url, ""), url, base)
                    if dp.get("ok"):
                        doctors_profiles.append(dp)
                state["doctor_profiles"] = doctors_profiles
                persist(state)

        state["main"] = {
            "service_url_count": main_data.get("service_url_count"),
            "service_urls_sample": service_urls[:5],
            "discovery": discovery_stats,
        }
        stats = dict(state.get("stats") or {})
        stats.update(
            {
                "services_fetched": len(services_parsed),
                "clinics_fetched": len(clinics_parsed),
                "doctors_on_services": len(all_doctors),
                "doctor_profiles_fetched": len(doctors_profiles),
                "clinic_urls_planned": len(clinic_urls),
                "doctor_profiles_planned": len(doctor_urls),
            }
        )
        state["stats"] = stats
        state["clinics_parsed"] = clinics_parsed
        state["doctor_profiles"] = doctors_profiles

        # Каноническое название города (Иркутск/Москва/...): берём из
        # первого распарсенного сервиса — там есть target_city_name из
        # preloadedState.city.cities[0].name (а если нет — из slug).
        target_city_name = None
        for sv in services_parsed:
            tcn = sv.get("target_city_name") if isinstance(sv, dict) else None
            if tcn:
                target_city_name = tcn
                break
        state["target_city_name"] = target_city_name

        result = finalize_crawl_state(state)
        persist(result)

        return {
            "ok": True,
            "base_url": base,
            "city_slug": result.get("city_slug"),
            "target_city_name": result.get("target_city_name") or target_city_name,
            "main": result.get("main"),
            "stats": result.get("stats"),
            "services": result.get("services_parsed") or [],
            "clinics": result.get("clinics_parsed") or [],
            "doctors": result.get("doctors") or [],
            "doctor_profiles": result.get("doctor_profiles") or [],
            "reviews": result.get("reviews") or [],
            "checkpoint_path": str(ckpt_path),
        }

    except Exception as exc:
        log.exception("crawl failed at status=%s", state.get("status"))
        state["status"] = "failed"
        state["error"] = str(exc)
        persist(state)
        raise


def fetch_and_parse(url: str, **fetch_kwargs) -> dict[str, Any]:
    html = fetch_html(url, **fetch_kwargs)
    return parse_docdoc_html(html, url)
