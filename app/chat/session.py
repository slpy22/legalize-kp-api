from __future__ import annotations

import uuid

_sessions: dict[str, list[dict]] = {}


def get_or_create(session_id: str | None) -> tuple[str, list[dict]]:
    """Return (session_id, messages). Creates a new session if needed."""
    if session_id and session_id in _sessions:
        return session_id, _sessions[session_id]
    new_id = session_id or str(uuid.uuid4())
    _sessions[new_id] = []
    return new_id, _sessions[new_id]


def add_message(session_id: str, role: str, content: str) -> None:
    """Append a message to the session history."""
    if session_id not in _sessions:
        _sessions[session_id] = []
    _sessions[session_id].append({"role": role, "content": content})
