"""Фильтр JSON-экспорта Telegram по году (потоковое чтение через ijson)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def message_year(msg: dict[str, Any]) -> int | None:
    raw = msg.get("date")
    if isinstance(raw, str) and len(raw) >= 4:
        try:
            return int(raw[:4])
        except ValueError:
            pass
    unix = msg.get("date_unixtime")
    if unix is not None:
        try:
            return datetime.fromtimestamp(int(unix), tz=timezone.utc).year
        except (TypeError, ValueError, OSError):
            return None
    return None


def filter_telegram_export_by_year(
    source: str | Path,
    destination: str | Path,
    *,
    year: int = 2026,
) -> dict[str, Any]:
    """
    Копирует метаданные чата и оставляет только сообщения за указанный год.
    Исходный файл не изменяется.
    """
    src = Path(source)
    dst = Path(destination)
    if not src.is_file():
        raise FileNotFoundError(src)

    try:
        import ijson  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError("Нужен ijson: pip install ijson") from exc

    meta: dict[str, Any] = {}
    for key in ("name", "type", "id"):
        with src.open("rb") as f:
            try:
                meta[key] = next(ijson.items(f, key))
            except StopIteration:
                pass

    filtered: list[dict[str, Any]] = []
    with src.open("rb") as f:
        for msg in ijson.items(f, "messages.item"):
            if not isinstance(msg, dict):
                continue
            y = message_year(msg)
            if y == year:
                filtered.append(msg)

    export = {**meta, "messages": filtered}
    dst.write_text(json.dumps(export, ensure_ascii=False, indent=1), encoding="utf-8")

    stats = {
        "source": str(src.resolve()),
        "destination": str(dst.resolve()),
        "year": year,
        "messages_kept": len(filtered),
        "chat_name": meta.get("name"),
        "chat_id": meta.get("id"),
    }
    log.info(
        "filtered %s -> %s: %s messages for %s",
        src.name,
        dst.name,
        len(filtered),
        year,
    )
    return stats
