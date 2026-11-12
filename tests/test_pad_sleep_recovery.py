import queue
import threading
import time
import types

import pytest

import hid_protocol
import pad_link

from test_hid_pad_link import FakeHidException, hello_report, make_candidate


def wait_until(predicate, timeout=1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.002)
    assert predicate(), "condition not reached before deadline"


class WirelessPad:
    """A receiver stays enumerated while its sleeping pad drops reports."""

    def __init__(self):
        self.awake = True
        self.devices = []
        self.slots = [hid_protocol.STATE_OFF] * 2
        self.bank = None

    def device(self, path):
        device = ReceiverHandle(self)
        self.devices.append(device)
        return device


class ReceiverHandle:
    def __init__(self, pad):
        self.pad = pad
        self.incoming = queue.Queue()
        self.closed = False
        self.stale = False
        self.fail_writes = False
        self.pings = 0

    def write(self, packet):
        if self.closed or self.fail_writes:
            raise FakeHidException("stale handle after wake")
        if not self.pad.awake or self.stale:
            return len(packet)
        report = packet[1:]
        if report[0] == hid_protocol.MSG_PING:
            self.pings += 1
            hello = bytearray(hello_report(slots=2))
            hello[4] = hid_protocol.CAP_SESSION_BANKS
            self.incoming.put(bytes(hello))
        elif report[0] == hid_protocol.MSG_SLOT:
            self.pad.slots[report[1]] = report[2]
        elif report[0] == hid_protocol.MSG_BANK_STATE:
            self.pad.bank = tuple(report[1:4])
        return len(packet)

    def read(self, size, timeout=None):
        try:
            return self.incoming.get(timeout=.002)
        except queue.Empty:
            return b""

    def close(self):
        self.closed = True


@pytest.fixture
def wireless_pad(monkeypatch):
    pad = WirelessPad()
    board = hid_protocol.KEYCHRON_K0_MAX_WIRELESS
    monkeypatch.setattr(pad_link, "hid", types.SimpleNamespace(
        Device=pad.device, HIDException=FakeHidException,
        enumerate=lambda vid, pid: [make_candidate(b"receiver", vid=board.vid, pid=board.pid)]
        if (vid, pid) == (board.vid, board.pid) else []))
    discover = pad_link.discover_hid_pad
    monkeypatch.setattr(pad_link, "discover_hid_pad",
                        lambda candidates: discover(candidates, handshake_timeout=.02))
    monkeypatch.setattr(pad_link.HidPadLink, "HEARTBEAT_INTERVAL_SECONDS", .03, raising=False)
    monkeypatch.setattr(pad_link.HidPadLink, "HEARTBEAT_TIMEOUT_SECONDS", .025, raising=False)
    monkeypatch.setattr(pad_link.HidPadLink, "RECONNECT_POLL_SECONDS", .01)
    monkeypatch.setattr(pad_link.HidPadLink, "WIRELESS_WRITE_INTERVAL", 0)
    return pad


def test_wireless_sleep_restores_bank_page_colors_empty_slots_and_key_target(
        recording_daemon, wireless_pad, monkeypatch):
    d, _ = recording_daemon
    d.apply_handshake({"slots": 2, "capabilities": hid_protocol.CAP_SESSION_BANKS})
    addresses = [d.slots.allocate(name, "agent-shell") for name in ("a", "b", "c", "d")]
    d.slots.select_bank(2)
    d.slots.next_page()
    focused = []
    monkeypatch.setattr(d, "dispatch_bring_to_front", lambda i, session: focused.append(session))
    d.pad = pad_link.HidPadLink(d.on_device_event, on_reattach=d.resync_pad)
    d.pad.open()
    try:
        for address in addresses:
            d._send_pad({"t": "slot", "i": address, "state": "working"})
        old_handle = wireless_pad.devices[-1]
        wireless_pad.awake = False
        d._send_pad({"t": "slot", "i": addresses[2], "state": "done"})
        d.slots.free("d")
        d._send_pad({"t": "clear", "i": addresses[3]})
        wait_until(lambda: old_handle.closed)
        wireless_pad.awake = True
        wait_until(lambda: wireless_pad.bank == (2, 1, 2)
                   and wireless_pad.slots == [hid_protocol.STATE_DONE, hid_protocol.STATE_OFF])
        assert d.slots.slot_for("c") == addresses[2]
        assert d.slots.slot_for("a") == addresses[0]
        wireless_pad.devices[-1].incoming.put(
            bytes([hid_protocol.MSG_KEY, 0]) + bytes(hid_protocol.REPORT_SIZE - 2))
        wait_until(lambda: focused == ["c"])
    finally:
        d.pad.close()


def test_short_sleep_restores_missed_update_without_reopening(wireless_pad):
    link = pad_link.HidPadLink(lambda msg: None)
    link.HEARTBEAT_INTERVAL_SECONDS = .1
    latest = {"t": "slot", "i": 0, "state": "done"}
    link.on_reattach = lambda: link.write_json(latest)
    link.open()
    try:
        original = wireless_pad.devices[-1]
        wireless_pad.awake = False
        link.write_json(latest)
        wireless_pad.awake = True
        wait_until(lambda: wireless_pad.slots[0] == hid_protocol.STATE_DONE)
        assert not original.closed
    finally:
        link.close()


@pytest.mark.parametrize("failure", ["silent_handle", "write_error"])
def test_stale_handle_after_mac_wake_reopens_and_restores_display(wireless_pad, failure):
    recovered = threading.Event()
    link = pad_link.HidPadLink(lambda msg: None)

    def restore():
        link.write_json({"t": "slot", "i": 0, "state": "question"})
        recovered.set()

    link.on_reattach = restore
    link.open()
    try:
        original = wireless_pad.devices[-1]
        if failure == "silent_handle":
            original.stale = True
        else:
            original.fail_writes = True
            link.write_json({"t": "slot", "i": 0, "state": "question"})
        assert recovered.wait(1)
        assert original.closed
        assert wireless_pad.slots[0] == hid_protocol.STATE_QUESTION
        assert link.attached
    finally:
        link.close()


def test_quiet_healthy_pad_keeps_its_handle_across_heartbeats(wireless_pad):
    link = pad_link.HidPadLink(lambda msg: None)
    link.open()
    try:
        original = wireless_pad.devices[-1]
        wait_until(lambda: original.pings >= 3)
        assert not original.closed
        assert link.attached
        assert len(wireless_pad.devices) == 2  # discovery and persistent handle
    finally:
        link.close()


def test_pad_asleep_at_open_recovers_without_new_session_events(wireless_pad):
    wireless_pad.awake = False
    recovered = threading.Event()
    link = pad_link.HidPadLink(lambda msg: None, on_reattach=recovered.set)
    link.open()
    try:
        assert not link.attached
        wireless_pad.awake = True
        assert recovered.wait(1)
        assert link.attached
    finally:
        link.close()


def test_transient_os_discovery_failure_does_not_end_recovery(wireless_pad, monkeypatch):
    recovered = threading.Event()
    discovery_failed = threading.Event()
    link = pad_link.HidPadLink(lambda msg: None, on_reattach=recovered.set)
    link.open()
    enumerate_devices = pad_link.hid.enumerate

    def temporarily_unavailable(vid, pid):
        discovery_failed.set()
        raise FakeHidException("USB discovery unavailable during wake")

    try:
        wireless_pad.devices[-1].stale = True
        monkeypatch.setattr(pad_link.hid, "enumerate", temporarily_unavailable)
        assert discovery_failed.wait(1)
        monkeypatch.setattr(pad_link.hid, "enumerate", enumerate_devices)
        assert recovered.wait(1)
        assert link.attached
    finally:
        link.close()


def test_close_during_rediscovery_does_not_reopen_or_replay(wireless_pad, monkeypatch):
    link = pad_link.HidPadLink(lambda msg: None)
    replayed = threading.Event()
    link.on_reattach = replayed.set
    link.open()
    discovering = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    discover = pad_link.discover_hid_pad

    def blocked_discovery(candidates):
        result = discover(candidates)
        discovering.set()
        assert release.wait(1)
        return result

    monkeypatch.setattr(pad_link, "discover_hid_pad", blocked_discovery)
    original = wireless_pad.devices[-1]
    original.fail_writes = True
    link.write_json({"t": "clear", "i": 0})
    closer = threading.Thread(target=lambda: (link.close(), closed.set()))
    try:
        assert discovering.wait(1)
        opens_before_close = len(wireless_pad.devices)
        closer.start()
        assert not closed.wait(.02)
        release.set()
        assert closed.wait(1)
        assert len(wireless_pad.devices) == opens_before_close
        assert not replayed.is_set()
        assert not link.attached
    finally:
        release.set()
        if closer.ident is not None:
            closer.join(1)
        link.close()
