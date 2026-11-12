import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# --- daemon.py fixtures --------------------------------------------------


@pytest.fixture
def recording_daemon(monkeypatch):
    """A Daemon() with pad.write_json replaced by a recorder, so tests
    can assert on what would've been sent to the pad without a real
    connection. The pad's open() is never called in these tests (only
    serve() calls it), so no discovery probing happens either.
    """
    import daemon as daemon_mod

    d = daemon_mod.Daemon()
    monkeypatch.setattr(d, "_spawn_dispatch_cache_warmup", lambda *args: None)
    monkeypatch.setattr(d, "_terminal_available", lambda target, tty: bool(target or tty))
    monkeypatch.setattr(daemon_mod, "discover_running_sessions", lambda: [])
    monkeypatch.setattr(daemon_mod, "discover_codex_terminals", lambda: [])
    monkeypatch.setattr(d, "_codex_terminal", lambda session_id: {
        "sessionId": session_id, "tmux_socket": "/tmp/test-server", "tmux_pane": "%1",
        "tty": "/dev/ttys001", "pid": 1, "cwd": "/work/project"})
    sent = []
    monkeypatch.setattr(d.pad, "write_json", sent.append)
    return d, sent
