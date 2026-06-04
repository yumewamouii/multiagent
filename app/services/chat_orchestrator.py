"""
Топ-уровневый чат-роутер: решает, к какой системе адресовать запрос —
к DocDoc-аналитике (`docdoc_chat_router`) или к общему MultiAgentRuntime
(который ходит в `reviews_chunks` — telegram/общий ingestion).

Поддерживает multi-turn через session_id (sticky route: если последняя реплика
была про DocDoc и пользователь говорит «а теперь сравни …», остаёмся в DocDoc).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

from app.core.llm import chat_completion, parse_json_response
from app.services.docdoc_chat_session import (
    SessionStore,
    default_store as default_session_store,
)

log = logging.getLogger(__name__)

System = Literal["docdoc", "general"]

_DOCDOC_TRIGGERS = (
    "клиник",
    "медицинск",
    "медцентр",
    "услуг",
    "процедур",
    "врач",
    "доктор",
    "отзыв",
    "пациент",
    "репутац",
    "докдок",
    "docdoc",
    "сберздоров",
    "симптом",
    "приём",
    "прием у",
    "больниц",
    "поликлиник",
)

_GENERAL_TRIGGERS = (
    "товар",
    "продукт",
    "айфон",
    "iphone",
    "телефон",
    "ноутбук",
    "наушник",
    "телеграм",
    "telegram",
    "канал",
    "пост",
)

_DOCDOC_INTENT_RE = re.compile(
    r"\b(проанализируй|разбери|сделай разбор|сравни|сравнение|сравнить)\b",
    re.IGNORECASE,
)


def _heuristic_route(query: str) -> dict[str, Any]:
    low = query.lower()
    docdoc_hits = sum(1 for t in _DOCDOC_TRIGGERS if t in low)
    general_hits = sum(1 for t in _GENERAL_TRIGGERS if t in low)
    has_intent = bool(_DOCDOC_INTENT_RE.search(low))

    if docdoc_hits > 0 and docdoc_hits >= general_hits:
        confidence = 0.85 if docdoc_hits >= 2 or has_intent else 0.7
        return {"system": "docdoc", "confidence": confidence, "rationale": f"docdoc_triggers={docdoc_hits}"}
    if general_hits > 0:
        return {
            "system": "general",
            "confidence": 0.7 if general_hits >= 2 else 0.55,
            "rationale": f"general_triggers={general_hits}",
        }
    if has_intent:
        # «Сравни / проанализируй» без явных слов → ставим на DocDoc как более сильный сценарий
        return {"system": "docdoc", "confidence": 0.55, "rationale": "intent_only"}
    return {"system": "general", "confidence": 0.4, "rationale": "no_triggers"}


_LLM_SYSTEM = (
    "Ты top-роутер мультиагентной системы. Реши, в какую систему адресовать запрос: "
    "docdoc — отзывы и аналитика медицинских клиник DocDoc/СберЗдоровье; "
    "general — общая база отзывов (товары, telegram-каналы и пр.). "
    "Ответ — только JSON без markdown: "
    '{"system": "docdoc"|"general", "confidence": 0..1, "rationale": "..."}.'
)


def _llm_route(query: str, *, chat_completion_fn: Any | None = None) -> dict[str, Any] | None:
    chat_fn = chat_completion_fn or chat_completion
    try:
        raw = chat_fn(
            system_prompt=_LLM_SYSTEM,
            user_prompt=f"Запрос пользователя: {query}",
            temperature=0.0,
            max_tokens=120,
        )
    except Exception as exc:
        log.debug("top_route LLM failed: %s", exc)
        return None
    parsed = parse_json_response(raw)
    if not isinstance(parsed, dict):
        return None
    system = (parsed.get("system") or "").strip().lower()
    if system not in {"docdoc", "general"}:
        return None
    try:
        confidence = float(parsed.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "system": system,
        "confidence": max(0.0, min(1.0, confidence)),
        "rationale": str(parsed.get("rationale") or "llm")[:200],
    }


def detect_top_route(
    query: str,
    *,
    use_llm: bool = True,
    chat_completion_fn: Any | None = None,
    sticky_system: str | None = None,
) -> dict[str, Any]:
    """Решаем DocDoc vs general. Если есть sticky_system из сессии и запрос неоднозначный — sticky."""
    h = _heuristic_route(query)
    base = h
    if use_llm and h["confidence"] < 0.7:
        llm = _llm_route(query, chat_completion_fn=chat_completion_fn)
        if llm:
            base = {**h, **llm, "confidence": max(h["confidence"], llm["confidence"])}

    if sticky_system in {"docdoc", "general"} and base["confidence"] < 0.7:
        base = {
            **base,
            "system": sticky_system,
            "confidence": max(base["confidence"], 0.65),
            "rationale": f"{base.get('rationale','')}; sticky={sticky_system}".lstrip("; "),
        }
    return base


def run_orchestrator(
    query: str,
    *,
    session_id: str | None = None,
    city_slug: str | None = None,
    source_id: int | None = None,
    crawl_path: str | None = None,
    top_k: int = 5,
    use_llm: bool = True,
    use_rag: bool = True,
    system_override: str | None = None,
    session_store: SessionStore | None = None,
    detect_top_route_fn: Any | None = None,
    docdoc_run_fn: Any | None = None,
    general_orchestrate_fn: Any | None = None,
) -> dict[str, Any]:
    """Главный orchestrator: top-route + диспетчеризация в нужный пайплайн.

    Возвращает структуру, объединяющую ответ DocDoc-чата и/или общий answer мультиагентов.
    """
    store = session_store if session_store is not None else default_session_store
    session = store.get_or_create(session_id) if (session_id or session_store is not None) else None
    sticky = None
    if session is not None:
        # храним последний "system" в meta поверх last_intent — у docdoc-сессии last_intent заполнен
        if session.last_intent in {"reputation_analyze", "reputation_compare", "rag_search"}:
            sticky = "docdoc"

    detector = detect_top_route_fn or detect_top_route
    if system_override in {"docdoc", "general"}:
        route_info = {"system": system_override, "confidence": 1.0, "rationale": "override"}
    else:
        route_info = detector(query, use_llm=use_llm, sticky_system=sticky)

    out: dict[str, Any] = {
        "ok": True,
        "top_route": route_info,
        "answer": "",
    }

    if route_info["system"] == "docdoc":
        if docdoc_run_fn is None:
            from app.services.docdoc_chat_router import run_chat as _docdoc_run

            docdoc_run_fn = _docdoc_run
        result = docdoc_run_fn(
            query,
            city_slug=city_slug,
            source_id=source_id,
            crawl_path=crawl_path,
            use_llm=use_llm,
            use_rag=use_rag,
            session_id=session.session_id if session else session_id,
            session_store=store,
        )
        out.update(
            {
                "answer": result.get("answer", ""),
                "docdoc": {
                    "intent": result.get("intent"),
                    "reputation": result.get("reputation"),
                    "compare": result.get("compare"),
                    "rag": result.get("rag"),
                    "hint": result.get("hint"),
                },
                "session": result.get("session"),
            }
        )
        return out

    # general → общий MultiAgentRuntime; сессии у него своей нет, но мы зафиксируем
    # факт general-маршрутизации в нашей сессии (через дополнительное поле history)
    if general_orchestrate_fn is None:
        from app.agents.hierarchy import orchestrate as _general

        general_orchestrate_fn = _general

    general_result = general_orchestrate_fn(query=query, top_k=top_k)
    # general — это coroutine при дефолтном вызове
    if hasattr(general_result, "__await__"):
        return {
            "ok": True,
            "top_route": route_info,
            "answer": "",
            "general_pending_coroutine": general_result,  # caller должен await'нуть
        }
    out["general"] = general_result
    out["answer"] = general_result.get("answer", "") if isinstance(general_result, dict) else ""
    if session is not None:
        store.append_turn(
            session,
            user_query=query,
            bot_answer=out["answer"],
            intent="general",
            entity_type=None,
            entities=[],
            city_slug=None,
        )
        out["session"] = session.to_dict()
    return out


async def run_orchestrator_async(
    query: str,
    *,
    session_id: str | None = None,
    city_slug: str | None = None,
    source_id: int | None = None,
    crawl_path: str | None = None,
    top_k: int = 5,
    use_llm: bool = True,
    use_rag: bool = True,
    system_override: str | None = None,
    session_store: SessionStore | None = None,
) -> dict[str, Any]:
    """Async-обёртка: docdoc-часть синхронная (асинхронит её caller), general-часть — реально async."""
    import asyncio

    # docdoc сразу выполняем синхронно в thread, а если general — пропускаем и зовём async-orchestrate здесь.
    # Простой путь: сначала только определяем маршрут, потом исполняем.
    store = session_store if session_store is not None else default_session_store
    session = store.get_or_create(session_id) if (session_id or session_store is not None) else None
    sticky = None
    if session is not None and session.last_intent in {
        "reputation_analyze",
        "reputation_compare",
        "rag_search",
    }:
        sticky = "docdoc"

    if system_override in {"docdoc", "general"}:
        route_info = {"system": system_override, "confidence": 1.0, "rationale": "override"}
    else:
        route_info = await asyncio.to_thread(
            detect_top_route, query, use_llm=use_llm, sticky_system=sticky
        )

    if route_info["system"] == "docdoc":
        from app.services.docdoc_chat_router import run_chat as _docdoc_run

        result = await asyncio.to_thread(
            _docdoc_run,
            query,
            city_slug=city_slug,
            source_id=source_id,
            crawl_path=crawl_path,
            use_llm=use_llm,
            use_rag=use_rag,
            session_id=session.session_id if session else session_id,
            session_store=store,
        )
        return {
            "ok": True,
            "top_route": route_info,
            "answer": result.get("answer", ""),
            "docdoc": {
                "intent": result.get("intent"),
                "reputation": result.get("reputation"),
                "compare": result.get("compare"),
                "rag": result.get("rag"),
                "hint": result.get("hint"),
            },
            "session": result.get("session"),
        }

    from app.agents.hierarchy import orchestrate as _general

    general_result = await _general(query=query, top_k=top_k)
    answer = general_result.get("answer", "") if isinstance(general_result, dict) else ""
    if session is not None:
        store.append_turn(
            session,
            user_query=query,
            bot_answer=answer,
            intent="general",
            entity_type=None,
            entities=[],
            city_slug=None,
        )
    return {
        "ok": True,
        "top_route": route_info,
        "answer": answer,
        "general": general_result,
        "session": session.to_dict() if session else None,
    }


__all__ = [
    "detect_top_route",
    "run_orchestrator",
    "run_orchestrator_async",
]
