"""
Маршрутизатор пользовательских вопросов в DocDoc-чате.

Определяет, чего хочет пользователь:
- reputation_analyze — «Проанализируй отзывы по …»
- reputation_compare — «Сравни X и Y»
- rag_search        — «Что говорят про …», «Покажи отзывы про …»
- fallback          — что-то другое (ответим RAG-поиском по умолчанию)

И достаёт сущности (clinic/service/category/doctor) из текста.
Под капотом: regex-эвристика + опциональный LLM-уточнитель.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

from app.core.llm import chat_completion, parse_json_response
from app.services.docdoc_chat_session import (
    ChatSession,
    SessionStore,
    default_store,
)

log = logging.getLogger(__name__)

_CONTINUATION_TRIGGERS = (
    "а теперь",
    "теперь по",
    "то же",
    "тоже самое",
    "то же самое",
    "ту же",
    "тот же",
    "по той же",
    "по тому же",
    "и ещё",
    "ещё одна",
    "ещё одного",
    "ещё одну",
    "тогда",
    "и по",
)

Intent = Literal["reputation_analyze", "reputation_compare", "rag_search", "fallback"]
EntityType = Literal["clinic", "service", "category", "doctor"]

_ANALYZE_TRIGGERS = (
    "проанализируй",
    "разбери",
    "сделай разбор",
    "репутац",
    "что раздража",
    "что хвалят",
    "опиши репутацию",
    "что не нравится",
    "что нравится пациентам",
)
_COMPARE_TRIGGERS = (
    "сравни ",
    "сравнить ",
    "сравнение ",
    " vs ",
    " против ",
    "что лучше",
    "лучше ли",
)
_SEARCH_TRIGGERS = (
    "что говорят про",
    "что пишут про",
    "покажи отзывы",
    "найди отзывы",
    "отзывы про",
)

_ENTITY_TYPE_HINTS: dict[str, EntityType] = {
    "клиник": "clinic",
    "медицинск центр": "clinic",
    "медцентр": "clinic",
    "услуг": "service",
    "процедур": "service",
    "направлен": "category",
    "категор": "category",
    "врач": "doctor",
    "доктор": "doctor",
    "специалист": "doctor",
}

_QUOTED_RE = re.compile(r"[«\"'']\s*([^«»\"'']{2,120}?)\s*[»\"'']")
_AFTER_KEY_RE = re.compile(
    r"(?:по\s+(?:услуге|клинике|категории|направлению|врачу|доктору)|"
    r"клиник[уи]|услуг[уе]|направлени[ея]|врач[ау]|доктор[ау])\s+([^.;:,!?\n]{3,120})",
    re.IGNORECASE,
)
_COMPARE_PAIR_RE = re.compile(
    r"(?:сравни(?:ть)?|сравнение)\s+(.+?)\s+(?:и|с|vs|против)\s+(.+?)(?:[\.\?\!,;]|$)",
    re.IGNORECASE,
)


def _detect_entity_type(text_low: str) -> EntityType | None:
    for needle, etype in _ENTITY_TYPE_HINTS.items():
        if needle in text_low:
            return etype
    return None


def _strip_question_tail(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^(пожалуйста|пож-та|please)[,\s]+", "", text, flags=re.IGNORECASE)
    return text.strip()


def _extract_quoted(text: str) -> list[str]:
    return [m.group(1).strip() for m in _QUOTED_RE.finditer(text) if m.group(1).strip()]


def _heuristic_intent(query: str) -> dict[str, Any]:
    """Быстрая регэксп-эвристика без LLM."""
    raw = _strip_question_tail(query)
    low = raw.lower()

    # Compare имеет приоритет, потому что его триггеры явные
    pair_match = _COMPARE_PAIR_RE.search(low)
    if pair_match or any(t in low for t in _COMPARE_TRIGGERS):
        quoted = _extract_quoted(raw)
        entities: list[str] = list(quoted)
        if pair_match and len(entities) < 2:
            a = raw[pair_match.start(1) : pair_match.end(1)].strip(" .,;:!?\"'«»")
            b = raw[pair_match.start(2) : pair_match.end(2)].strip(" .,;:!?\"'«»")
            entities = [e for e in [a, b] if e]
        etype = _detect_entity_type(low) or "clinic"
        if len(quoted) >= 2:
            confidence = 0.9
        elif pair_match:
            confidence = 0.7
        else:
            confidence = 0.55
        return {
            "intent": "reputation_compare",
            "entity_type": etype,
            "entities": entities,
            "confidence": confidence,
            "rationale": "compare_trigger",
        }

    if any(t in low for t in _ANALYZE_TRIGGERS):
        entities = _extract_quoted(raw)
        if not entities:
            m = _AFTER_KEY_RE.search(raw)
            if m:
                entities = [m.group(1).strip(" .,;:!?\"'«»")]
        etype = _detect_entity_type(low) or "clinic"
        return {
            "intent": "reputation_analyze",
            "entity_type": etype,
            "entities": entities,
            "confidence": 0.75 if entities else 0.45,
            "rationale": "analyze_trigger",
        }

    if any(t in low for t in _SEARCH_TRIGGERS):
        entities = _extract_quoted(raw) or []
        if not entities:
            m = re.search(r"(?:про|о)\s+(.+?)(?:[\.\?\!,;]|$)", raw, flags=re.IGNORECASE)
            if m:
                entities = [m.group(1).strip(" .,;:!?\"'«»")]
        etype = _detect_entity_type(low)
        return {
            "intent": "rag_search",
            "entity_type": etype,
            "entities": entities,
            "confidence": 0.5,
            "rationale": "search_trigger",
        }

    return {
        "intent": "fallback",
        "entity_type": _detect_entity_type(low),
        "entities": _extract_quoted(raw),
        "confidence": 0.2,
        "rationale": "no_trigger",
    }


_LLM_SYSTEM = (
    "Ты классификатор запросов пользователя в системе аналитики отзывов медицинских клиник DocDoc. "
    "Определи intent (reputation_analyze, reputation_compare, rag_search, fallback), "
    "entity_type (clinic, service, category, doctor) и список entities (имя клиники / услуги / направления / ФИО врача). "
    "reputation_analyze — если пользователь просит разбор/обзор отзывов по одному объекту. "
    "reputation_compare — если просит сравнение двух или более объектов. "
    "rag_search — если просит найти/показать отзывы или чанки. "
    "fallback — если не подходит ни одно. "
    "Ответ — только JSON без markdown по схеме: "
    '{"intent": "...", "entity_type": "...", "entities": ["..."], "confidence": 0..1, "rationale": "..."}.'
)


def _llm_intent(query: str, *, chat_completion_fn: Any | None = None) -> dict[str, Any] | None:
    chat_fn = chat_completion_fn or chat_completion
    try:
        raw = chat_fn(
            system_prompt=_LLM_SYSTEM,
            user_prompt=f"Запрос пользователя: {query}",
            temperature=0.0,
            max_tokens=300,
        )
    except Exception as exc:
        log.debug("chat_router LLM failed: %s", exc)
        return None
    parsed = parse_json_response(raw)
    if not isinstance(parsed, dict):
        return None
    intent = (parsed.get("intent") or "").strip()
    if intent not in {"reputation_analyze", "reputation_compare", "rag_search", "fallback"}:
        return None
    etype = (parsed.get("entity_type") or "").strip().lower() or None
    if etype not in {"clinic", "service", "category", "doctor", None}:
        etype = None
    entities = parsed.get("entities") or []
    if not isinstance(entities, list):
        entities = [str(entities)]
    entities = [str(e).strip() for e in entities if str(e).strip()]
    try:
        confidence = float(parsed.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "intent": intent,
        "entity_type": etype,
        "entities": entities,
        "confidence": max(0.0, min(1.0, confidence)),
        "rationale": str(parsed.get("rationale") or "llm")[:200],
    }


def _is_continuation(text: str) -> bool:
    low = text.lower()
    return any(t in low for t in _CONTINUATION_TRIGGERS)


def _apply_session_context(
    intent: dict[str, Any],
    *,
    session: ChatSession | None,
    query: str,
) -> dict[str, Any]:
    """Если в новом запросе нет нужных полей — наследуем из последней реплики."""
    if session is None:
        return intent
    out = dict(intent)
    is_cont = _is_continuation(query)
    inherited: list[str] = []

    if not out.get("entity_type") and session.last_entity_type:
        out["entity_type"] = session.last_entity_type
        inherited.append("entity_type")

    if not out.get("entities") and session.last_entities and not is_cont:
        # Наследуем сущности только если это явно «расскажи дальше про то же»,
        # т.е. fallback без новых сущностей. Если пользователь назвал новую сущность —
        # её уже извлечёт эвристика/LLM.
        if out["intent"] in {"fallback"}:
            out["entities"] = list(session.last_entities)
            inherited.append("entities_from_fallback")

    if out["intent"] == "fallback" and session.last_intent and is_cont:
        out["intent"] = session.last_intent
        out["confidence"] = max(out.get("confidence") or 0.0, 0.7)
        inherited.append("intent_from_continuation")

    # Continuation + есть новые сущности, но нет intent → берём last_intent
    if is_cont and out.get("entities") and out["intent"] in {"fallback", "rag_search"} and session.last_intent:
        out["intent"] = session.last_intent
        out["confidence"] = max(out.get("confidence") or 0.0, 0.75)
        inherited.append("intent_from_continuation_with_entities")

    # late entity extraction для continuation: если intent восстановили,
    # но эвристика не нашла сущности — попробуем кавычки и регекс «по клинике X»
    if (
        out["intent"] in {"reputation_analyze", "reputation_compare", "rag_search"}
        and not out.get("entities")
    ):
        extra = _extract_quoted(query)
        if not extra:
            m = _AFTER_KEY_RE.search(query)
            if m:
                extra = [m.group(1).strip(" .,;:!?\"'«»")]
        if extra:
            out["entities"] = extra
            inherited.append("entities_late_extract")

    if inherited:
        rationale = out.get("rationale") or ""
        out["rationale"] = (
            f"{rationale}; inherited={','.join(inherited)}".lstrip("; ").strip()
        )
    return out


def detect_intent(
    query: str,
    *,
    use_llm: bool = True,
    chat_completion_fn: Any | None = None,
    session: ChatSession | None = None,
) -> dict[str, Any]:
    """Сначала эвристика, потом — LLM-уточнитель, если уверенность низкая.
    Если передана session — добавляет контекст предыдущих реплик.
    """
    h = _heuristic_intent(query)
    base = h
    if use_llm and (h["confidence"] < 0.8 or not h["entities"]):
        llm = _llm_intent(query, chat_completion_fn=chat_completion_fn)
        if llm:
            merged = {**h, **llm}
            if not merged.get("entities"):
                merged["entities"] = h.get("entities") or []
            if not merged.get("entity_type"):
                merged["entity_type"] = h.get("entity_type")
            merged["confidence"] = max(h["confidence"], llm["confidence"])
            base = merged
    return _apply_session_context(base, session=session, query=query)


def _format_reputation_answer(rep: dict[str, Any]) -> str:
    if not rep.get("ok"):
        head = f"Не получилось разобрать объект: {rep.get('error') or 'unknown'}."
        hint = (rep.get("hint") or "").strip()
        suggestions = rep.get("suggestions") or []
        body = " ".join(p for p in [head, hint] if p).strip()
        if suggestions:
            body += "\n\nПохожие в данных: " + ", ".join(f"«{s}»" for s in suggestions[:5])
        return body
    report = rep.get("report") or {}
    parts: list[str] = []
    name = rep.get("entity_name") or rep.get("entity_id") or ""
    metrics = rep.get("metrics") or {}
    parts.append(
        f"**{name}** — отзывов: {metrics.get('reviews_count', 0)}, "
        f"avg: {metrics.get('avg_rating')}, "
        f"негатив: {metrics.get('negative_share_pct')}%, "
        f"без ответа: {metrics.get('unanswered_share_pct')}%"
    )
    if report.get("executive_summary"):
        parts.append(report["executive_summary"])
    if report.get("what_patients_value"):
        parts.append("**Что ценят:** " + "; ".join(report["what_patients_value"]))
    if report.get("top_complaints"):
        parts.append("**Жалобы:** " + "; ".join(report["top_complaints"]))
    if report.get("landing_page_gaps"):
        parts.append("**На страницу добавить:** " + "; ".join(report["landing_page_gaps"]))
    if report.get("ad_angle"):
        parts.append("**Угол для рекламы:** " + report["ad_angle"])
    return "\n\n".join(p for p in parts if p)


def _format_compare_answer(cmp: dict[str, Any]) -> str:
    if not cmp.get("ok"):
        head = f"Не получилось сравнить: {cmp.get('error') or 'unknown'}."
        hint = (cmp.get("hint") or "").strip()
        body = " ".join(p for p in [head, hint] if p).strip()
        suggestions = cmp.get("suggestions") or {}
        if isinstance(suggestions, dict) and suggestions:
            chunks: list[str] = []
            for v, cands in list(suggestions.items())[:3]:
                if cands:
                    chunks.append(f"«{v}» → " + ", ".join(f"«{c}»" for c in cands[:3]))
            if chunks:
                body += "\n\nПохожие в данных: " + "; ".join(chunks)
        return body
    cb = cmp.get("compare") or {}
    parts: list[str] = []
    if cb.get("summary"):
        parts.append(cb["summary"])
    for pe in cb.get("per_entity") or []:
        block = [f"**{pe.get('entity_name') or pe.get('entity_id')}**"]
        if pe.get("strengths"):
            block.append("сильные: " + "; ".join(pe["strengths"]))
        if pe.get("weaknesses"):
            block.append("слабые: " + "; ".join(pe["weaknesses"]))
        parts.append("\n".join(block))
    if cb.get("shared_complaints"):
        parts.append("**Общие жалобы:** " + "; ".join(cb["shared_complaints"]))
    if cb.get("ad_angle"):
        parts.append("**Кому какой угол:** " + cb["ad_angle"])
    return "\n\n".join(p for p in parts if p)


_HELP_HINT = (
    "Я умею разбирать отзывы DocDoc. Попробуйте:\n"
    '- «Проанализируй отзывы по клинике "Союз"»\n'
    '- «Сравни клиники "Союз" и "Авиценна" по услуге "Тонзиллор"»\n'
    "- «Что говорят про детского ЛОРа?»\n"
    "Список доступных клиник/услуг — на странице /reputation (подсказки рядом с полем ввода)."
)


def _format_rag_answer(rag: dict[str, Any], query: str) -> str:
    if not rag.get("ok"):
        err = rag.get("error") or ""
        return (
            f"RAG-поиск временно недоступен{(': ' + err) if err else ''}. "
            "Запустите POST /docdoc/rag/build на /dashboard, либо переформулируйте запрос."
        ).strip()
    items = rag.get("items") or []
    if not items:
        return (
            f"По запросу «{query}» в RAG-индексе ничего не нашлось.\n\n" + _HELP_HINT
        )
    head = f"Нашёл {len(items)} релевантных фрагментов в RAG-индексе DocDoc:"
    bullets = []
    for it in items[:5]:
        title = it.get("title") or it.get("chunk_id")
        snip = (it.get("snippet") or "").strip().replace("\n", " ")
        if len(snip) > 240:
            snip = snip[:240] + "…"
        bullets.append(f"- **{title}** — {snip}")
    return head + "\n" + "\n".join(bullets)


def run_chat(
    query: str,
    *,
    city_slug: str | None = None,
    source_id: int | None = None,
    crawl_path: str | None = None,
    use_llm: bool = True,
    use_rag: bool = True,
    intent_override: str | None = None,
    session_id: str | None = None,
    session_store: SessionStore | None = None,
    detect_intent_fn: Any | None = None,
    analyze_fn: Any | None = None,
    compare_fn: Any | None = None,
    rag_search_fn: Any | None = None,
) -> dict[str, Any]:
    """Главный обработчик чата.

    Возвращает структуру с intent, развёрнутым ответом и raw-данными вызванного эндпоинта.
    Если передан session_id (или session_store), используется multi-turn контекст —
    последние сообщения сохраняются, и фразы «а теперь по X» переиспользуют intent.
    """
    store = session_store if session_store is not None else default_store
    session = store.get_or_create(session_id) if (session_id or session_store is not None) else None
    if session and not city_slug and session.last_city_slug:
        city_slug = session.last_city_slug

    detector = detect_intent_fn or detect_intent
    intent_info = detector(query, use_llm=use_llm, session=session)
    if intent_override and intent_override in {
        "reputation_analyze",
        "reputation_compare",
        "rag_search",
        "fallback",
    }:
        intent_info = {**intent_info, "intent": intent_override, "rationale": "override"}

    intent = intent_info["intent"]
    entities: list[str] = intent_info.get("entities") or []
    entity_type = intent_info.get("entity_type") or "clinic"

    out: dict[str, Any] = {"ok": True, "intent": intent_info, "answer": ""}

    def _commit_session(answer: str) -> None:
        if session is not None:
            store.append_turn(
                session,
                user_query=query,
                bot_answer=answer,
                intent=intent,
                entity_type=intent_info.get("entity_type"),
                entities=entities,
                city_slug=city_slug,
            )
            out["session"] = session.to_dict()

    if intent == "reputation_analyze":
        if not entities:
            answer = (
                "Я понял, что вы просите разбор, но не нашёл, по какой клинике/услуге/врачу. "
                'Уточните — например, в кавычках: «Проанализируй отзывы по услуге "Тонзиллор"».'
            )
            out["answer"] = answer
            out["hint"] = "no_entity"
            _commit_session(answer)
            return out
        from app.services.docdoc_reputation import analyze_entity_reputation

        fn = analyze_fn or analyze_entity_reputation
        rep = fn(
            entity_type=entity_type,
            entity=entities[0],
            source_id=source_id,
            city_slug=city_slug,
            crawl_path=crawl_path,
            use_rag=use_rag,
            use_llm=use_llm,
        )
        out["reputation"] = rep
        out["answer"] = _format_reputation_answer(rep)
        _commit_session(out["answer"])
        return out

    if intent == "reputation_compare":
        if len(entities) < 2:
            answer = (
                "Для сравнения нужно две сущности. Уточните, что с чем сравнить — например, "
                '«Сравни клинику "Союз" и "Авиценна"».'
            )
            out["answer"] = answer
            out["hint"] = "need_two_entities"
            _commit_session(answer)
            return out
        from app.services.docdoc_reputation import compare_entities

        fn = compare_fn or compare_entities
        # Если в session был mixed-режим (или явные dict-сущности) — пробрасываем как есть.
        cmp = fn(
            entity_type=entity_type,
            entities=entities,
            source_id=source_id,
            city_slug=city_slug,
            crawl_path=crawl_path,
            use_rag=use_rag,
            use_llm=use_llm,
        )
        out["compare"] = cmp
        out["answer"] = _format_compare_answer(cmp)
        _commit_session(out["answer"])
        return out

    # fallback / rag_search → пытаемся RAG, и если он совсем пуст, отдаём
    # дружественную подсказку с примерами вопросов.
    if rag_search_fn is None:
        try:
            from app.services.docdoc_rag import search_docdoc_rag as rag_search_fn  # type: ignore
        except Exception as exc:
            log.debug("docdoc_rag import failed: %s", exc)
            rag_search_fn = None

    if rag_search_fn is None:
        answer = (
            "RAG-индекс пока не построен. Запустите его на странице /dashboard "
            "(кнопка «Построить RAG-индекс») и повторите запрос.\n\n" + _HELP_HINT
        )
        out["answer"] = answer
        out["hint"] = "rag_unavailable"
        _commit_session(answer)
        return out

    try:
        rag_result = rag_search_fn(
            query,
            top_k=8,
            city_slug=city_slug,
            source_id=source_id,
        )
    except Exception as exc:
        log.warning("rag search failed: %s", exc)
        rag_result = {"ok": False, "error": str(exc)}
    out["rag"] = rag_result
    answer = _format_rag_answer(rag_result, query)
    if intent == "fallback" and not (rag_result.get("items") or []):
        answer = (
            "Я не понял, по чему делать разбор. " + _HELP_HINT
        )
    out["answer"] = answer
    _commit_session(out["answer"])
    return out


__all__ = [
    "Intent",
    "EntityType",
    "detect_intent",
    "run_chat",
]
