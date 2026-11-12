import daemon
from session_banks import SlotAddress


def banked(recording_daemon):
    d, sent = recording_daemon
    d.apply_handshake({"slots": 10, "capabilities": 3})
    return d, sent


def event(d, session, **fields):
    d.handle_hook_event({"hook_event_name": "SessionStart", "agent": "claude-code",
                         "session_id": session, "cwd": "/work/project", **fields})


def test_seed_excludes_background_nonterminal_and_agent_shell_backends(recording_daemon, monkeypatch):
    d, sent = banked(recording_daemon)
    monkeypatch.setattr(daemon, "discover_running_sessions", lambda: [
        {"sessionId": "background", "kind": "background", "pid": 1},
        {"sessionId": "desktop", "kind": "interactive", "pid": 2},
        {"sessionId": "terminal", "kind": "interactive", "pid": 3},
        {"sessionId": "acp-backend", "kind": "interactive", "pid": 4},
    ])
    monkeypatch.setattr(daemon, "discover_running_grok_sessions", lambda: [])
    monkeypatch.setattr(daemon, "_controlling_tty", lambda pid: {1: "/dev/ttys001", 3: "/dev/ttys003"}.get(pid))
    monkeypatch.setattr(daemon, "_tmux_target_for_tty", lambda tty: daemon.TmuxTarget("/tmp/server", "%3"))
    d.seed_existing_sessions()
    assert d.slots.slot_to_session == ["terminal"] + [None] * 9
    assert d.slots.page_count() == 1
    assert sent[-10]["state"] == "idle"
    assert sent[-9:] == [{"t": "clear", "i": i} for i in range(1, 10)]


def test_nonterminal_hook_cannot_fill_a_claude_key(recording_daemon):
    d, sent = banked(recording_daemon)
    event(d, "desktop", tmux_pane="", tmux_socket="")
    assert d.slots.slot_for("desktop") is None
    assert sent == []


def test_background_hook_with_inherited_terminal_is_excluded(recording_daemon, monkeypatch):
    d, sent = banked(recording_daemon)
    monkeypatch.setattr(daemon, "discover_running_sessions", lambda: [
        {"sessionId": "background", "kind": "background"}])
    event(d, "background", tmux_pane="%3", tmux_socket="/tmp/server")
    assert d.slots.slot_for("background") is None
    assert sent == []


def test_session_can_register_when_it_later_has_a_live_terminal(recording_daemon):
    d, _ = banked(recording_daemon)
    event(d, "s")
    assert d.slots.slot_for("s") is None
    event(d, "s", tmux_pane="%3", tmux_socket="/tmp/server")
    assert d.slots.slot_for("s") == SlotAddress("claude-code", 0)


def test_direct_terminal_without_tmux_can_register(recording_daemon):
    d, _ = banked(recording_daemon)
    event(d, "s", controlling_tty="/dev/ttys005")
    assert d.slots.slot_for("s") == SlotAddress("claude-code", 0)


def test_stale_terminal_target_cannot_fill_a_claude_key(recording_daemon, monkeypatch):
    d, sent = banked(recording_daemon)
    monkeypatch.setattr(d, "_terminal_available", lambda target, tty: False)
    event(d, "s", tmux_pane="%999", tmux_socket="/tmp/stale-server")
    assert d.slots.slot_for("s") is None
    assert sent == []
