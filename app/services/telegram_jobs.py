"""Фоновый ingest Telegram-экспорта (долгие JSON без обрыва HTTP)."""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services import telegram_ingest

log = logging.getLogger(__name__)

_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def start_ingest_job(
    path: Path,
    *,
    limit: int | None,
    heuristic_short_circuit: bool,
) -> str:
    job_id = uuid.uuid4().hex
    with _lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "created_at": _now(),
            "updated_at": _now(),
            "export_path": str(path),
            "error": None,
            "result": None,
        }

    def _run() -> None:
        _set_job(job_id, status="running")
        try:
            result = telegram_ingest.ingest_telegram_export_file(
                path,
                limit=limit,
                use_heuristic_short_circuit=heuristic_short_circuit,
            )
            _set_job(job_id, status="completed", result=result)
        except Exception as exc:
            log.exception("telegram ingest job %s failed", job_id)
            _set_job(job_id, status="failed", error=str(exc))

    threading.Thread(target=_run, name=f"tg-ingest-{job_id[:8]}", daemon=True).start()
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
