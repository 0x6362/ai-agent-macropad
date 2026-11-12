import asyncio
import subprocess
import types

import pytest

import daemon
import session_lifecycle
from session_banks import SlotAddress
from session_lifecycle import Observation, Presence, buffer_presence, process_presence, terminal_presence


@pytest.mark.parametrize("visible,process,expected", [
    ({"s": 10}, Presence.ALIVE, Presence.ALIVE),
    ({}, Presence.ALIVE, Presence.UNKNOWN),
    ({}, Presence.UNKNOWN, Presence.UNKNOWN),
    ({}, Presence.CLOSED, Presence.CLOSED),
    ({"other": 10}, Presence.ALIVE, Presence.CLOSED),
    ({"s": 10}, Presence.CLOSED, Presence.CLOSED),
])
def test_terminal_presence_requires_positive_evidence_of_closure(visible, process, expected):
    assert terminal_presence("s", 10, visible, process) is expected


@pytest.mark.parametrize("sessions,expected", [
    (None, Presence.UNKNOWN), (frozenset(), Presence.CLOSED),
    (frozenset({"s"}), Presence.ALIVE),
])
def test_buffer_presence_distinguishes_unavailable_server_from_closed_buffer(sessions, expected):
    assert buffer_presence("s", sessions) is expected


def test_missing_process_confirms_terminal_closure(monkeypatch):
    def gone(*args):
        raise ProcessLookupError()
    monkeypatch.setattr(session_lifecycle.os, "kill", gone)
    assert process_presence(10) is Presence.CLOSED


def test_inaccessible_process_is_not_reported_closed(monkeypatch):
    def denied(*args):
        raise PermissionError()
    monkeypatch.setattr(session_lifecycle.os, "kill", denied)
    assert process_presence(10) is Presence.UNKNOWN


@pytest.mark.parametrize("output,expected", [
    ("S+ ttys001\n", Presence.ALIVE), ("Z ttys001\n", Presence.CLOSED),
    ("S ??\n", Presence.CLOSED), ("", Presence.UNKNOWN),
])
def test_process_probe_accounts_for_terminal_and_zombie_state(monkeypatch, output, expected):
    monkeypatch.setattr(session_lifecycle.os, "kill", lambda *args: None)
    monkeypatch.setattr(session_lifecycle.subprocess, "run", lambda *args, **kwargs:
                        types.SimpleNamespace(returncode=0, stdout=output))
    assert process_presence(10) is expected


def register(d, sid, agent="codex", **fields):
    d.handle_hook_event({"hook_event_name": "SessionStart", "session_id": sid,
                         "agent": agent, "cwd": "/work/project", **fields})


def banked(recording_daemon):
    d, sent = recording_daemon
    d.apply_handshake({"slots": 10, "capabilities": 3})
    d.on_device_event({"t": "select_bank", "bank": 1})
    return d, sent


def test_closed_session_releases_its_key_and_next_new_session_reuses_that_hole(recording_daemon, monkeypatch):
    d, sent = banked(recording_daemon)
    for sid in ("a", "b", "c"):
        register(d, sid)
    tracked = d._tracked_sessions()
    observations = {s.session_id: Observation(Presence.CLOSED if s.session_id == "b" else Presence.ALIVE)
                    for s in tracked}
    monkeypatch.setattr(d, "_read_session_presence", lambda sessions: observations)
    sent.clear()
    asyncio.run(d.reconcile_closed_sessions())
    assert d.slots.slot_to_session[:3] == ["a", None, "c"]
    assert {"t": "clear", "i": 1} in sent
    register(d, "new")
    assert d.slots.slot_to_session[:3] == ["a", "new", "c"]
    assert d.slots.slot_for("c") == SlotAddress("codex", 2)


def test_unknown_observation_preserves_key_and_display_state(recording_daemon):
    d, sent = banked(recording_daemon)
    register(d, "a")
    tracked = d._tracked_sessions()
    before = dict(d.pad_state)
    sent.clear()
    d._apply_session_presence(tracked, {"a": Observation(Presence.UNKNOWN)})
    assert d.slots.slot_for("a") == SlotAddress("codex", 0)
    assert d.pad_state == before
    assert sent == []


def test_closed_selected_session_clears_pending_approval_and_cached_routes(recording_daemon):
    d, _ = banked(recording_daemon)
    register(d, "s", "agent-shell", emacs_session="s", permission_count=1, permission_request_id=42)
    d.selected_session = "s"
    d.pending_calls["s"] = {"slot": d.slots.slot_for("s")}
    d._apply_session_presence(d._tracked_sessions(), {"s": Observation(Presence.CLOSED)})
    assert d.selected_session is None
    assert d.slots.slot_for("s") is None
    assert "s" not in d.session_permissions
    assert "s" not in d.pending_calls
    assert "s" not in d.session_emacs
    assert "s" not in d.session_epochs


def test_old_probe_cannot_clear_a_resumed_session_with_the_same_identity(recording_daemon):
    d, _ = banked(recording_daemon)
    register(d, "s")
    tracked = d._tracked_sessions()
    register(d, "s")
    d._apply_session_presence(tracked, {"s": Observation(Presence.CLOSED)})
    assert d.slots.slot_for("s") == SlotAddress("codex", 0)


def test_old_probe_cannot_clear_a_reused_key(recording_daemon):
    d, _ = banked(recording_daemon)
    register(d, "old")
    tracked = d._tracked_sessions()
    d._retire_session("old", "SessionEnd")
    register(d, "new")
    d._apply_session_presence(tracked, {"old": Observation(Presence.CLOSED)})
    assert d.slots.slot_to_session[0] == "new"


def test_old_probe_cannot_clear_a_session_whose_terminal_pid_changed(recording_daemon):
    d, _ = banked(recording_daemon)
    register(d, "s")
    tracked = d._tracked_sessions()
    d.session_pids["s"] = 100
    d._apply_session_presence(tracked, {"s": Observation(Presence.CLOSED)})
    assert d.slots.slot_for("s") is not None


def test_empty_last_page_disappears_without_moving_surviving_sessions(recording_daemon):
    d, sent = banked(recording_daemon)
    for i in range(11):
        register(d, str(i))
    d.on_device_event({"t": "next_page"})
    tracked = d._tracked_sessions()
    observations = {s.session_id: Observation(Presence.CLOSED if s.session_id == "10" else Presence.ALIVE)
                    for s in tracked}
    d._apply_session_presence(tracked, observations)
    assert d.slots.view == ("codex", 0)
    assert d.slots.page_count() == 1
    assert d.slots.slot_for("9") == SlotAddress("codex", 9)


def test_freed_slots_are_reused_in_ascending_order_across_pages(recording_daemon):
    d, _ = banked(recording_daemon)
    for i in range(12):
        register(d, str(i))
    d._retire_session("2", "ClosedSession")
    d._retire_session("0", "ClosedSession")
    register(d, "first")
    register(d, "second")
    assert d.slots.slot_for("first") == SlotAddress("codex", 0)
    assert d.slots.slot_for("second") == SlotAddress("codex", 2)
    assert d.slots.slot_for("11") == SlotAddress("codex", 11)


def test_live_process_with_unmatched_codex_title_is_preserved(recording_daemon, monkeypatch):
    d, _ = banked(recording_daemon)
    register(d, "s")
    monkeypatch.setattr(daemon, "discover_codex_terminals", lambda: [])
    monkeypatch.setattr(daemon, "process_presence", lambda pid: Presence.ALIVE)
    observed = d._read_session_presence(d._tracked_sessions())
    assert observed["s"].presence is Presence.UNKNOWN


def test_dead_terminal_is_closed_even_without_a_session_end_hook(recording_daemon, monkeypatch):
    d, _ = banked(recording_daemon)
    register(d, "s")
    monkeypatch.setattr(daemon, "process_presence", lambda pid: Presence.CLOSED)
    observed = d._read_session_presence(d._tracked_sessions())
    assert observed["s"].presence is Presence.CLOSED


def test_switching_same_codex_client_to_another_session_closes_old_assignment(recording_daemon, monkeypatch):
    d, _ = banked(recording_daemon)
    register(d, "s")
    monkeypatch.setattr(daemon, "process_presence", lambda pid: Presence.ALIVE)
    monkeypatch.setattr(daemon, "discover_codex_terminals", lambda: [{"sessionId": "different", "pid": 1}])
    observed = d._read_session_presence(d._tracked_sessions())
    assert observed["s"].presence is Presence.CLOSED


def test_late_backend_hook_cannot_reoccupy_a_closed_codex_key(recording_daemon, monkeypatch):
    d, _ = banked(recording_daemon)
    register(d, "old")
    d._retire_session("old", "ClosedSession")
    original = d._codex_terminal
    monkeypatch.setattr(d, "_codex_terminal", lambda sid: None if sid == "old" else original(sid))
    d.handle_hook_event({"session_id": "old", "agent": "codex", "hook_event_name": "PreToolUse"})
    register(d, "new")
    assert d.slots.slot_for("old") is None
    assert d.slots.slot_for("new") == SlotAddress("codex", 0)


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("emacsclient", 3), PermissionError()])
def test_emacs_client_failure_is_unknown_not_an_empty_buffer_list(recording_daemon, monkeypatch, error):
    d, _ = recording_daemon
    def timeout(*args, **kwargs):
        raise error
    monkeypatch.setattr(daemon.subprocess, "run", timeout)
    assert d._emacs_session_ids() is None


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("ps", 2), PermissionError()])
def test_failed_process_probe_is_unknown(monkeypatch, error):
    monkeypatch.setattr(session_lifecycle.os, "kill", lambda *args: None)
    def unavailable(*args, **kwargs):
        raise error
    monkeypatch.setattr(session_lifecycle.subprocess, "run", unavailable)
    assert process_presence(10) is Presence.UNKNOWN


def test_successful_empty_emacs_snapshot_is_distinct_from_unavailable(recording_daemon, monkeypatch):
    d, _ = recording_daemon
    monkeypatch.setattr(daemon.subprocess, "run", lambda *args, **kwargs:
                        types.SimpleNamespace(returncode=0, stdout='"[]"\n'))
    assert d._emacs_session_ids() == frozenset()
