import json
import os
from pathlib import Path
import subprocess
import types

import pytest

import daemon
import hid_protocol
import pad_link
from session_banks import SOURCES, SessionBanks, SlotAddress


def test_banks_have_independent_stable_positions_and_pages():
    slots = SessionBanks(2)
    assert slots.allocate("c1", "claude-code") == SlotAddress("claude-code", 0)
    slots.allocate("c2", "claude-code")
    slots.allocate("c3", "claude-code")
    assert slots.allocate("x1", "codex") == SlotAddress("codex", 0)
    assert slots.slot_to_session == ["c1", "c2"]
    slots.next_page()
    assert slots.slot_to_session == ["c3", None]
    slots.select_bank(1)
    assert slots.slot_to_session == ["x1", None]
    slots.select_bank(0)
    assert slots.slot_to_session == ["c3", None]
    slots.free("c1")
    assert slots.slot_for("c2") == SlotAddress("claude-code", 1)
    assert slots.allocate("c4", "claude-code") == SlotAddress("claude-code", 0)


def test_free_last_page_clamps_view_and_eviction_targets_visible_session():
    slots = SessionBanks(2)
    for i in range(3):
        slots.allocate(str(i), "agent-shell")
    slots.select_bank(2)
    slots.next_page()
    assert slots.evict(0) == "2"
    assert slots.view == ("agent-shell", 0)
    assert slots.slot_to_session == ["0", "1"]
    assert slots.page_count() == 1


def test_unknown_bank_source_is_not_mislabeled():
    slots = SessionBanks(10)
    assert slots.allocate("other", "unknown") is None
    assert not slots.session_to_slot
    with pytest.raises(ValueError):
        slots.select_bank(-1)


def test_hidden_slots_have_no_physical_index():
    slots = SessionBanks(2)
    a = slots.allocate("a", "claude-code")
    b = slots.allocate("b", "codex")
    assert slots.physical_index(a) == 0
    assert slots.physical_index(b) is None
    slots.select_bank(1)
    assert slots.physical_index(a) is None
    assert slots.physical_index(b) == 0


@pytest.fixture
def banked_daemon(recording_daemon, monkeypatch):
    d, sent = recording_daemon
    d.apply_handshake({"slots": 10, "capabilities": hid_protocol.CAP_SESSION_BANKS})
    monkeypatch.setattr(d, "_spawn_dispatch_cache_warmup", lambda *args: None)
    return d, sent


def hook(d, session, agent, event="SessionStart", **fields):
    if agent == "claude-code":
        fields = {"tmux_socket": "/tmp/test-server", "tmux_pane": "%1", **fields}
    d.handle_hook_event({"session_id": session, "agent": agent,
                         "hook_event_name": event, "cwd": "/work/project", **fields})


def test_hidden_bank_state_is_retained_and_macro_summarizes_all_pages(banked_daemon):
    d, sent = banked_daemon
    for i in range(11):
        hook(d, f"x{i}", "codex")
    sent.clear()
    hook(d, "x10", "codex", "PermissionRequest")
    assert not any(m["t"] == "slot" for m in sent)
    assert sent[-1]["attention"] == [0, 2, 0]
    d.on_device_event({"t": "select_bank", "bank": 1})
    assert sent[-11]["pages"] == 2
    d.on_device_event({"t": "next_page"})
    assert sent[-10] == {"t": "slot", "i": 0, "state": "question", "label": "project"}
    assert all(m["t"] == "clear" for m in sent[-9:])


def test_empty_bank_stays_dark_after_switch_and_reconnect(banked_daemon):
    d, sent = banked_daemon
    for i in range(10):
        hook(d, f"claude{i}", "claude-code")
    d.on_device_event({"t": "select_bank", "bank": 1})
    assert sent[-10:] == [{"t": "clear", "i": i} for i in range(10)]
    sent.clear()
    d.resync_pad()
    assert sent[-10:] == [{"t": "clear", "i": i} for i in range(10)]


def test_ended_session_goes_dark_without_affecting_other_sessions(banked_daemon):
    d, sent = banked_daemon
    hook(d, "first", "claude-code")
    hook(d, "second", "claude-code")
    hook(d, "first", "claude-code", "SessionEnd")
    sent.clear()
    d.resync_pad()
    assert sent[1] == {"t": "clear", "i": 0}
    assert sent[2] == {"t": "slot", "i": 1, "state": "idle", "label": "project"}
    assert sent[3:] == [{"t": "clear", "i": i} for i in range(2, 10)]


def test_late_wireless_hello_preserves_sessions_and_clears_unbound_digits(recording_daemon, monkeypatch):
    d, sent = recording_daemon
    monkeypatch.setattr(d, "_spawn_dispatch_cache_warmup", lambda *args: None)
    hook(d, "claude", "claude-code", "PreToolUse", tool_name="Read")
    hook(d, "codex", "codex", "PermissionRequest")
    d.on_device_event({"t": "hello", "slots": 10, "protocol": 1, "capabilities": 3})
    assert d.slots.slot_for("claude") == SlotAddress("claude-code", 0)
    assert d.slots.slot_for("codex") == SlotAddress("codex", 0)
    assert d.pending_calls["claude"]["slot"] == SlotAddress("claude-code", 0)
    assert d._permission_tokens
    assert sent[-10]["state"] == "tool_running"
    assert sent[-9:] == [{"t": "clear", "i": i} for i in range(1, 10)]
    d.on_device_event({"t": "select_bank", "bank": 1})
    assert sent[-10]["state"] == "question"
    assert sent[-9:] == [{"t": "clear", "i": i} for i in range(1, 10)]


def test_hook_exact_tmux_target_is_updated_and_retired(recording_daemon, monkeypatch):
    d, _ = recording_daemon
    monkeypatch.setattr(d, "_spawn_dispatch_cache_warmup", lambda *args: None)
    hook(d, "s", "codex", tmux_socket="/tmp/first", tmux_pane="%1")
    assert d.session_tmux["s"] == daemon.TmuxTarget("/tmp/first", "%1")
    hook(d, "s", "codex", "UserPromptSubmit", tmux_socket="/tmp/second", tmux_pane="%2")
    assert d.session_tmux["s"] == daemon.TmuxTarget("/tmp/second", "%2")
    hook(d, "s", "codex", "UserPromptSubmit", tmux_socket="", tmux_pane="")
    assert "s" not in d.session_tmux
    hook(d, "s", "codex", "SessionEnd")
    assert "s" not in d.session_tmux


def test_digit_focus_and_hold_use_current_bank_page(banked_daemon, monkeypatch):
    d, sent = banked_daemon
    for i in range(11):
        hook(d, f"x{i}", "codex")
    hook(d, "claude", "claude-code")
    d.on_device_event({"t": "select_bank", "bank": 1})
    d.on_device_event({"t": "next_page"})
    focused = []
    monkeypatch.setattr(d, "dispatch_bring_to_front", lambda slot, session: focused.append(session))
    d.on_device_event({"t": "key", "i": 0})
    assert focused == ["x10"]
    assert d.selected_session == "x10"
    d.on_device_event({"t": "key_held", "i": 0})
    assert d.slots.slot_for("x10") is None
    assert d.slots.slot_for("claude") is not None
    assert d.selected_session is None
    assert d.slots.view == ("codex", 0)


@pytest.mark.parametrize("action", [{"t": "select_bank", "bank": 2}, {"t": "next_page"}])
def test_view_changes_clear_approval_selection(banked_daemon, action):
    d, _ = banked_daemon
    d.selected_session = "previous"
    d.on_device_event(action)
    assert d.selected_session is None


def test_permission_response_targets_selected_request_once(banked_daemon, monkeypatch):
    d, _ = banked_daemon
    hook(d, "emacs", "agent-shell", "PermissionRequest", emacs_session="agent-shell:abc",
         permission_count=1, permission_request_id=42)
    d.selected_session = "emacs"
    commands = []
    monkeypatch.setattr(daemon.subprocess, "run", lambda command, **kwargs:
                        commands.append(command) or types.SimpleNamespace(returncode=0, stdout="t\n"))
    assert d._respond_selected_permission(True)
    assert commands == [["emacsclient", "--eval",
                         '(agent-shell-macropad-respond "agent-shell:abc" 42 "allow")']]
    assert not d._respond_selected_permission(True)
    assert len(commands) == 1


def test_cli_permission_or_multiple_emacs_requests_cannot_be_approved(banked_daemon, monkeypatch):
    d, _ = banked_daemon
    hook(d, "cli", "codex", "PermissionRequest")
    d.selected_session = "cli"
    assert not d._respond_selected_permission(True)
    hook(d, "emacs", "agent-shell", "PermissionRequest", emacs_session="agent-shell:abc",
         permission_count=2, permission_request_id=42)
    d.selected_session = "emacs"
    assert not d._respond_selected_permission(False)


def test_exact_emacs_focus_has_no_project_window_fallback(recording_daemon, monkeypatch):
    d, _ = recording_daemon
    d.session_emacs["s"] = "agent-shell:abc"
    d.session_projects["s"] = "shared-project"
    calls = []
    monkeypatch.setattr(d, "_dispatch_emacs", lambda slot, target: calls.append(target) or False)
    monkeypatch.setattr(d, "_dispatch_vscode", lambda *args: pytest.fail("must not focus a sibling window"))
    d.dispatch_bring_to_front(0, "s")
    assert calls == ["agent-shell:abc"]


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_native_hooks_skip_agent_shell_child_processes(agent, tmp_path):
    nc = tmp_path / "nc"
    marker = tmp_path / "forwarded"
    nc.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    nc.chmod(0o700)
    script = Path(__file__).resolve().parents[1] / agent / "hook.sh"
    result = subprocess.run(["bash", str(script)], input='{"hook_event_name":"SessionStart"}',
                            text=True, capture_output=True,
                            env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
                                 "AI_AGENT_MACROPAD_SOURCE": "agent-shell"})
    assert result.returncode == 0
    assert not marker.exists()


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_native_hooks_report_tmux_server_and_pane_together(agent, tmp_path):
    nc = tmp_path / "nc"
    marker = tmp_path / "forwarded"
    nc.write_text('#!/bin/sh\ncat > "$MACROPAD_TEST_CAPTURE"\n')
    nc.chmod(0o700)
    script = Path(__file__).resolve().parents[1] / agent / "hook.sh"
    result = subprocess.run(
        ["bash", str(script)],
        input='{"hook_event_name":"UserPromptSubmit","session_id":"s"}',
        text=True, capture_output=True,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
             "AI_AGENT_MACROPAD_SOURCE": "", "MACROPAD_TEST_CAPTURE": str(marker),
             "TMUX": "/tmp/named-server,123,0", "TMUX_PANE": "%7"})
    assert result.returncode == 0
    payload = json.loads(marker.read_text())
    assert payload["tmux_pane"] == "%7"
    assert payload["tmux_socket"] == "/tmp/named-server"


def test_bank_control_wire_messages_and_capabilities():
    hello = bytes([hid_protocol.MSG_HELLO, 0xC0, 10, 1, 1]).ljust(32, b"\0")
    assert hid_protocol.parse_report(hello)["capabilities"] == hid_protocol.CAP_SESSION_BANKS
    for raw, expected in [
        (bytes([0x24, 2]), {"t": "select_bank", "bank": 2}),
        (bytes([0x25]), {"t": "next_page"}),
        (bytes([0x27, 1]), {"t": "focus_app", "app": 1}),
        (bytes([0x28, 1, 42, 0, 0, 0]), {"t": "permission_decision", "allow": True, "token": 42}),
        (bytes([0x28, 0, 42, 0, 0, 0]), {"t": "permission_decision", "allow": False, "token": 42}),
    ]:
        assert hid_protocol.parse_report(raw) == expected
    assert hid_protocol.parse_report(bytes([0x28, 2])) is None
    report = pad_link._pack_for_hid({"t": "bank", "bank": 2, "page": 1,
                                     "pages": 3, "attention": [2, 0, 1]})
    assert report == bytes([0x26, 2, 1, 3, 2, 0, 1]).ljust(32, b"\0")


def test_queued_permission_button_cannot_answer_a_replacement_request(banked_daemon, monkeypatch):
    d, _ = banked_daemon
    d._permission_tokens = True
    hook(d, "emacs", "agent-shell", "PermissionRequest", emacs_session="agent-shell:abc",
         permission_count=1, permission_request_id=42)
    d.selected_session = "emacs"
    d._send_bank_state()
    old_token = d._approval_token
    hook(d, "emacs", "agent-shell", "PermissionRequest", emacs_session="agent-shell:abc",
         permission_count=1, permission_request_id=43)
    calls = []
    monkeypatch.setattr(d, "_respond_selected_permission", lambda allow, **kwargs: calls.append(allow))
    d.on_device_event({"t": "permission_decision", "allow": True, "token": old_token})
    assert calls == []
    d.on_device_event({"t": "permission_decision", "allow": False, "token": d._approval_token})
    assert calls == [False]
