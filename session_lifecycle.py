"""Conservative observations of the terminal or buffer displaying a session."""

from dataclasses import dataclass
from enum import Enum
import os
import subprocess

from session_banks import SlotAddress


class Presence(Enum):
    ALIVE = "alive"
    CLOSED = "closed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TrackedSession:
    session_id: str
    address: SlotAddress
    pid: int | None
    epoch: int


@dataclass(frozen=True)
class Observation:
    presence: Presence
    pid: int | None = None


def process_presence(pid):
    """A missing process or a process without a terminal confirms closure."""
    if not isinstance(pid, int) or pid <= 0:
        return Presence.UNKNOWN
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return Presence.CLOSED
    except OSError:
        return Presence.UNKNOWN
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "stat=,tty="],
                                capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return Presence.UNKNOWN
    fields = result.stdout.split()
    if result.returncode or len(fields) != 2:
        return Presence.UNKNOWN
    status, tty = fields
    return Presence.CLOSED if status.startswith("Z") or tty == "??" else Presence.ALIVE


def terminal_presence(session_id, pid, visible_sessions, process):
    """Unmatchable titles and failed catalogs are not evidence of closure."""
    if process is Presence.CLOSED:
        return Presence.CLOSED
    if session_id in visible_sessions:
        return Presence.ALIVE
    if process is Presence.ALIVE and pid in visible_sessions.values():
        return Presence.CLOSED  # The same client is displaying another session.
    return Presence.UNKNOWN


def buffer_presence(session_id, open_sessions):
    if open_sessions is None:
        return Presence.UNKNOWN
    return Presence.ALIVE if session_id in open_sessions else Presence.CLOSED
