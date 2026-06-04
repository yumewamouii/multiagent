"""Разбор JSON-экспорта чата Telegram (Telethon / Desktop export)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)


@dataclass
class NormalizedTelegramMessage:
    """Нормализованное текстовое (или текст+медиа) сообщение для пайплайна ingest."""

    message_id: int
    text: str
    author_name: str
    author_id: str
    message_date: datetime | None
    reply_to_message_id: int | None = None
    media_type: str | None = None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


def extract_message_plain_text(message: dict[str, Any]) -> str:
    """
    Плоский текст: text, text_entities, caption (фото/видео/файл), опрос, стикер.
    """
    parts: list[str] = []

    raw = message.get("text")
    if isinstance(raw, str) and raw.strip():
        parts.append(raw.strip())
    elif isinstance(raw, list):
        parts.append(_text_from_fragments(raw))

    entities = message.get("text_entities")
    if not parts and isinstance(entities, list):
        parts.append(
            "".join(
                str(e.get("text") or "")
                for e in entities
                if isinstance(e, dict)
            ).strip()
        )

    caption = message.get("caption")
    if isinstance(caption, str) and caption.strip():
        parts.append(caption.strip())
    elif isinstance(caption, list):
        cap = _text_from_fragments(caption)
        if cap:
            parts.append(cap)

    poll = message.get("poll")
    if isinstance(poll, dict):
        q = str(poll.get("question") or "").strip()
        if q:
            parts.append(q)
        answers = poll.get("answers")
        if isinstance(answers, list):
            for a in answers:
                if isinstance(a, dict):
                    t = str(a.get("text") or "").strip()
                    if t:
                        parts.append(t)

    if not parts:
        emoji = message.get("sticker_emoji")
        if isinstance(emoji, str) and emoji.strip():
            media = message.get("media_type") or "sticker"
            parts.append(f"[{media}: {emoji.strip()}]")

    return "\n".join(p for p in parts if p).strip()


def _text_from_fragments(raw: list[Any]) -> str:
    out: list[str] = []
    for item in raw:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            if "text" in item:
                out.append(str(item.get("text") or ""))
    return "".join(out).strip()


def extract_message_author(message: dict[str, Any]) -> tuple[str, str]:
    author = (
        message.get("from")
        or message.get("sender_name")
        or message.get("actor")
        or ""
    )
    author_id = (
        message.get("from_id")
        or message.get("sender_id")
        or message.get("actor_id")
        or ""
    )
    return str(author or ""), str(author_id or "")


def parse_message_date(msg: dict[str, Any]) -> datetime | None:
    raw = msg.get("date")
    if not raw or not isinstance(raw, str):
        unix = msg.get("date_unixtime")
        if unix is not None:
            try:
                return datetime.utcfromtimestamp(int(unix))
            except (TypeError, ValueError, OSError):
                return None
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def export_meta(export: dict[str, Any]) -> tuple[int | None, str]:
    chat_id = export.get("id")
    cid: int | None = None
    if isinstance(chat_id, int):
        cid = chat_id
    elif isinstance(chat_id, str) and chat_id.lstrip("-").isdigit():
        cid = int(chat_id)
    name = export.get("name")
    return cid, str(name or "") if name is not None else ""


def export_meta_from_file(path: str | Path) -> tuple[int | None, str]:
    """Метаданные чата без загрузки всех сообщений (ijson при наличии)."""
    p = Path(path)
    try:
        import ijson  # type: ignore[import-untyped]

        meta: dict[str, Any] = {}
        for key in ("name", "type", "id"):
            try:
                with p.open("rb") as f:
                    meta[key] = next(ijson.items(f, key))
            except StopIteration:
                pass
        return export_meta(meta)
    except ImportError:
        export = load_telegram_export(p)
        return export_meta(export)


def export_unique_key(chat_id: int | None, chat_name: str) -> str:
    if chat_id is not None:
        return str(chat_id)
    slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in chat_name.strip())[:120]
    return f"name:{slug}" if slug else "unknown_chat"


def normalize_message(msg: dict[str, Any]) -> NormalizedTelegramMessage | None:
    if (msg.get("type") or "") != "message":
        return None
    mid = msg.get("id")
    if not isinstance(mid, int):
        return None
    text = extract_message_plain_text(msg)
    if not text:
        return None
    author_name, author_id = extract_message_author(msg)
    reply_to = msg.get("reply_to_message_id")
    reply_id = int(reply_to) if isinstance(reply_to, int) else None
    media_type = msg.get("media_type")
    if not media_type:
        if msg.get("photo"):
            media_type = "photo"
        elif msg.get("file"):
            media_type = "file"
    return NormalizedTelegramMessage(
        message_id=mid,
        text=text,
        author_name=author_name,
        author_id=author_id,
        message_date=parse_message_date(msg),
        reply_to_message_id=reply_id,
        media_type=str(media_type) if media_type else None,
        raw=msg,
    )


def iter_export_messages(export: dict[str, Any]) -> Iterator[dict[str, Any]]:
    messages = export.get("messages")
    if not isinstance(messages, list):
        return
    for msg in messages:
        if isinstance(msg, dict) and (msg.get("type") or "") == "message":
            yield msg


def iter_export_text_messages(
    export: dict[str, Any],
) -> Iterator[tuple[dict[str, Any], str]]:
    """Сообщения type=message с ненулевым текстом (совместимость с ingest)."""
    for msg in iter_export_messages(export):
        text = extract_message_plain_text(msg)
        if text:
            yield msg, text


def iter_normalized_messages(export: dict[str, Any]) -> Iterator[NormalizedTelegramMessage]:
    for msg in iter_export_messages(export):
        norm = normalize_message(msg)
        if norm:
            yield norm


def iter_messages_from_file(path: str | Path) -> Iterator[dict[str, Any]]:
    """
    Потоковое чтение messages[] (ijson). Без ijson — полная загрузка JSON в память.
    """
    p = Path(path)
    try:
        import ijson  # type: ignore[import-untyped]

        with p.open("rb") as f:
            for msg in ijson.items(f, "messages.item"):
                if isinstance(msg, dict):
                    yield msg
        return
    except ImportError:
        log.warning("ijson не установлен — загрузка всего %s в память", p.name)

    export = load_telegram_export(p)
    yield from iter_export_messages(export)


def iter_normalized_messages_from_file(path: str | Path) -> Iterator[NormalizedTelegramMessage]:
    for msg in iter_messages_from_file(path):
        norm = normalize_message(msg)
        if norm:
            yield norm


def load_telegram_export(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    with p.open(encoding="utf-8") as f:
        return json.load(f)


def scan_export(path: str | Path, *, sample_limit: int | None = None) -> dict[str, Any]:
    """
    Статистика экспорта без LLM/БД.
    sample_limit: макс. сообщений для детального скана (None = все).
    """
    p = Path(path)
    chat_id, chat_name = export_meta_from_file(p)
    export_key = export_unique_key(chat_id, chat_name)

    total_messages = 0
    total_type_message = 0
    with_text = 0
    with_media_only = 0
    service_events = 0

    for msg in iter_messages_from_file(p):
        total_messages += 1
        if sample_limit is not None and total_messages > sample_limit:
            break
        mtype = msg.get("type") or ""
        if mtype == "service":
            service_events += 1
            continue
        if mtype != "message":
            continue
        total_type_message += 1
        text = extract_message_plain_text(msg)
        if text:
            with_text += 1
        elif msg.get("photo") or msg.get("file") or msg.get("video"):
            with_media_only += 1

    return {
        "path": str(p.resolve()),
        "export_key": export_key,
        "chat_id": chat_id,
        "chat_name": chat_name,
        "total_records_scanned": total_messages,
        "type_message": total_type_message,
        "with_extractable_text": with_text,
        "media_without_text": with_media_only,
        "service_events": service_events,
        "ingest_candidates": with_text,
    }


def parse_export_file(
    path: str | Path,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    """
    Только парсинг (без LLM/БД): список нормализованных сообщений + мета.
    limit: None или 0 = без лимита.
    """
    effective_limit = None if limit in (None, 0) else limit
    chat_id, chat_name = export_meta_from_file(path)
    export_key = export_unique_key(chat_id, chat_name)
    messages: list[dict[str, Any]] = []
    for norm in iter_normalized_messages_from_file(path):
        if effective_limit is not None and len(messages) >= effective_limit:
            break
        messages.append(
            {
                "message_id": norm.message_id,
                "text": norm.text,
                "author_name": norm.author_name,
                "author_id": norm.author_id,
                "message_date": norm.message_date.isoformat() if norm.message_date else None,
                "reply_to_message_id": norm.reply_to_message_id,
                "media_type": norm.media_type,
            }
        )
    stats = scan_export(path, sample_limit=None if effective_limit is None else effective_limit)
    return {
        "ok": True,
        "export_key": export_key,
        "chat_id": chat_id,
        "chat_name": chat_name,
        "messages_parsed": len(messages),
        "stats": stats,
        "messages": messages,
    }
