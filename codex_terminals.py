"""Bind Codex sessions to the local tmux clients displaying them.

Shared app-server hooks inherit the server's environment. The displayed
thread name and project come from each TUI and are matched exactly against
saved session metadata. Missing and ambiguous matches produce no binding.
"""

from collections import defaultdict
import logging
import os
from pathlib import Path
import re
import sqlite3
import subprocess

log = logging.getLogger("macropad-daemon")


def rendered_title(name, cwd):
    """Render the default Codex thread-name/project-name title components."""
    name = name if len(name) <= 48 else name[:45] + "..."
    return name + " | " + Path(cwd).name


def normalize_title(title):
    title = re.sub(r"^\[[!. ]+\] Action Required \| ", "", title)
    return re.sub(r"^[\u2800-\u28ff] ", "", title)


def match_terminals(threads, panes, processes):
    """Return only one-to-one session/pane matches backed by a Codex TTY."""
    names = defaultdict(list)
    for session_id, name, cwd in threads:
        if name and cwd:
            names[rendered_title(name, cwd)].append((session_id, cwd))
    matches = defaultdict(list)
    for pane in panes:
        pids = processes.get(pane["tty"], ())
        candidates = names.get(normalize_title(pane["title"]), ())
        if pane["command"] != "codex" or len(pids) != 1 or len(candidates) != 1:
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
    except (FileNotFoundError, subprocess.TimeoutExpired):
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
    sockets = {path.resolve() for path in directory.iterdir() if path.is_socket()} if directory.is_dir() else set()
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
