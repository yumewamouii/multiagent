"""Промежуточное сохранение длинного crawl (сбой / таймаут HTTP)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def default_checkpoint_path() -> Path:
    import os

    return Path(os.getenv("DOCDOC_CRAWL_CHECKPOINT", "docdoc_crawl_checkpoint.json"))


def new_crawl_state(*, base_url: str, city_slug: str | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "started",
        "updated_at": _now_iso(),
        "base_url": base_url,
        "city_slug": city_slug,
        "service_urls": [],
        "discovery": {},
        "reviews_by_url": {},
        "services_parsed": [],
        "clinics_parsed": [],
        "doctors": [],
        "doctor_profiles": [],
        "reviews": [],
        "stats": {},
        "error": None,
    }


def save_crawl_checkpoint(path: Path | str, state: dict[str, Any]) -> None:
    p = Path(path)
    state = dict(state)
    state["updated_at"] = _now_iso()
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
        log.info("checkpoint saved %s (phase=%s)", p, state.get("status"))
    except OSError as exc:
        log.warning("checkpoint save failed %s: %s", p, exc)


def load_crawl_checkpoint(path: Path | str) -> dict[str, Any] | None:
    p = Path(path)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("checkpoint load failed %s: %s", p, exc)
        return None


def finalize_crawl_state(state: dict[str, Any]) -> dict[str, Any]:
    from app.parsers.docdoc.reviews_fetch import merge_reviews_by_id

    review_chunks: list[list[dict[str, Any]]] = []
    for parsed in state.get("services_parsed") or []:
        if isinstance(parsed, dict):
            review_chunks.append(parsed.get("reviews") or [])
    for parsed in state.get("clinics_parsed") or []:
        if isinstance(parsed, dict):
            review_chunks.append(parsed.get("reviews") or [])
    for dp in state.get("doctor_profiles") or []:
        if isinstance(dp, dict):
            review_chunks.append(dp.get("reviews") or [])
    reviews = merge_reviews_by_id(review_chunks)

    state = dict(state)
    state["ok"] = True
    state["status"] = "completed"
    state["reviews"] = reviews
    state["updated_at"] = _now_iso()
    stats = dict(state.get("stats") or {})
    stats["reviews_collected"] = len(reviews)
    stats["services_fetched"] = len(state.get("services_parsed") or [])
    stats["clinics_fetched"] = len(state.get("clinics_parsed") or [])
    state["stats"] = stats
    return state


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
