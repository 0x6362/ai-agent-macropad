"""
HID connection to a QMK-based pad (e.g. NuPhy Air75 V2, Keychron K0 Max).

Owns the raw-HID device handle's whole lifecycle: discovery (see
discover_hid_device()/discover_hid_pad() below), the open connection
itself, a background thread that translates inbound reports into the
dict shapes daemon.py's on_device_event() consumes, and reconnection
after the OS-level handle dies (e.g. a wireless pad's BT link dropping
while asleep).

HidPadLink.open()/close() are meant to be called repeatedly over the
life of a daemon process, not just once at startup/shutdown — see
daemon.py's Daemon._reconcile_pad(), which releases the handle
whenever no Claude Code sessions are active (so the VIA app, which
needs exclusive access to this same raw HID interface, can use it)
and reacquires it lazily on the next session. Both open() and close()
are written to be safe across multiple cycles on the same instance —
see the comments inline on the specific state each one resets.
"""
import logging
from dataclasses import dataclass
import threading
import time

try:
    import hid  # optional — only needed to actually attach to a pad
except ImportError:
    hid = None

import hid_protocol

log = logging.getLogger("macropad-daemon")

# QMK's raw HID usage page/usage (RAW_USAGE_PAGE/RAW_USAGE_ID defaults
# in tmk_core/protocol/usb_descriptor_common.h) — narrows HID discovery
# to the raw HID interface specifically, not the same board's normal
# boot-keyboard/NKRO HID interfaces, which share the same VID/PID.
RAW_USAGE_PAGE = 0xFF60
RAW_USAGE_ID = 0x61


@dataclass(frozen=True)
class Ready:
    probe_at: float


@dataclass(frozen=True)
class AwaitingHello:
    deadline: float


@dataclass(frozen=True)
class Unresponsive:
    pass


LinkHealth = Ready | AwaitingHello | Unresponsive


def advance_health(health: LinkHealth, now: float, timeout: float) -> LinkHealth:
    match health:
        case Ready(probe_at) if now >= probe_at:
            return AwaitingHello(now + timeout)
        case AwaitingHello(deadline) if now >= deadline:
            return Unresponsive()
        case _:
            return health


def discover_hid_device(vid, pid, handshake_timeout=1.5):
    """Find a QMK pad's raw-HID interface path without assuming it's
    the only HID interface the board exposes. A QMK keyboard enumerates
    several interfaces under one VID/PID (boot keyboard, NKRO, and the
    raw HID/VIA interface) — usage_page/usage narrows candidates to
    just the raw HID one (RAW_USAGE_PAGE/RAW_USAGE_ID). Each candidate
    then gets a MSG_PING and only counts as a match if it answers
    MSG_HELLO within handshake_timeout — raw HID is call-and-response
    (see hid_protocol.py), so nothing arrives unprompted.

    Returns the interface's opaque hidapi `path`, or None if nothing
    answered.
    """
    if hid is None:
        return None
    try:
        candidates = [
            d for d in hid.enumerate(vid, pid)
            if d["usage_page"] == RAW_USAGE_PAGE and d["usage"] == RAW_USAGE_ID
        ]
    except hid.HIDException:
        log.warning("HID enumeration failed vid=0x%04x pid=0x%04x", vid, pid, exc_info=True)
        return None
    log.info(
        "HID discovery: %d raw-HID interface(s) matching vid=0x%04x pid=0x%04x",
        len(candidates), vid, pid,
    )
    if not candidates:
        return None

    for candidate in candidates:
        path = candidate["path"]
        try:
            dev = hid.Device(path=path)
        except hid.HIDException as e:
            log.info("HID discovery: %r failed to open: %s", path, e)
            continue
        try:
            dev.write(bytes([0]) + hid_protocol.pack_ping())
            deadline = time.monotonic() + handshake_timeout
            retry_at = time.monotonic() + handshake_timeout / 2
            while time.monotonic() < deadline:
                data = dev.read(hid_protocol.REPORT_SIZE, timeout=100)
                if not data:
                    if time.monotonic() >= retry_at:
                        dev.write(bytes([0]) + hid_protocol.pack_ping())
                        retry_at = deadline
                    continue
                msg = hid_protocol.parse_report(bytes(data))
                if msg and msg.get("t") == "hello":
                    log.info("HID discovery: %r replied hello — match", path)
                    return path
        except hid.HIDException as e:
            log.info("HID discovery: %r failed during handshake: %s", path, e)
        finally:
            try:
                dev.close()
            except hid.HIDException:
                log.warning("HID probe close failed path=%r", path, exc_info=True)

    log.info("HID discovery: no interface answered hello within %ss", handshake_timeout)
    return None


def discover_hid_pad(candidates=hid_protocol.KNOWN_HID_PADS, handshake_timeout=1.5):
    """Tries each hid_protocol.KnownPad in `candidates` in turn via
    discover_hid_device(), returning (path, pad) for whichever one
    answers hello first, or None if none do. Lets HidPadLink
    auto-detect which known QMK pad (if any) is plugged in.
    """
    if hid is None:
        return None
    for pad in candidates:
        path = discover_hid_device(pad.vid, pad.pid, handshake_timeout=handshake_timeout)
        if path:
            return path, pad
    return None


def _pack_for_hid(obj):
    """Translate a write_json() dict into a Phase 1 binary report, or
    None if this message type has no HID encoding. Only "slot" and
    "clear" ever reach write_json() today — a cleared slot has no
    label to clear on an RGB-only pad, so it's just MSG_SLOT with
    state "off" (fully dark — distinct from "idle"'s dim glow).
    """
    t = obj.get("t")
    if t == "bank":
        return hid_protocol.pack_bank_state(obj["bank"], obj["page"], obj["pages"], obj["attention"],
                                           obj.get("permission_token", 0))
    if t == "slot":
        return hid_protocol.pack_slot(obj["i"], obj["state"])
    if t == "clear":
        return hid_protocol.pack_slot(obj["i"], "off")
    return None


class HidPadLink:
    """Owns the HID connection to a QMK-based pad. write_json(obj) is
    host->device; a background thread handles device->host reads so
    the daemon's socket server never blocks on HID I/O.

    open()/close() are safe to call repeatedly on the same instance —
    see the comments in each about what state they reset, needed
    because Daemon._reconcile_pad() cycles this connection open and
    closed over the daemon's lifetime rather than opening it once at
    startup and closing it once at shutdown.
    """

    # Seconds between reconnect attempts once the handle has died — a
    # class attribute (not a literal in _reconnect()) so tests can
    # shrink it instead of eating this delay for real.
    RECONNECT_POLL_SECONDS = 2
    HEARTBEAT_INTERVAL_SECONDS = 5
    HEARTBEAT_TIMEOUT_SECONDS = 1.5
    # The K0 receiver drops raw-HID bursts; 25 ms keeps slot and bank
    # updates within its forwarding capacity, including clear reports.
    WIRELESS_WRITE_INTERVAL = 0.025

    def __init__(self, on_device_event, vid=None, pid=None, on_reattach=None):
        self.on_device_event = on_device_event
        self.attached = False
        # Replays display state on reconnection and periodic hello
        # confirmations, including sleeps shorter than a probe interval.
        self.on_reattach = on_reattach or (lambda: None)
        # Correlates a pending handshake() call with the "hello" reply
        # its ping eventually produces, without racing the background
        # reader thread for the same bytes — see _dispatch()/
        # handshake() below.
        self._hello_event = threading.Event()
        self._last_hello = None
        self._write_lock = threading.Lock()
        self._last_write_at = None
        self._health: LinkHealth = Unresponsive()
        self._io_failed = threading.Event()

        # An explicit vid/pid pins discovery to just that one (unnamed)
        # board; otherwise try every board this module knows about (see
        # hid_protocol.KNOWN_HID_PADS) and attach to whichever one
        # actually answers. self.vid/self.pid start as whatever was
        # passed in (possibly None) and get set to the pad that
        # actually answered once open()/_reconnect() succeeds.
        if vid is not None and pid is not None:
            self.candidates = (hid_protocol.KnownPad(None, vid, pid, None),)
        else:
            self.candidates = hid_protocol.KNOWN_HID_PADS
        self.vid = vid
        self.pid = pid
        self._dev = None
        self._stop = threading.Event()
        self._thread = None

    @property
    def running(self):
        """Whether a worker owns attachment or is trying to recover it."""
        return self._thread is not None and self._thread.is_alive()

    def open(self):
        if self.running:
            return
        # Undo close()'s permanent-looking stop signal from a prior
        # cycle on this same instance — without this, a reader thread
        # started below would see _stop already set and exit
        # immediately, leaving attached=True but nothing actually
        # reading device->host reports.
        self._stop.clear()
        self._io_failed.clear()
        self._hello_event.clear()
        self._last_hello = None
        self._health = Unresponsive()

        if hid is None:
            log.warning(
                "the 'hid' package isn't installed — HID transport unavailable "
                "(pip install hid, plus the native hidapi library)"
            )
            return
        found = discover_hid_pad(self.candidates)
        if not found:
            log.warning(
                "no known HID pad found (tried: %s) — running headless "
                "(will keep checking until the connection is closed)",
                ", ".join("%s (0x%04x/0x%04x)" % (p.name or "unnamed", p.vid, p.pid) for p in self.candidates),
            )
        else:
            path, pad = found
            self.vid, self.pid = pad.vid, pad.pid
            try:
                self._dev = hid.Device(path=path)
            except hid.HIDException:
                log.exception("failed to open HID device at %r", path)
            else:
                self.attached = True
                self._health = Ready(time.monotonic() + self.HEARTBEAT_INTERVAL_SECONDS)
                log.info("attached to pad over HID (path=%r)", path)
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread:
            # Discovery can outlast one read timeout. It must finish
            # before open() may clear the stop signal for another run.
            self._thread.join()
        self._close_device()
        # Reset every bit of state open() depends on being fresh — a
        # caller that checks .attached right after close() (e.g.
        # Daemon._reconcile_pad()) needs an accurate answer, and a
        # later open() call needs a clean slate rather than a stale
        # handle it never reopens.
        self.attached = False
        self._dev = None
        self._thread = None
        self._health = Unresponsive()

    def _close_device(self):
        with self._write_lock:
            device, self._dev = self._dev, None
            self.attached = False
            self._last_write_at = None
            if device is not None:
                try:
                    device.close()
                except hid.HIDException:
                    log.warning("HID close failed", exc_info=True)

    def write_json(self, obj):
        report = _pack_for_hid(obj)
        if report is None:
            log.debug("HID transport: no encoding for %s, dropping", obj)
            return
        if self._dev is None:
            log.debug("(headless) would send: %s", obj)
            return
        try:
            self._write_report(report)
        except hid.HIDException:
            log.exception("HID write failed")
            self._io_failed.set()

    def _send_ping(self):
        if self._dev is None:
            return
        try:
            self._write_report(hid_protocol.pack_ping())
        except hid.HIDException:
            log.exception("HID write failed (handshake ping)")
            self._io_failed.set()

    def _write_report(self, report):
        with self._write_lock:
            if self._dev is None:
                return
            wireless = hid_protocol.KEYCHRON_K0_MAX_WIRELESS
            if (self.vid, self.pid) == (wireless.vid, wireless.pid) and self._last_write_at is not None:
                delay = self.WIRELESS_WRITE_INTERVAL - (time.monotonic() - self._last_write_at)
                if delay > 0:
                    time.sleep(delay)
            # hidapi requires report-ID 0 before the unnumbered report.
            self._dev.write(bytes([0]) + report)
            self._last_write_at = time.monotonic()

    def _dispatch(self, msg):
        """The read loop calls this instead of on_device_event
        directly. A "hello" reply is forwarded normally *and* wakes up
        any handshake() call waiting on one — the same background
        thread does both jobs, so there's no separate blocking read to
        race against it.
        """
        replay = msg.get("t") == "hello" and isinstance(self._health, AwaitingHello)
        if msg.get("t") == "hello":
            self._health = Ready(time.monotonic() + self.HEARTBEAT_INTERVAL_SECONDS)
            self._last_hello = msg
            self._hello_event.set()
        self.on_device_event(msg)
        if replay:
            self.on_reattach()

    def handshake(self, timeout=1.5):
        """Ask the device how many slots it has: pings and waits up to
        `timeout` seconds for a "hello" reply correlated via
        _dispatch() above. Returns {"slots": N, "protocol": P}, or
        None if open() found no pad (nothing to ask) or nothing valid
        replied in time — callers should fall back to a sane default
        rather than treat that as fatal, same posture as a headless
        pad generally. `protocol` is the pad's PROTOCOL_VERSION; the
        daemon warns (but still attaches) if it doesn't match ours.
        """
        if not self.attached:
            return None
        self._hello_event.clear()
        self._send_ping()
        if self._hello_event.wait(timeout):
            result = {
                "slots": self._last_hello.get("slots"),
                "protocol": self._last_hello.get("protocol"),
            }
            if self._last_hello.get("capabilities"):
                result["capabilities"] = self._last_hello["capabilities"]
            return result
        log.warning("pad didn't answer a slots handshake within %ss", timeout)
        return None

    def _read_loop(self):
        while not self._stop.is_set():
            if self._dev is None or self._io_failed.is_set():
                self._health = Unresponsive()
                self._reconnect()
                continue
            try:
                data = self._dev.read(hid_protocol.REPORT_SIZE, timeout=200)
            except hid.HIDException:
                log.warning("HID read failed — pad asleep/disconnected, reconnecting")
                self._io_failed.set()
                continue
            if self._stop.is_set():
                break
            if data:
                msg = hid_protocol.parse_report(bytes(data))
                if msg is None:
                    log.warning("unrecognized HID report: %r", bytes(data))
                else:
                    self._dispatch(msg)
            health = advance_health(self._health, time.monotonic(), self.HEARTBEAT_TIMEOUT_SECONDS)
            if health != self._health:
                self._health = health
                if isinstance(health, AwaitingHello):
                    self._send_ping()
                else:
                    log.warning("pad hello timed out — receiver may be connected while pad sleeps; reconnecting")
                    self._io_failed.set()

    def _reconnect(self):
        """Block (without spinning) until the pad reappears over HID
        or close() is called, then swap in a fresh device handle.
        Re-runs discover_hid_device() each attempt rather than
        reopening the old path directly, since it may no longer be
        valid after the underlying handle died.
        """
        self._close_device()
        self._io_failed.clear()
        while not self._stop.is_set():
            found = discover_hid_pad(self.candidates)
            if self._stop.is_set():
                return
            if found:
                path, pad = found
                self.vid, self.pid = pad.vid, pad.pid
                try:
                    device = hid.Device(path=path)
                except hid.HIDException:
                    log.warning("HID reopen failed path=%r", path, exc_info=True)
                else:
                    with self._write_lock:
                        self._dev = device
                    if self._stop.is_set():
                        self._close_device()
                        return
                    self.attached = True
                    self._health = Ready(time.monotonic() + self.HEARTBEAT_INTERVAL_SECONDS)
                    log.info("pad reattached over HID (path=%r)", path)
                    self.on_reattach()
                    self._send_ping()
                    return
            self._stop.wait(self.RECONNECT_POLL_SECONDS)
