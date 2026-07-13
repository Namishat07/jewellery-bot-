"""
session_store.py
=================
In-memory, multi-user session storage. No database — each session holds
one user's scraped site (catalog + policies) plus their chat history.

Sessions are keyed by a UUID handed to the frontend after site ingestion.
A background cleanup task drops sessions inactive past SESSION_TTL_SECONDS
so memory doesn't grow unbounded across many concurrent users.

Designed to be a single module-level store shared by the FastAPI app
(import `store` and use it directly — no class instantiation needed
per-request).
"""

import time
import uuid
from dataclasses import dataclass, field
from threading import Lock


SESSION_TTL_SECONDS = 60 * 60  # 1 hour of inactivity -> session dropped


@dataclass
class Session:
    session_id: str
    site_url: str
    products: list
    policies: dict
    meta: dict
    chat_history: list = field(default_factory=list)  # [{"role": "user"/"assistant", "content": str}]
    last_active: float = field(default_factory=time.time)

    def touch(self):
        self.last_active = time.time()


class SessionStore:
    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._lock = Lock()

    def create(self, site_url: str, products: list, policies: dict, meta: dict) -> Session:
        session_id = str(uuid.uuid4())
        session = Session(
            session_id=session_id,
            site_url=site_url,
            products=products,
            policies=policies,
            meta=meta,
        )
        with self._lock:
            self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            session = self._sessions.get(session_id)
        if session:
            session.touch()
        return session

    def append_message(self, session_id: str, role: str, content: str) -> None:
        session = self.get(session_id)
        if session:
            session.chat_history.append({"role": role, "content": content})

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def cleanup_expired(self) -> int:
        """Remove sessions inactive past SESSION_TTL_SECONDS. Returns count removed."""
        now = time.time()
        with self._lock:
            expired = [
                sid for sid, s in self._sessions.items()
                if now - s.last_active > SESSION_TTL_SECONDS
            ]
            for sid in expired:
                del self._sessions[sid]
        return len(expired)

    def active_count(self) -> int:
        with self._lock:
            return len(self._sessions)


# Module-level singleton — import this from the FastAPI app
store = SessionStore()
