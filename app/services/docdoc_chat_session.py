"""
In-memory store пользовательских сессий для DocDoc-чата.

Хранит последние сообщения и контекст (last_intent / last_entity_type / last_entities / last_city_slug),
чтобы поддерживать multi-turn диалог: «а теперь по клинике X», «то же по другой услуге».

Это in-memory (на инстанс приложения). Для прода поверх можно поставить Redis,
не меняя интерфейс `SessionStore`.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

DEFAULT_TTL_SECONDS = 60 * 60  # 1 час
DEFAULT_HISTORY_LIMIT = 10


@dataclass
class ChatTurn:
    role: str  # "user" | "bot"
    content: str
    ts: str
    intent: str | None = None
    entity_type: str | None = None
    entities: list[str] = field(default_factory=list)


@dataclass
class ChatSession:
    session_id: str
    created_at: str
    updated_at: str
    last_intent: str | None = None
    last_entity_type: str | None = None
    last_entities: list[str] = field(default_factory=list)
    last_city_slug: str | None = None
    history: list[ChatTurn] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_intent": self.last_intent,
            "last_entity_type": self.last_entity_type,
            "last_entities": list(self.last_entities),
            "last_city_slug": self.last_city_slug,
            "history": [
                {
                    "role": t.role,
                    "content": t.content,
                    "ts": t.ts,
                    "intent": t.intent,
                    "entity_type": t.entity_type,
                    "entities": list(t.entities),
                }
                for t in self.history
            ],
        }


class SessionStore:
    """Потокобезопасный in-memory store с TTL и лимитом истории."""

    def __init__(
        self,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.history_limit = history_limit
        self._lock = threading.RLock()
        self._sessions: dict[str, ChatSession] = {}

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")

    def _purge_locked(self) -> None:
        if not self._sessions:
            return
        cutoff = datetime.now(UTC) - timedelta(seconds=self.ttl_seconds)
        stale: list[str] = []
        for sid, sess in self._sessions.items():
            try:
                ts = datetime.fromisoformat(sess.updated_at.replace("Z", "+00:00"))
            except ValueError:
                stale.append(sid)
                continue
            if ts < cutoff:
                stale.append(sid)
        for sid in stale:
            self._sessions.pop(sid, None)

    def get_or_create(self, session_id: str | None) -> ChatSession:
        with self._lock:
            self._purge_locked()
            if session_id and session_id in self._sessions:
                return self._sessions[session_id]
            sid = session_id or str(uuid4())
            now = self._now()
            sess = ChatSession(session_id=sid, created_at=now, updated_at=now)
            self._sessions[sid] = sess
            return sess

    def get(self, session_id: str) -> ChatSession | None:
        with self._lock:
            self._purge_locked()
            return self._sessions.get(session_id)

    def append_turn(
        self,
        session: ChatSession,
        *,
        user_query: str,
        bot_answer: str,
        intent: str | None,
        entity_type: str | None,
        entities: list[str],
        city_slug: str | None,
    ) -> ChatSession:
        with self._lock:
            now = self._now()
            session.updated_at = now
            session.history.append(ChatTurn(role="user", content=user_query, ts=now))
            session.history.append(
                ChatTurn(
                    role="bot",
                    content=bot_answer,
                    ts=now,
                    intent=intent,
                    entity_type=entity_type,
                    entities=list(entities),
                )
            )
            if len(session.history) > self.history_limit * 2:
                session.history = session.history[-self.history_limit * 2 :]
            if intent and intent != "fallback":
                session.last_intent = intent
            if entity_type:
                session.last_entity_type = entity_type
            if entities:
                session.last_entities = list(entities)
            if city_slug:
                session.last_city_slug = city_slug
            self._sessions[session.session_id] = session
            return session

    def reset(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)


# Глобальный store на инстанс процесса.
default_store = SessionStore()


__all__ = ["ChatSession", "ChatTurn", "SessionStore", "default_store"]
