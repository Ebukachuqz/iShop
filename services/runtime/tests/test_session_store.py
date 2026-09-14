from pathlib import Path
from uuid import uuid4
import time
from ishop.domain.session_store import SessionStore


def test_session_store_round_trip():
    path = Path(".test-state") / f"{uuid4().hex}.sqlite3"
    store = SessionStore(path)
    state = {"session_id": "s1", "latest_request_revision": 3, "conversation_history": [{"role": "user", "content": "find a snowboard"}]}
    store.save("s1", state)
    assert store.load("s1") == state
    path.unlink(missing_ok=True)


def test_session_store_expires_conversation_state():
    path = Path(".test-state") / f"{uuid4().hex}.sqlite3"
    store = SessionStore(path, ttl_ms=1)
    store.save("s-expired", {"session_id": "s-expired"})
    time.sleep(0.01)
    assert store.load("s-expired") is None
    path.unlink(missing_ok=True)
