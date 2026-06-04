"""Загрузка экспорта Telegram: эвристики спама/рекламы и LLM-разбор интересов к исследованиям рынка."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.llm import chat_completion, parse_json_response
from app.models import orm as models
from app.parsers.telegram_export import (
    NormalizedTelegramMessage,
    export_meta,
    export_meta_from_file,
    export_unique_key,
    iter_normalized_messages,
    iter_normalized_messages_from_file,
    load_telegram_export,
    scan_export,
)
from app.rag.embedding import create_embedding

log = logging.getLogger(__name__)

# --- быстрые правила: массовые приглашения, промо, короткие объявления со ссылками ---
_SPAM_PHRASES = re.compile(
    r"|".join(
        re.escape(x)
        for x in (
            "приглашаем принять участие",
            "приглашаем вас",
            "регистрац",
            "промокод",
            "скидк",
            "рассылк",
            "подписывайтесь на канал",
            "переходите по ссылке",
            "успейте купить",
            "только сегодня",
            "бесплатный вебинар",
            "запись вебинара",
            "купить курс",
            "оплатить",
        )
    ),
    re.IGNORECASE,
)

_ZOOM_MEET = re.compile(r"zoom\.us|идентификатор конференции|код доступа:\s*\d", re.I)
_LINK = re.compile(r"https?://\S+|t\.me/\S+", re.I)
_EVENT_WORDS = re.compile(
    r"мозговой штурм|вебинар|конференц|трансляц|прямой эфир|не пропустите",
    re.I,
)


def heuristic_spam_or_ad(text: str) -> tuple[bool, str]:
    """
    Эвристика для быстрого отсечения явного промо/спама без вызова LLM.
    Возвращает (spam_or_ad, короткий код причины).
    """
    t = (text or "").strip()
    if not t:
        return True, "empty"
    if len(t) < 12:
        return False, ""

    spam_hits = 0
    if _SPAM_PHRASES.search(t):
        spam_hits += 1
    if _EVENT_WORDS.search(t) and (_LINK.search(t) or _ZOOM_MEET.search(t)):
        spam_hits += 1
    if _ZOOM_MEET.search(t) and len(t) < 1200:
        spam_hits += 1

    # Чистое объявление: мало текста, много ссылок/призыв
    if len(t) < 350 and _LINK.search(t) and _EVENT_WORDS.search(t):
        spam_hits += 1

    if spam_hits >= 2:
        return True, "promo_event_or_links"
    if spam_hits == 1 and _SPAM_PHRASES.search(t) and len(t) < 500:
        return True, "short_promo_cta"
    return False, ""


def _normalize_topics(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, list):
        return ", ".join(str(x).strip() for x in raw if str(x).strip())
    return str(raw).strip()


def _parse_telegram_llm(content: str) -> dict[str, Any] | None:
    if not (content or "").strip():
        return None
    parsed = parse_json_response(content)
    if isinstance(parsed, dict):
        return parsed
    # запасной поиск ключей
    spam_m = re.search(r'"spam_or_ad"\s*:\s*(true|false)', content, re.I)
    interest_m = re.search(r'"market_research_interest"\s*:\s*(true|false)', content, re.I)
    topics_m = re.search(r'"topics"\s*:\s*\[(.*?)\]', content, re.DOTALL)
    summary_m = re.search(r'"summary"\s*:\s*"((?:[^"\\]|\\.)*)"', content, re.DOTALL)
    reason_m = re.search(r'"spam_reason"\s*:\s*"((?:[^"\\]|\\.)*)"', content, re.DOTALL)
    out: dict[str, Any] = {}
    if spam_m:
        out["spam_or_ad"] = spam_m.group(1).lower() == "true"
    if interest_m:
        out["market_research_interest"] = interest_m.group(1).lower() == "true"
    if summary_m:
        out["summary"] = summary_m.group(1).replace("\\n", "\n").strip()
    if reason_m:
        out["spam_reason"] = reason_m.group(1).strip()
    if topics_m:
        inner = topics_m.group(1)
        parts = re.findall(r'"([^"]*)"', inner)
        out["topics"] = parts if parts else []
    return out if out else None


def analyze_marketer_message_with_llm(text: str) -> dict[str, Any]:
    system_prompt = (
        "Ты анализируешь сообщение из чата маркетологов медицинских клиник. "
        "Ответь одним JSON на русском, без markdown и без текста вокруг. Схема: "
        '{"spam_or_ad": <true|false>, "spam_reason": "<кратко или пустая строка>", '
        '"market_research_interest": <true|false>, '
        '"topics": ["короткая тема 1", ...], '
        '"summary": "<1-2 предложения>"}. '
        "spam_or_ad: массовые приглашения, чистая реклама услуг/курсов, оффтоп, шум без смысла. "
        "market_research_interest: обсуждение или вопросы про ЦА, спрос, конкурентов, цены, "
        "упаковку клиники, рекламные каналы, аналитику, исследования рынка, positioning."
    )
    user_prompt = f"Сообщение:\n{text[:8000]}"
    try:
        content = chat_completion(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.1,
            max_tokens=400,
        )
    except Exception:
        return {
            "spam_or_ad": False,
            "spam_reason": "llm_error",
            "market_research_interest": True,
            "topics": "",
            "summary": text[:200],
            "analysis_source": "llm_fallback",
        }

    parsed = _parse_telegram_llm(content)
    if not parsed:
        return {
            "spam_or_ad": False,
            "spam_reason": "unparsed",
            "market_research_interest": True,
            "topics": "",
            "summary": (content or text)[:300],
            "analysis_source": "llm_fallback",
        }

    topics = _normalize_topics(parsed.get("topics"))
    return {
        "spam_or_ad": bool(parsed.get("spam_or_ad")),
        "spam_reason": str(parsed.get("spam_reason") or "")[:300],
        "market_research_interest": bool(parsed.get("market_research_interest")),
        "topics": topics[:500],
        "summary": str(parsed.get("summary") or "")[:800],
        "analysis_source": "llm",
    }


def _merge_analysis(
    text: str,
    *,
    heuristic_spam: bool,
    heuristic_reason: str,
    use_heuristic_short_circuit: bool,
) -> dict[str, Any]:
    if use_heuristic_short_circuit and heuristic_spam:
        return {
            "spam_or_ad": True,
            "spam_reason": heuristic_reason or "heuristic",
            "market_research_interest": False,
            "topics": "",
            "summary": "",
            "analysis_source": "heuristic",
        }
    llm = analyze_marketer_message_with_llm(text)
    if heuristic_spam and not llm["spam_or_ad"]:
        # доверяем эвристике только как мягкому сигналу; LLM мог переопределить
        pass
    return llm


def _embedding_input(body: str, summary: str, topics: str) -> str:
    base = f"{summary}. {topics}. {body}".strip()
    return base[:6000]


def _effective_limit(limit: int | None) -> int | None:
    """None или 0 — обработать все текстовые сообщения."""
    if limit is None or limit == 0:
        return None
    return max(1, limit)


def _body_for_analysis(norm: NormalizedTelegramMessage) -> str:
    if norm.reply_to_message_id:
        return f"[ответ на #{norm.reply_to_message_id}] {norm.text}"
    return norm.text


def _row_from_analysis(
    norm: NormalizedTelegramMessage,
    analysis: dict[str, Any],
    *,
    export_key: str,
    chat_id: int | None,
    chat_name: str,
) -> models.TelegramChatAnalysis:
    spam = analysis["spam_or_ad"]
    summary = analysis.get("summary") or ""
    topics = analysis.get("topics") or ""
    body = _body_for_analysis(norm)

    emb: list[float] | None = None
    if not spam:
        emb = create_embedding(_embedding_input(body, summary, topics))

    return models.TelegramChatAnalysis(
        export_key=export_key[:160],
        export_chat_id=chat_id,
        export_chat_name=chat_name[:512] if chat_name else "",
        telegram_message_id=norm.message_id,
        message_date=norm.message_date,
        author_name=norm.author_name[:256],
        author_id=norm.author_id[:128],
        body=body,
        spam_or_ad=spam,
        spam_reason=str(analysis.get("spam_reason") or "")[:512],
        market_research_interest=bool(analysis.get("market_research_interest")),
        topics=topics,
        summary=summary,
        analysis_source=str(analysis.get("analysis_source") or "")[:32],
        embedding=emb,
    )


def ingest_telegram_export_file(
    path: str | Path,
    *,
    limit: int | None = None,
    use_heuristic_short_circuit: bool | None = None,
    batch_commit: int | None = None,
) -> dict[str, Any]:
    """
    Читает JSON-экспорт → эвристика/LLM/эмбеддинг → telegram_chat_analyses.
    limit: None или 0 = без лимита (все сообщения с текстом).
    """
    if use_heuristic_short_circuit is None:
        use_heuristic_short_circuit = (
            os.getenv("TELEGRAM_HEURISTIC_SPAM_SHORT_CIRCUIT", "true").lower() == "true"
        )
    if batch_commit is None:
        batch_commit = int(os.getenv("TELEGRAM_INGEST_BATCH", "32"))

    p = Path(path)
    effective_limit = _effective_limit(limit)
    chat_id, chat_name = export_meta_from_file(p)
    export_key = export_unique_key(chat_id, chat_name)

    inserted = 0
    skipped_existing = 0
    skipped_empty = 0
    processed = 0
    spam_count = 0
    market_count = 0
    log_every = int(os.getenv("TELEGRAM_INGEST_LOG_EVERY", "100"))

    pending: list[models.TelegramChatAnalysis] = []

    def flush(db: Session) -> None:
        nonlocal pending, inserted
        if not pending:
            return
        db.add_all(pending)
        db.commit()
        inserted += len(pending)
        pending = []

    def _iter_messages():
        try:
            import ijson  # noqa: F401

            yield from iter_normalized_messages_from_file(p)
        except ImportError:
            export = load_telegram_export(p)
            yield from iter_normalized_messages(export)

    with SessionLocal() as db:
        for norm in _iter_messages():
            if effective_limit is not None and processed >= effective_limit:
                break
            processed += 1

            if processed % log_every == 0:
                log.info(
                    "telegram ingest %s: processed=%s inserted=%s skipped=%s",
                    export_key,
                    processed,
                    inserted,
                    skipped_existing,
                )

            exists = db.scalar(
                select(models.TelegramChatAnalysis).where(
                    models.TelegramChatAnalysis.export_key == export_key,
                    models.TelegramChatAnalysis.telegram_message_id == norm.message_id,
                )
            )
            if exists:
                skipped_existing += 1
                continue

            text = norm.text
            h_spam, h_reason = heuristic_spam_or_ad(text)
            analysis = _merge_analysis(
                text,
                heuristic_spam=h_spam,
                heuristic_reason=h_reason,
                use_heuristic_short_circuit=use_heuristic_short_circuit,
            )
            if analysis["spam_or_ad"]:
                spam_count += 1
            if analysis.get("market_research_interest"):
                market_count += 1

            pending.append(
                _row_from_analysis(
                    norm,
                    analysis,
                    export_key=export_key,
                    chat_id=chat_id,
                    chat_name=chat_name,
                )
            )

            if len(pending) >= batch_commit:
                flush(db)

        flush(db)

    return {
        "ok": True,
        "export_key": export_key,
        "chat_id": chat_id,
        "chat_name": chat_name,
        "limit": effective_limit,
        "processed_messages": processed,
        "inserted": inserted,
        "skipped_existing": skipped_existing,
        "skipped_empty": skipped_empty,
        "spam_flagged": spam_count,
        "market_research_flagged": market_count,
        "path": str(p.resolve()),
    }


def aggregate_market_topics_data(db: Session, *, export_key: str | None = None) -> dict[str, Any]:
    stmt = select(models.TelegramChatAnalysis).where(
        models.TelegramChatAnalysis.market_research_interest.is_(True),
        models.TelegramChatAnalysis.spam_or_ad.is_(False),
    )
    if export_key:
        stmt = stmt.where(models.TelegramChatAnalysis.export_key == export_key)
    rows = db.execute(stmt).scalars().all()
    topics_count: dict[str, int] = {}
    for r in rows:
        for part in re.split(r"[,;]", r.topics or ""):
            p = part.strip().lower()
            if len(p) < 2:
                continue
            topics_count[p] = topics_count.get(p, 0) + 1
    top = sorted(topics_count.items(), key=lambda x: -x[1])[:80]
    return {"unique_topic_keys": len(topics_count), "top_topics": top, "messages_count": len(rows)}


def aggregate_market_topics_json(db: Session, *, export_key: str | None = None) -> str:
    return json.dumps(aggregate_market_topics_data(db, export_key=export_key), ensure_ascii=False, indent=2)
