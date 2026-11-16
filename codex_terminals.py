"""Bind Codex sessions to the local tmux clients displaying them.

Shared app-server hooks inherit the server's environment. The displayed
thread name and project come from each TUI's default title and are matched
against saved session metadata. Codex truncates each title segment with a
trailing "..." (its widths vary by release), and names the project after the
git repository or `.codex` project root containing the cwd, so a segment
matches a full value it is a prefix of, and a project matches any directory
name on the cwd's path. Missing and ambiguous matches produce no binding.
"""

from collections import defaultdict
import logging
import os
from pathlib import Path
import re
import sqlite3
import subprocess

log = logging.getLogger("macropad-daemon")


def normalize_title(title):
    title = re.sub(r"^\[[!. ]+\] Action Required \| ", "", title)
    return re.sub(r"^[\u2800-\u28ff] ", "", title)


def segment_matches(segment, value):
    """True when a title segment shows `value` whole or truncated with "..."."""
    if segment == value:
        return True
    return segment.endswith("...") and len(value) > len(segment) - 3 and value.startswith(segment[:-3])


def title_matches(title, name, cwd):
    displayed_name, separator, displayed_project = title.rpartition(" | ")
    path = Path(cwd)
    return bool(separator) and segment_matches(displayed_name, name) and any(
        segment_matches(displayed_project, directory.name) for directory in (path, *path.parents) if directory.name)


def match_terminals(threads, panes, processes):
    """Return only one-to-one session/pane matches backed by a Codex TTY."""
    threads = [(session_id, name, cwd) for session_id, name, cwd in threads if name and cwd]
    matches = defaultdict(list)
    for pane in panes:
        pids = processes.get(pane["tty"], ())
        if pane["command"] != "codex" or len(pids) != 1:
            continue
        title = normalize_title(pane["title"])
        candidates = [(session_id, cwd) for session_id, name, cwd in threads if title_matches(title, name, cwd)]
        if len(candidates) != 1:
            continue
        session_id, cwd = candidates[0]
        matches[session_id].append({
            "sessionId": session_id, "cwd": cwd, "pid": pids[0],
            "tty": pane["tty"], "tmux_socket": pane["socket"], "tmux_pane": pane["pane"],
        })
    return [bindings[0] for bindings in matches.values() if len(bindings) == 1]


def _thread_metadata():
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()
    database = codex_home / "state_5.sqlite"
    if not database.is_file():
        return []
    try:
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=.2)
        try:
            return connection.execute(
                "SELECT id, COALESCE(NULLIF(name, ''), title), cwd FROM threads WHERE archived = 0"
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as error:
        log.info("Codex terminal metadata unavailable: %s", error)
        return []


def _run(command):
    """Read process/tmux metadata without starting sessions or changing focus."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout if result.returncode == 0 else ""


def _codex_processes():
    processes = defaultdict(list)
    for line in _run(["ps", "-axo", "pid=,tty=,comm="]).splitlines():
        fields = line.split(None, 2)
        if len(fields) != 3:
            continue
        pid, tty, executable = fields
        if tty != "??" and pid.isdigit() and Path(executable).name == "codex":
            processes["/dev/" + tty].append(int(pid))
    return processes


def _tmux_panes():
    directory = Path(os.environ.get("TMUX_TMPDIR", "/tmp")) / f"tmux-{os.getuid()}"
    try:
        sockets = {path.resolve() for path in directory.iterdir() if path.is_socket()} if directory.is_dir() else set()
    except OSError as error:
        log.info("tmux socket discovery unavailable: %s", error)
        sockets = set()
    inherited = os.environ.get("TMUX", "").split(",", 1)[0]
    if inherited:
        sockets.add(Path(inherited).resolve())
    panes = []
    for socket in sorted(sockets):
        output = _run(["tmux", "-S", str(socket), "list-panes", "-a", "-F",
                       "#{socket_path}\t#{pane_id}\t#{pane_tty}\t#{pane_current_command}\t#{pane_title}"])
        for line in output.splitlines():
            fields = line.split("\t", 4)
            if len(fields) == 5:
                panes.append(dict(zip(("socket", "pane", "tty", "command", "title"), fields)))
    return panes


def discover_codex_terminals():
    return match_terminals(_thread_metadata(), _tmux_panes(), _codex_processes())
