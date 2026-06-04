"""Фоновые задачи crawl DocDoc (HTTP не ждёт 10+ часов)."""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.parsers.docdoc.crawl import crawl_docdoc
from app.parsers.docdoc.crawl_checkpoint import (
    default_checkpoint_path,
    load_crawl_checkpoint,
    save_crawl_checkpoint,
)
from app.services import docdoc_ingest

log = logging.getLogger(__name__)

_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def start_crawl_job(
    *,
    base_url: str,
    max_services: int | None,
    max_clinics: int | None,
    max_doctor_profiles: int,
    fetch_clinics: bool,
    full_reviews: bool,
    dual_review_pages: bool,
    discover_category_hubs: bool,
    headless: bool,
    save_to_db: bool,
    checkpoint_path: Path | str | None = None,
) -> str:
    job_id = uuid.uuid4().hex
    ckpt = Path(checkpoint_path or default_checkpoint_path())
    backup = Path("docdoc_crawl_last.json")

    with _lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "created_at": _now(),
            "updated_at": _now(),
            "checkpoint_path": str(ckpt),
            "backup_path": str(backup),
            "error": None,
            "result_stats": None,
            "db_inserted": None,
        }

    def _run() -> None:
        _set_job(job_id, status="running")
        try:
            result = crawl_docdoc(
                base_url,
                max_services=max_services,
                max_clinics=max_clinics,
                max_doctor_profiles=max_doctor_profiles,
                fetch_clinics=fetch_clinics,
                full_reviews=full_reviews,
                dual_review_pages=dual_review_pages,
                discover_category_hubs=discover_category_hubs,
                headless=headless,
                checkpoint_path=ckpt,
            )
            import json

            backup.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            db_inserted = None
            if save_to_db and result.get("ok"):
                ing = docdoc_ingest.ingest_docdoc_crawl_result(result)
                db_inserted = ing.get("inserted")
            _set_job(
                job_id,
                status="completed",
                result_stats=result.get("stats"),
                db_inserted=db_inserted,
                city_slug=result.get("city_slug"),
            )
        except Exception as exc:
            log.exception("docdoc crawl job %s failed", job_id)
            partial = load_crawl_checkpoint(ckpt)
            if partial:
                save_crawl_checkpoint(ckpt, partial)
            _set_job(job_id, status="failed", error=str(exc))

    threading.Thread(target=_run, name=f"docdoc-crawl-{job_id[:8]}", daemon=True).start()
    return job_id


def _set_job(job_id: str, **fields: Any) -> None:
    with _lock:
        if job_id not in _jobs:
            return
        _jobs[job_id].update(fields)
        _jobs[job_id]["updated_at"] = _now()


def get_job(job_id: str) -> dict[str, Any] | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def ingest_checkpoint_file(
    path: Path | str,
    *,
    require_completed: bool = False,
) -> dict[str, Any]:
    """Загрузить checkpoint/backup в БД."""
    from app.parsers.docdoc.crawl_checkpoint import finalize_crawl_state

    raw = load_crawl_checkpoint(path)
    if not raw:
        return {"ok": False, "error": "checkpoint_not_found"}

    services = raw.get("services_parsed") or raw.get("services") or []
    if not services:
        return {
            "ok": False,
            "error": "no_services_in_checkpoint",
            "status": raw.get("status"),
        }

    if raw.get("ok") and raw.get("services"):
        crawl = dict(raw)
    else:
        crawl = finalize_crawl_state(dict(raw))
        crawl["services"] = crawl.pop("services_parsed", services)
        crawl["clinics"] = crawl.pop("clinics_parsed", crawl.get("clinics_parsed") or [])

    crawl["ok"] = True
    ing = docdoc_ingest.ingest_docdoc_crawl_result(crawl)
    return {"ok": True, "ingest": ing, "status": raw.get("status")}
