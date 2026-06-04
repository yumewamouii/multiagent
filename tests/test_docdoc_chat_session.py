import time

from app.services.docdoc_chat_router import (
    _apply_session_context,
    _is_continuation,
    detect_intent,
    run_chat,
)
from app.services.docdoc_chat_session import SessionStore


def test_session_store_create_and_append():
    store = SessionStore()
    sess = store.get_or_create(None)
    assert sess.session_id
    store.append_turn(
        sess,
        user_query="Проанализируй клинику Союз",
        bot_answer="ответ",
        intent="reputation_analyze",
        entity_type="clinic",
        entities=["Союз"],
        city_slug="irk",
    )
    again = store.get(sess.session_id)
    assert again is not None
    assert again.last_intent == "reputation_analyze"
    assert again.last_entity_type == "clinic"
    assert again.last_entities == ["Союз"]
    assert again.last_city_slug == "irk"
    assert len(again.history) == 2


def test_session_store_history_limit():
    store = SessionStore(history_limit=2)
    sess = store.get_or_create(None)
    for i in range(5):
        store.append_turn(
            sess,
            user_query=f"q{i}",
            bot_answer=f"a{i}",
            intent="rag_search",
            entity_type=None,
            entities=[],
            city_slug=None,
        )
    # 2 user + 2 bot = 4 турна
    assert len(sess.history) == 4
    assert sess.history[0].content == "q3"
    assert sess.history[-1].content == "a4"


def test_session_store_ttl_purge():
    store = SessionStore(ttl_seconds=0)
    sess = store.get_or_create(None)
    sid = sess.session_id
    time.sleep(0.01)
    # любой следующий доступ должен очистить просроченную сессию
    fresh = store.get(sid)
    assert fresh is None


def test_is_continuation_triggers():
    assert _is_continuation("А теперь по клинике Авиценна")
    assert _is_continuation("Тогда по Союзу")
    assert _is_continuation("То же по другой услуге")
    assert _is_continuation("И ещё одну")
    assert not _is_continuation("Сравни две клиники")


def test_apply_session_context_inherits_intent_and_entity_type():
    from app.services.docdoc_chat_session import ChatSession

    sess = ChatSession(
        session_id="s1",
        created_at="2025-01-01T00:00:00Z",
        updated_at="2025-01-01T00:00:00Z",
        last_intent="reputation_analyze",
        last_entity_type="clinic",
        last_entities=["Союз"],
    )
    base = {
        "intent": "fallback",
        "entity_type": None,
        "entities": ["Авиценна"],
        "confidence": 0.3,
        "rationale": "no_trigger",
    }
    out = _apply_session_context(base, session=sess, query="а теперь по клинике Авиценна")
    assert out["intent"] == "reputation_analyze"
    assert out["entity_type"] == "clinic"
    assert out["entities"] == ["Авиценна"]
    assert "inherited" in (out["rationale"] or "")


def test_detect_intent_uses_session_for_continuation():
    from app.services.docdoc_chat_session import ChatSession

    sess = ChatSession(
        session_id="s1",
        created_at="2025-01-01T00:00:00Z",
        updated_at="2025-01-01T00:00:00Z",
        last_intent="reputation_analyze",
        last_entity_type="service",
        last_entities=["Тонзиллор"],
    )
    out = detect_intent(
        'А теперь то же по «Промывание лакун»',
        use_llm=False,
        session=sess,
    )
    assert out["intent"] == "reputation_analyze"
    assert out["entity_type"] == "service"
    assert "Промывание лакун" in out["entities"]


def test_run_chat_multi_turn(tmp_path):
    """Полный multi-turn: первый запрос Проанализируй, потом «а теперь по другой клинике»."""
    store = SessionStore()
    captured_calls: list[dict] = []

    def fake_analyze(**kwargs):
        captured_calls.append(kwargs)
        return {
            "ok": True,
            "entity_id": kwargs["entity"],
            "entity_name": kwargs["entity"],
            "metrics": {"reviews_count": 5, "avg_rating": 8.0, "negative_share_pct": 10.0, "unanswered_share_pct": 20.0},
            "report": {
                "executive_summary": f"Разбор {kwargs['entity']}",
                "what_patients_value": [], "top_complaints": [], "service_improvements": [],
                "landing_page_gaps": [], "ad_angle": "", "target_audience": "", "risk_topics": [],
            },
        }

    rag_search = lambda *a, **k: {"ok": True, "items": []}

    out1 = run_chat(
        'Проанализируй клинику «Союз» в Иркутске',
        analyze_fn=fake_analyze,
        compare_fn=lambda **k: {"ok": True},
        rag_search_fn=rag_search,
        use_llm=False,
        session_id=None,
        session_store=store,
    )
    sid = out1["session"]["session_id"]
    assert out1["intent"]["intent"] == "reputation_analyze"
    assert captured_calls[0]["entity"] == "Союз"

    out2 = run_chat(
        "А теперь по клинике Авиценна",
        analyze_fn=fake_analyze,
        compare_fn=lambda **k: {"ok": True},
        rag_search_fn=rag_search,
        use_llm=False,
        session_id=sid,
        session_store=store,
    )
    assert out2["session"]["session_id"] == sid
    assert out2["intent"]["intent"] == "reputation_analyze"
    assert "Авиценна" in captured_calls[1]["entity"]
    # история накопилась: 2 пары
    assert len(out2["session"]["history"]) == 4


def test_run_chat_session_persists_city_slug():
    store = SessionStore()
    captured: list[dict] = []

    def fake_analyze(**kwargs):
        captured.append(kwargs)
        return {
            "ok": True,
            "entity_id": kwargs["entity"],
            "entity_name": kwargs["entity"],
            "metrics": {"reviews_count": 1, "avg_rating": 9.0, "negative_share_pct": 0.0, "unanswered_share_pct": 0.0},
            "report": {
                "executive_summary": "ok", "what_patients_value": [], "top_complaints": [],
                "service_improvements": [], "landing_page_gaps": [], "ad_angle": "", "target_audience": "", "risk_topics": [],
            },
        }

    out1 = run_chat(
        'Проанализируй клинику «Союз»',
        city_slug="irk",
        analyze_fn=fake_analyze,
        compare_fn=lambda **k: {"ok": True},
        rag_search_fn=lambda *a, **k: {"ok": True, "items": []},
        use_llm=False,
        session_store=store,
    )
    sid = out1["session"]["session_id"]
    out2 = run_chat(
        'Проанализируй клинику «Авиценна»',
        analyze_fn=fake_analyze,
        compare_fn=lambda **k: {"ok": True},
        rag_search_fn=lambda *a, **k: {"ok": True, "items": []},
        use_llm=False,
        session_id=sid,
        session_store=store,
    )
    # city_slug унаследован из session
    assert captured[0]["city_slug"] == "irk"
    assert captured[1]["city_slug"] == "irk"
    assert out2["session"]["last_city_slug"] == "irk"
