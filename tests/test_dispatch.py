import subprocess
import types

import daemon


def fake_result(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# --- _dispatch_tmux: representative of the subprocess-driven dispatch
# helpers' shared shape (success / nonzero exit / not-found / timeout). ---


def test_dispatch_tmux_success(recording_daemon, monkeypatch):
    d, _ = recording_daemon
    monkeypatch.setattr(daemon.subprocess, "run", lambda *a, **k: fake_result(0))
    assert d._dispatch_tmux(0, "%3") is True


def test_dispatch_tmux_nonzero_exit_is_failure(recording_daemon, monkeypatch):
    d, _ = recording_daemon
    monkeypatch.setattr(
        daemon.subprocess, "run", lambda *a, **k: fake_result(1, stderr="can't find pane")
    )
    assert d._dispatch_tmux(0, "%3") is False


def test_dispatch_tmux_missing_binary_is_failure(recording_daemon, monkeypatch):
    d, _ = recording_daemon

    def raise_not_found(*a, **k):
        raise FileNotFoundError()

    monkeypatch.setattr(daemon.subprocess, "run", raise_not_found)
    assert d._dispatch_tmux(0, "%3") is False


def test_dispatch_tmux_timeout_is_failure(recording_daemon, monkeypatch):
    d, _ = recording_daemon

    def raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="tmux", timeout=2)

    monkeypatch.setattr(daemon.subprocess, "run", raise_timeout)
    assert d._dispatch_tmux(0, "%3") is False


def tmux_terminal(monkeypatch, d, panes="%3\t$1\t@2\n", clients="/dev/ttys005\t$0\n"):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if "list-panes" in command:
            return fake_result(stdout=panes)
        if "list-clients" in command:
            return fake_result(stdout=clients)
        return fake_result()

    monkeypatch.setattr(daemon.subprocess, "run", run)
    monkeypatch.setattr(daemon, "_pid_on_tty", lambda tty: 10)
    monkeypatch.setattr(daemon, "_gui_app_ancestor_pid", lambda pid: 20)
    return commands


def test_exact_tmux_focus_selects_pane_switches_client_and_raises_terminal(recording_daemon, monkeypatch):
    d, _ = recording_daemon
    commands = tmux_terminal(monkeypatch, d)
    raised = []
    monkeypatch.setattr(d, "_dispatch_generic_gui", lambda slot, tty, **kwargs: raised.append(tty) or True)
    assert d._dispatch_tmux_target(0, daemon.TmuxTarget("/tmp/named-server", "%3"))
    assert all(command[:3] == ["tmux", "-S", "/tmp/named-server"] for command in commands)
    assert commands[-1][3:] == ["select-window", "-t", "$1:@2", ";", "select-pane", "-t", "%3", ";",
                               "switch-client", "-c", "/dev/ttys005", "-t", "$1"]
    assert raised == ["/dev/ttys005"]


def test_tmux_probe_does_not_select_or_raise(recording_daemon, monkeypatch):
    d, _ = recording_daemon
    commands = tmux_terminal(monkeypatch, d)
    monkeypatch.setattr(d, "_dispatch_generic_gui", lambda slot, tty, probe_only=False: probe_only)
    assert d._dispatch_tmux_target(0, daemon.TmuxTarget("/tmp/server", "%3"), probe_only=True)
    assert len(commands) == 2


def test_missing_exact_tmux_target_never_guesses_an_editor(recording_daemon, monkeypatch):
    d, _ = recording_daemon
    commands = tmux_terminal(monkeypatch, d, panes="%4\t$0\t@1\n")
    d.session_tmux["s"] = daemon.TmuxTarget("/tmp/server", "%3")
    d.session_projects["s"] = "same-project"
    monkeypatch.setattr(d, "_dispatch_vscode", lambda *args: (_ for _ in ()).throw(AssertionError("wrong target")))
    d.dispatch_bring_to_front(0, "s")
    assert len(commands) == 1


def test_native_session_without_terminal_target_never_guesses_an_editor(recording_daemon, monkeypatch):
    d, _ = recording_daemon
    d.session_agents["s"] = "codex"
    d.session_projects["s"] = "same-project"
    assert all(not target for _, target, _ in d._dispatch_candidates("s"))


def test_ambiguous_tmux_clients_do_not_change_focus(recording_daemon, monkeypatch):
    d, _ = recording_daemon
    commands = tmux_terminal(monkeypatch, d, clients="/dev/ttys001\t$1\n/dev/ttys002\t$1\n")
    assert not d._dispatch_tmux_target(0, daemon.TmuxTarget("/tmp/server", "%3"))
    assert len(commands) == 2


def test_ghostty_activation_uses_the_owning_app_bundle(recording_daemon, monkeypatch):
    d, _ = recording_daemon
    monkeypatch.setattr(daemon, "_pid_on_tty", lambda tty: 10)
    monkeypatch.setattr(daemon, "_gui_app_ancestor_pid", lambda pid: 20)
    monkeypatch.setattr(daemon.subprocess, "run", lambda *args, **kwargs:
                        fake_result(stdout="/Applications/Ghostty.app/Contents/MacOS/ghostty\n"))
    raised = []
    monkeypatch.setattr(d, "_focus_application", lambda app: raised.append(app) or True)
    assert d._dispatch_generic_gui(0, "/dev/ttys005", probe_only=True)
    assert raised == []
    assert d._dispatch_generic_gui(0, "/dev/ttys005")
    assert raised == ["/Applications/Ghostty.app"]


# --- dispatch_bring_to_front: fallthrough order across targets ---------


def test_dispatch_bring_to_front_tries_tmux_first(recording_daemon, monkeypatch):
    d, _ = recording_daemon
    d.session_panes["s1"] = "%3"
    d.session_ttys["s1"] = "/dev/ttys003"
    d.session_projects["s1"] = "my-project"

    calls = []
    monkeypatch.setattr(d, "_dispatch_tmux", lambda *a: calls.append("tmux") or True)
    monkeypatch.setattr(d, "_dispatch_terminal", lambda *a: calls.append("terminal") or True)
    monkeypatch.setattr(d, "_dispatch_vscode", lambda *a: calls.append("vscode") or True)
    monkeypatch.setattr(d, "_dispatch_intellij", lambda *a: calls.append("intellij") or True)

    d.dispatch_bring_to_front(0, "s1")
    assert calls == ["tmux"]


def test_dispatch_bring_to_front_falls_through_to_terminal(recording_daemon, monkeypatch):
    d, _ = recording_daemon
    d.session_panes["s1"] = "%3"
    d.session_ttys["s1"] = "/dev/ttys003"

    calls = []
    monkeypatch.setattr(d, "_dispatch_tmux", lambda *a: calls.append("tmux") or False)
    monkeypatch.setattr(d, "_dispatch_terminal", lambda *a: calls.append("terminal") or True)
    monkeypatch.setattr(d, "_dispatch_vscode", lambda *a: calls.append("vscode") or True)

    d.dispatch_bring_to_front(0, "s1")
    assert calls == ["tmux", "terminal"]


def test_dispatch_bring_to_front_falls_through_to_vscode_then_intellij(
    recording_daemon, monkeypatch
):
    d, _ = recording_daemon
    d.session_projects["s1"] = "my-project"  # no pane, no tty known

    calls = []
    monkeypatch.setattr(d, "_dispatch_vscode", lambda *a: calls.append("vscode") or False)
    monkeypatch.setattr(d, "_dispatch_intellij", lambda *a: calls.append("intellij") or True)

    d.dispatch_bring_to_front(0, "s1")
    assert calls == ["vscode", "intellij"]


def test_dispatch_bring_to_front_no_target_known_is_a_noop(recording_daemon):
    d, _ = recording_daemon
    # No pane, tty, or project recorded for this session — should not
    # raise, just log and return.
    d.dispatch_bring_to_front(0, "unknown-session")


# --- on_device_event: key press routing ---------------------------------


def test_key_press_with_no_mapped_session_clears_the_slot(recording_daemon):
    d, sent = recording_daemon
    d.on_device_event({"t": "key", "i": 0})
    assert sent == [{"t": "clear", "i": 0}]


def test_key_press_with_mapped_session_dispatches(recording_daemon, monkeypatch):
    d, sent = recording_daemon
    d.slots.allocate("s1")  # occupies slot 0

    calls = []
    monkeypatch.setattr(d, "dispatch_bring_to_front", lambda slot, sid: calls.append((slot, sid)))

    d.on_device_event({"t": "key", "i": 0})
    assert calls == [(0, "s1")]
    assert sent == []  # no clear sent — a real session was found


def test_key_press_out_of_range_index_is_ignored(recording_daemon):
    d, sent = recording_daemon
    d.on_device_event({"t": "key", "i": 999})
    assert sent == []


# --- on_device_event: key_held routing (manual slot eviction) -----------


def test_key_held_on_mapped_slot_clears_it_and_forgets_the_session(recording_daemon):
    d, sent = recording_daemon
    d.slots.allocate("s1")  # occupies slot 0
    d.session_agents["s1"] = "claude-code"
    d.session_panes["s1"] = "%3"
    d.session_ttys["s1"] = "/dev/ttys003"
    d.session_projects["s1"] = "my-project"
    d.pending_calls["s1"] = {"tool_name": "Bash"}

    d.on_device_event({"t": "key_held", "i": 0})

    assert sent == [{"t": "clear", "i": 0}]
    assert d.slots.slot_for("s1") is None
    assert "s1" not in d.session_agents
    assert "s1" not in d.session_panes
    assert "s1" not in d.session_ttys
    assert "s1" not in d.session_projects
    assert "s1" not in d.pending_calls
    # slot is free again for a new session
    assert d.slots.allocate("s2") == 0


def test_key_held_on_empty_slot_still_clears_the_pad(recording_daemon):
    d, sent = recording_daemon
    d.on_device_event({"t": "key_held", "i": 0})
    assert sent == [{"t": "clear", "i": 0}]


def test_key_held_out_of_range_index_is_ignored(recording_daemon):
    d, sent = recording_daemon
    d.on_device_event({"t": "key_held", "i": 999})
    assert sent == []


def test_encoder_and_click_events_do_not_raise(recording_daemon):
    d, _ = recording_daemon
    d.on_device_event({"t": "enc", "d": 1})
    d.on_device_event({"t": "enc_click"})
    d.on_device_event({"t": "something_unknown"})
