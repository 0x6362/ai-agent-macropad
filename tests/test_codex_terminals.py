import sqlite3

import pytest

import codex_terminals
import daemon
from session_banks import SlotAddress


def pane(title, socket="/tmp/live-server", pane_id="%21", tty="/dev/ttys001", command="codex"):
    return {"title": title, "socket": socket, "pane": pane_id, "tty": tty, "command": command}


@pytest.mark.parametrize("prefix", ["", "⠼ ", "[ ! ] Action Required | ", "[ . ] Action Required | "])
def test_exact_display_identity_resolves_live_terminal(prefix):
    threads = [("session", "Fix macropad", "/work/macropad")]
    matches = codex_terminals.match_terminals(
        threads, [pane(prefix + "Fix macropad | macropad")], {"/dev/ttys001": [42]})
    assert matches == [{"sessionId": "session", "cwd": "/work/macropad", "pid": 42,
                        "tty": "/dev/ttys001", "tmux_socket": "/tmp/live-server", "tmux_pane": "%21"}]


def test_long_default_title_matches_the_exact_rendered_truncation():
    name = "continue to monitor live datadog laws for new instances of failures"
    matches = codex_terminals.match_terminals(
        [("session", name, "/work/prosite")],
        [pane("continue to monitor live datadog laws for new... | prosite")],
        {"/dev/ttys001": [42]})
    assert [match["sessionId"] for match in matches] == ["session"]


def test_same_display_identity_for_two_sessions_is_not_guessed():
    assert codex_terminals.match_terminals(
        [("one", "Fix", "/work/project"), ("two", "Fix", "/other/project")],
        [pane("Fix | project")], {"/dev/ttys001": [42]}) == []


def test_same_session_in_two_terminal_clients_is_not_guessed():
    assert codex_terminals.match_terminals(
        [("one", "Fix", "/work/project")],
        [pane("Fix | project"), pane("Fix | project", pane_id="%22", tty="/dev/ttys002")],
        {"/dev/ttys001": [42], "/dev/ttys002": [43]}) == []


@pytest.mark.parametrize("processes,command,title", [
    ({}, "codex", "Fix | project"),
    ({"/dev/ttys001": [42, 43]}, "codex", "Fix | project"),
    ({"/dev/ttys001": [42]}, "zsh", "Fix | project"),
    ({"/dev/ttys001": [42]}, "codex", "Fix | different-project"),
    ({"/dev/ttys001": [42]}, "codex", "Fix something | project"),
])
def test_missing_stale_or_nonexact_terminal_identity_has_no_binding(processes, command, title):
    assert codex_terminals.match_terminals(
        [("one", "Fix", "/work/project")], [pane(title, command=command)], processes) == []


def test_discovery_reads_session_metadata_without_modifying_database(tmp_path, monkeypatch):
    database = tmp_path / "state_5.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE threads(id, name, title, cwd, archived)")
        connection.execute("INSERT INTO threads VALUES ('s', 'Fix', 'old prompt', '/work/project', 0)")
        connection.execute("INSERT INTO threads VALUES ('old', 'Fix', 'old prompt', '/work/project', 1)")
    before = database.read_bytes()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(codex_terminals, "_tmux_panes", lambda: [pane("Fix | project")])
    monkeypatch.setattr(codex_terminals, "_codex_processes", lambda: {"/dev/ttys001": [42]})
    assert [match["sessionId"] for match in codex_terminals.discover_codex_terminals()] == ["s"]
    assert database.read_bytes() == before


def banked(recording_daemon):
    d, sent = recording_daemon
    d.apply_handshake({"slots": 10, "capabilities": 3})
    return d, sent


def event(d, event_name="SessionStart"):
    d.handle_hook_event({"session_id": "s", "agent": "codex", "hook_event_name": event_name,
                         "cwd": "/work/project", "tmux_socket": "/tmp/stale-backend", "tmux_pane": "%5"})


def test_shared_backend_hook_cannot_overwrite_real_terminal_binding(recording_daemon):
    d, _ = banked(recording_daemon)
    event(d)
    event(d, "UserPromptSubmit")
    assert d.session_tmux["s"] == daemon.TmuxTarget("/tmp/test-server", "%1")
    assert d.slots.slot_for("s") == SlotAddress("codex", 0)


def test_focus_refreshes_actual_client_socket_and_pane(recording_daemon, monkeypatch):
    d, _ = banked(recording_daemon)
    event(d)
    monkeypatch.setattr(d, "_codex_terminal", lambda session: {
        "sessionId": session, "tmux_socket": "/tmp/new-server", "tmux_pane": "%24", "tty": "/dev/ttys002"})
    focused = []
    monkeypatch.setattr(d, "_dispatch_tmux_target", lambda slot, target: focused.append(target) or True)
    d.dispatch_bring_to_front(0, "s")
    assert focused == [daemon.TmuxTarget("/tmp/new-server", "%24")]


def test_backend_only_session_does_not_fill_a_numbered_key(recording_daemon, monkeypatch):
    d, sent = banked(recording_daemon)
    monkeypatch.setattr(d, "_codex_terminal", lambda session: None)
    event(d)
    assert d.slots.slot_for("s") is None
    assert sent == []


def test_startup_seeds_codex_using_the_discovered_named_socket(recording_daemon, monkeypatch):
    d, sent = banked(recording_daemon)
    binding = {"sessionId": "existing", "cwd": "/work/project", "pid": 42,
               "tty": "/dev/ttys009", "tmux_socket": "/tmp/named-server", "tmux_pane": "%24"}
    monkeypatch.setattr(daemon, "discover_codex_terminals", lambda: [binding])
    monkeypatch.setattr(d, "_codex_terminal", lambda session: binding if session == "existing" else None)
    monkeypatch.setattr(daemon, "discover_running_grok_sessions", lambda: [])
    d.seed_existing_sessions()
    assert d.slots.slot_for("existing") == SlotAddress("codex", 0)
    assert d.session_tmux["existing"] == daemon.TmuxTarget("/tmp/named-server", "%24")
    d.on_device_event({"t": "select_bank", "bank": 1})
    assert sent[-10] == {"t": "slot", "i": 0, "state": "idle", "label": "project"}


def test_missing_client_never_falls_back_to_stale_backend_target(recording_daemon, monkeypatch):
    d, _ = banked(recording_daemon)
    event(d)
    monkeypatch.setattr(d, "_codex_terminal", lambda session: None)
    assert d._dispatch_candidates("s") == []


def test_late_banked_handshake_rejects_a_cached_backend_only_route(recording_daemon, monkeypatch):
    d, _ = recording_daemon
    event(d)
    assert d.session_tmux["s"] == daemon.TmuxTarget("/tmp/stale-backend", "%5")
    monkeypatch.setattr(d, "_codex_terminal", lambda session: None)
    d.on_device_event({"t": "hello", "slots": 10, "capabilities": 3})
    assert d.slots.slot_for("s") is None
