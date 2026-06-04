"""Тесты HTTP-эндпоинтов GET/DELETE /chat/session/{id}.

TestClient создаётся без `with`, чтобы не запускать lifespan
(он пытается ходить в Postgres, которого может не быть в окружении CI).
"""

from fastapi.testclient import TestClient

import app.services.docdoc_chat_session as docdoc_chat_session
from app.api.main import app

client = TestClient(app)


def _reset_store(monkeypatch):
    fresh = docdoc_chat_session.SessionStore()
    monkeypatch.setattr(docdoc_chat_session, "default_store", fresh)
    return fresh


def test_chat_session_get_404_for_unknown(monkeypatch):
    _reset_store(monkeypatch)
    resp = client.get("/chat/session/does-not-exist")
    assert resp.status_code == 404


def test_chat_session_get_returns_state(monkeypatch):
    store = _reset_store(monkeypatch)
    sess = store.get_or_create(None)
    store.append_turn(
        sess,
        user_query="Проанализируй клинику Союз",
        bot_answer="ответ-бота",
        intent="reputation_analyze",
        entity_type="clinic",
        entities=["Союз"],
        city_slug="irk",
    )
    resp = client.get(f"/chat/session/{sess.session_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == sess.session_id
    assert body["last_intent"] == "reputation_analyze"
    assert body["last_entity_type"] == "clinic"
    assert body["last_entities"] == ["Союз"]
    assert body["last_city_slug"] == "irk"
    assert len(body["history"]) == 2


def test_chat_session_delete(monkeypatch):
    store = _reset_store(monkeypatch)
    sess = store.get_or_create(None)
    store.append_turn(
        sess,
        user_query="q",
        bot_answer="a",
        intent="rag_search",
        entity_type=None,
        entities=[],
        city_slug=None,
    )
    sid = sess.session_id

    resp = client.delete(f"/chat/session/{sid}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["deleted"] is True
    assert body["session_id"] == sid

    resp2 = client.delete(f"/chat/session/{sid}")
    assert resp2.status_code == 200
    assert resp2.json()["deleted"] is False

    resp3 = client.get(f"/chat/session/{sid}")
    assert resp3.status_code == 404


def test_chat_sessions_list(monkeypatch):
    store = _reset_store(monkeypatch)
    s1 = store.get_or_create(None)
    s2 = store.get_or_create(None)
    resp = client.get("/chat/sessions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] >= 2
    assert s1.session_id in body["session_ids"]
    assert s2.session_id in body["session_ids"]
