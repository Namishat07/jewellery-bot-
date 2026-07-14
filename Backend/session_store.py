"""
session_store.py
=================
In-memory, multi-user session storage. No database — each session holds
one user's scraped site (catalog + policies) plus their chat history.

Sessions are keyed by a UUID handed to the frontend after site ingestion.
A background cleanup task drops sessions inactive past SESSION_TTL_SECONDS
so memory doesn't grow unbounded across many concurrent users.

Scraping happens as a background task, not inline in the HTTP request that
creates the session (see main.py). That means a session exists in a
"scraping" state the instant it's created, and transitions to "ready" or
"failed" once the background scrape finishes. This is what lets the initial
POST /api/session request return in milliseconds regardless of how long the
actual scrape takes -- no request needs to stay open long enough to hit a
hosting platform's proxy/gateway timeout.

Designed to be a single module-level store shared by the FastAPI app
(import `store` and use it directly — no class instantiation needed
per-request).
"""

import time
import uuid
from dataclasses import dataclass, field
from threading import Lock


SESSION_TTL_SECONDS = 60 * 60  # 1 hour of inactivity -> session dropped

# Session.status values
STATUS_SCRAPING = "scraping"
STATUS_READY = "ready"
STATUS_FAILED = "failed"


@dataclass
class Session:
    session_id: str
    site_url: str
    products: list = field(default_factory=list)
    policies: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    status: str = STATUS_SCRAPING
    error_status_code: int | None = None   # set if status == "failed"
    error_detail: str | None = None        # set if status == "failed"
    chat_history: list = field(default_factory=list)  # [{"role": "user"/"assistant", "content": str}]
    last_active: float = field(default_factory=time.time)

    def touch(self):
        self.last_active = time.time()


class SessionStore:
    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._lock = Lock()

    def create_pending(self, site_url: str) -> Session:
        """
        Create a session immediately, before scraping has even started.
        Status starts as "scraping"; call mark_ready() or mark_failed() once
        the background scrape finishes.
        """
        session_id = str(uuid.uuid4())
        session = Session(session_id=session_id, site_url=site_url)
        with self._lock:
            # Single-active-site model: starting a new scrape replaces
            # whatever was scraped (or is still being scraped) before,
            # rather than accumulating multiple catalogues in memory side by
            # side. Old data is fully dropped the moment a new session is
            # created, and memory stays bounded to one site at a time.
            #
            # Trade-off: if this app is ever used by multiple people at the
            # same time, one person starting a new scrape will wipe everyone
            # else's active session too. Fine for a single-user/demo setup;
            # remove the `.clear()` line below if concurrent multi-user
            # sessions need to coexist.
            self._sessions.clear()
            self._sessions[session_id] = session
        return session

    def mark_ready(self, session_id: str, products: list, policies: dict, meta: dict) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return  # session was cleared (e.g. a newer scrape started) -- nothing to update
            session.products = products
            session.policies = policies
            session.meta = meta
            session.status = STATUS_READY
            session.touch()

    def mark_failed(self, session_id: str, status_code: int, detail: str) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            session.status = STATUS_FAILED
            session.error_status_code = status_code
            session.error_detail = detail
            session.touch()

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
