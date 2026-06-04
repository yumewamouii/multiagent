import asyncio

import pytest

from app.services.chat_orchestrator import (
    _heuristic_route,
    detect_top_route,
    run_orchestrator,
    run_orchestrator_async,
)
from app.services.docdoc_chat_session import SessionStore


def test_heuristic_route_docdoc():
    out = _heuristic_route("Проанализируй отзывы по клинике Союз")
    assert out["system"] == "docdoc"
    assert out["confidence"] >= 0.7


def test_heuristic_route_general():
    out = _heuristic_route("Что говорят про iPhone в телеграм-канале")
    assert out["system"] == "general"
    assert out["confidence"] >= 0.5


def test_heuristic_route_intent_only_to_docdoc():
    out = _heuristic_route("Сравни эти два варианта")
    assert out["system"] == "docdoc"


def test_detect_top_route_uses_sticky_when_low_confidence():
    out = detect_top_route(
        "и ещё",
        use_llm=False,
        sticky_system="docdoc",
    )
    assert out["system"] == "docdoc"
    assert "sticky" in (out.get("rationale") or "")


def test_detect_top_route_uses_llm_when_uncertain():
    calls: list[str] = []

    def fake_chat(**kwargs):
        calls.append(kwargs.get("user_prompt", ""))
        return '{"system": "general", "confidence": 0.85, "rationale": "test"}'

    out = detect_top_route(
        "ну расскажи что-нибудь",
        use_llm=True,
        chat_completion_fn=fake_chat,
    )
    assert out["system"] == "general"
    assert calls


def test_run_orchestrator_routes_to_docdoc():
    store = SessionStore()
    captured: list[dict] = []

    def fake_docdoc(query, **kwargs):
        captured.append({"query": query, **kwargs})
        sess = kwargs["session_store"].get_or_create(kwargs.get("session_id"))
        kwargs["session_store"].append_turn(
            sess,
            user_query=query,
            bot_answer="docdoc-answer",
            intent="reputation_analyze",
            entity_type="clinic",
            entities=["Союз"],
            city_slug=None,
        )
        return {
            "ok": True,
            "intent": {"intent": "reputation_analyze", "entity_type": "clinic", "entities": ["Союз"], "confidence": 0.9, "rationale": "test"},
            "answer": "docdoc-answer",
            "session": sess.to_dict(),
        }

    def fake_general(**kwargs):
        return {"answer": "should-not-be-called"}

    out = run_orchestrator(
        "Проанализируй отзывы по клинике Союз",
        session_store=store,
        use_llm=False,
        docdoc_run_fn=fake_docdoc,
        general_orchestrate_fn=fake_general,
    )
    assert out["top_route"]["system"] == "docdoc"
    assert out["answer"] == "docdoc-answer"
    assert out["docdoc"]["intent"]["intent"] == "reputation_analyze"
    assert captured[0]["query"].startswith("Проанализируй")


def test_run_orchestrator_routes_to_general():
    store = SessionStore()

    def fake_docdoc(*a, **k):
        raise AssertionError("docdoc must not be called")

    def fake_general(*, query, top_k):
        return {
            "answer": "general-answer",
            "route": "product_lookup",
            "critic": {"confidence": 0.5, "notes": "ok"},
            "evidence": [],
        }

    out = run_orchestrator(
        "Что говорят про iPhone в телеграм",
        session_store=store,
        use_llm=False,
        docdoc_run_fn=fake_docdoc,
        general_orchestrate_fn=fake_general,
    )
    assert out["top_route"]["system"] == "general"
    assert out["answer"] == "general-answer"
    # general маршрутизация фиксируется как last_intent="general" — это не docdoc-intent,
    # поэтому sticky в DocDoc не триггерится в следующих репликах
    assert out["session"]["last_intent"] == "general"


def test_run_orchestrator_sticky_after_docdoc_in_session():
    """Если последняя реплика была docdoc, продолжение «и ещё» остаётся в docdoc."""
    store = SessionStore()
    sess = store.get_or_create(None)
    store.append_turn(
        sess,
        user_query="Проанализируй клинику Союз",
        bot_answer="ответ",
        intent="reputation_analyze",
        entity_type="clinic",
        entities=["Союз"],
        city_slug=None,
    )
    sid = sess.session_id

    fake_docdoc_calls: list[str] = []

    def fake_docdoc(query, **kwargs):
        fake_docdoc_calls.append(query)
        return {
            "ok": True,
            "intent": {"intent": "rag_search", "entity_type": "clinic", "entities": ["Союз"], "confidence": 0.5, "rationale": "x"},
            "answer": "ok",
            "session": sess.to_dict(),
        }

    out = run_orchestrator(
        "и ещё",
        session_id=sid,
        session_store=store,
        use_llm=False,
        docdoc_run_fn=fake_docdoc,
        general_orchestrate_fn=lambda **k: {"answer": "x"},
    )
    assert out["top_route"]["system"] == "docdoc"
    assert fake_docdoc_calls == ["и ещё"]


def test_run_orchestrator_system_override():
    store = SessionStore()

    def fake_docdoc(query, **kwargs):
        return {"ok": True, "intent": {"intent": "fallback", "entity_type": None, "entities": [], "confidence": 0.0, "rationale": ""}, "answer": "force-docdoc"}

    out = run_orchestrator(
        "Что говорят про iPhone",
        system_override="docdoc",
        session_store=store,
        docdoc_run_fn=fake_docdoc,
        general_orchestrate_fn=lambda **k: {"answer": "no"},
    )
    assert out["top_route"]["system"] == "docdoc"
    assert out["top_route"]["confidence"] == 1.0
    assert out["answer"] == "force-docdoc"


def test_run_orchestrator_async_general():
    """Async путь до general orchestrate."""
    store = SessionStore()

    async def fake_general_async(*, query, top_k):
        await asyncio.sleep(0)
        return {"answer": "general-async", "route": "product_lookup", "critic": {"confidence": 0.5, "notes": ""}, "evidence": []}

    # Подменим импорт на лету
    import app.agents.hierarchy as hierarchy

    saved = hierarchy.orchestrate
    hierarchy.orchestrate = fake_general_async
    try:
        result = asyncio.run(
            run_orchestrator_async(
                "Что говорят про iPhone",
                session_store=store,
                use_llm=False,
            )
        )
    finally:
        hierarchy.orchestrate = saved
    assert result["top_route"]["system"] == "general"
    assert result["answer"] == "general-async"
