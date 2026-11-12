"""
Wire-level protocol for the HID transport (NuPhy Air75 V2 and, in
principle, any other QMK-based pad with RAW_ENABLE) — fixed-size
binary reports over raw HID. See README's "Pad messages" section for
the full protocol writeup.

Consumed by pad_link.HidPadLink to translate daemon.py's
write_json(dict) calls into reports the device understands, and to
parse inbound reports back into the {"t": ...} dict shapes
Daemon.on_device_event() consumes.

Report layout (REPORT_SIZE bytes, unused trailing bytes zero-padded):

    byte 0: message type (MSG_*)
    byte 1: message-specific
    byte 2: message-specific
    byte 3: message-specific (MSG_HELLO: protocol version)
    remaining bytes: message-specific, zero-padded

MSG_PING   (host -> device): byte 1 = PROTOCOL_VERSION. Requests a
            MSG_HELLO reply — necessary because raw HID is
            call-and-response: the device only sends an IN report
            after receiving an OUT report from the host, it can't
            push "hello" unprompted the moment it's plugged in.
MSG_HELLO  (device -> host): byte 1 = device id, byte 2 = num_slots,
            byte 3 = PROTOCOL_VERSION, byte 4 = optional capability flags.
MSG_SLOT   (host -> device): byte 1 = slot index, byte 2 = state
            (see STATE_*). A cleared slot is just MSG_SLOT with
            STATE_OFF — the firmware's handle_message() equivalent
            renders "clear" and "idle" as NOT visually identical (idle
            is a dim gray glow, meaning a session is mapped here but
            quiet; cleared is fully black, meaning no session is
            mapped at all), so both need their own state value here
            too — they just don't need a separate wire *message
            type*, since there's no label to clear on an RGB-only pad,
            only a color.
MSG_KEY    (device -> host): byte 1 = slot index. Sent on key-down,
            mirroring the firmware's own key-event handling — this is
            what drives the instant window-focus dispatch, so it
            fires immediately rather than waiting to see how long the
            key stays down.
MSG_KEY_HELD (device -> host): byte 1 = slot index. Sent the instant
            a key has been held past the firmware's hold threshold —
            while it's still down, not on release, so the slot clears
            immediately rather than waiting for key-up. A normal tap
            never reaches the threshold, so it only ever produces a
            MSG_KEY and nothing else. Used to manually clear a slot's
            session mapping (see Daemon._evict_slot()).
MSG_SELECT_BANK (device -> host): byte 1 = source bank (0..2).
MSG_NEXT_PAGE (device -> host): no payload; advance the current bank's page.
MSG_BANK_STATE (host -> device): bytes 1..3 = bank, page, page count;
            bytes 4..6 = attention per bank; bytes 7..10 = uint32 LE
            permission token for the selected session/request.
MSG_FOCUS_APP (device -> host): byte 1 = app index (Emacs, Ghostty).
MSG_PERMISSION_DECISION (device -> host): byte 1 = allow (1) / deny (0);
            bytes 2..5 echo the uint32 LE permission token.
"""

from collections import namedtuple

REPORT_SIZE = 32  # QMK's RAW_EPSIZE default for ChibiOS boards (confirmed unchanged in
                   # NuPhy's fork during Phase 0 — see keyboards/nuphy/air75_v2/ansi/)

# MSG_SLOT/MSG_PING/MSG_KEY live at 0x20+ deliberately: 0x01-0x15 is
# QMK VIA's own reserved via_command_id range (quantum/via.h), which
# the claude_macropad keymap now builds with VIA_ENABLE=yes — a value
# inside that range would collide with VIA's own commands on that same
# raw HID endpoint. See claude_macropad.h for the firmware side of
# this.
#
# MSG_HELLO is deliberately not the next sequential byte after MSG_KEY
# below — it's the value discover_hid_device() treats as proof this is
# our pad (see parse_report()'s msg_type dispatch), and 0x01 is exactly
# what an unrelated raw-HID interface's first-ever report is likely to
# contain by coincidence. 0xA1 ("AI") is distinctive enough that a
# collision would mean something is actually wrong (and is well clear
# of VIA's range too).
MSG_HELLO = 0xA1
MSG_SLOT = 0x20
MSG_PING = 0x21
MSG_KEY = 0x22
MSG_KEY_HELD = 0x23
MSG_SELECT_BANK = 0x24
MSG_NEXT_PAGE = 0x25
MSG_BANK_STATE = 0x26
MSG_FOCUS_APP = 0x27
MSG_PERMISSION_DECISION = 0x28
CAP_SESSION_BANKS = 0x01
CAP_PERMISSION_TOKEN = 0x02

# Sent on both MSG_PING (byte 1) and MSG_HELLO (byte 3). Must match
# AI_AGENT_MACROPAD_PROTOCOL_VERSION in
# qmk-userspace/users/ai_agent_macropad/ai_agent_macropad.h — the
# pytest suite greps that #define so the two can't drift.
PROTOCOL_VERSION = 1

# "off" turns out NOT to be device-local only: the firmware's own
# handle_message() equivalent sends it explicitly on "clear" (see
# MSG_SLOT above), distinct from "idle". hook_to_state() itself never
# produces "off" — only write_json({"t": "clear", ...})'s translation
# does.
STATE_IDLE = 0
STATE_WORKING = 1
STATE_WAITING = 2
STATE_DONE = 3
STATE_ERROR = 4
STATE_QUESTION = 5
STATE_TOOL_RUNNING = 6
STATE_TOOL_STALLED = 7

# Deliberately pinned far above the currently-defined states above,
# rather than "however many states currently exist" — so adding a new
# state in the future only ever means inserting another STATE_* constant
# before this one, never renumbering STATE_OFF itself (and everything
# that's anchored to its value: claude_macropad.c's raw_hid_receive
# bounds check, and its enum mirror in claude_macropad.h). Byte values
# between the last defined state and STATE_OFF are reserved headroom —
# a device that receives one it doesn't recognize (e.g. an older
# firmware build talking to a newer daemon) renders it as a distinct
# "unknown" fallback color rather than silently reusing idle's.
STATE_OFF = 31

STATE_TO_CODE = {
    "idle": STATE_IDLE,
    "working": STATE_WORKING,
    "waiting": STATE_WAITING,
    "done": STATE_DONE,
    "error": STATE_ERROR,
    "question": STATE_QUESTION,
    "tool_running": STATE_TOOL_RUNNING,
    "tool_stalled": STATE_TOOL_STALLED,
    "off": STATE_OFF,
}
CODE_TO_STATE = {v: k for k, v in STATE_TO_CODE.items()}

# Confirmed against NuPhy's own keyboard.json during Phase 0.
DEVICE_ID_AIR75_V2 = 0xA7

# Keychron K0 Max (RGB numpad) — see qmk-userspace/keyboards/keychron/k0_max/
# keymaps/ai_agent_macropad/keymap.c. VID/PID (0x3434/0x0A06) and
# DEVICE_ID confirmed against a live board; see the README's Keychron
# K0 Max section.
DEVICE_ID_K0_MAX = 0xC0

# Single source of truth for every known QMK HID pad's identity: `vid`/
# `pid` for USB discovery (pad_link.discover_hid_pad()), `device_id`
# for the MSG_HELLO handshake byte above. pad_link.py and
# hid_bringup_test.py both import this module rather than keeping
# their own copies of vid/pid.
KnownPad = namedtuple("KnownPad", ["name", "vid", "pid", "device_id"])

NUPHY_AIR75_V2 = KnownPad("NuPhy Air75 V2 (ANSI)", 0x19F5, 0x3246, DEVICE_ID_AIR75_V2)
KEYCHRON_K0_MAX = KnownPad("Keychron K0 Max", 0x3434, 0x0A06, DEVICE_ID_K0_MAX)
KEYCHRON_K0_MAX_WIRELESS = KnownPad("Keychron K0 Max (2.4 GHz)", 0x3434, 0xD030, DEVICE_ID_K0_MAX)

# Tried in this order by daemon.py's discover_hid_pad() — add a new
# KnownPad here for any future board rather than hardcoding its VID/PID
# somewhere else.
KNOWN_HID_PADS = (NUPHY_AIR75_V2, KEYCHRON_K0_MAX, KEYCHRON_K0_MAX_WIRELESS)


def pack_ping():
    """Host -> device: request a MSG_HELLO reply, advertising
    PROTOCOL_VERSION.
    """
    report = bytearray(REPORT_SIZE)
    report[0] = MSG_PING
    report[1] = PROTOCOL_VERSION
    return bytes(report)


def pack_bank_state(bank, page, pages, attention, permission_token=0):
    if not 0 <= bank < 3 or not 0 <= page <= 255 or not 1 <= pages <= 255:
        raise ValueError("invalid bank view")
    if len(attention) != 3 or any(value not in (0, 1, 2) for value in attention):
        raise ValueError("invalid bank attention states")
    return (bytes([MSG_BANK_STATE, bank, page, pages, *attention])
            + permission_token.to_bytes(4, "little")).ljust(REPORT_SIZE, b"\0")


def pack_slot(index, state):
    """Host -> device: set one slot's state.

    `index` must fit in a byte (0..255 — SlotManager's num_slots is
    never that large in practice, but this isn't the place to assume
    a cap); `state` is one of hook_to_state's string outputs.
    """
    if not (0 <= index <= 0xFF):
        raise ValueError(f"slot index {index} does not fit in a byte")
    if state not in STATE_TO_CODE:
        raise ValueError(f"unknown state {state!r}")
    report = bytearray(REPORT_SIZE)
    report[0] = MSG_SLOT
    report[1] = index
    report[2] = STATE_TO_CODE[state]
    return bytes(report)


def parse_report(report):
    """Device -> host: decode one inbound report into the {"t": ...}
    dict shapes pad_link.HidPadLink's _read_loop hands to
    on_device_event.

    Returns None for anything not recognized (unknown message type,
    or a report shorter than the fields it claims to carry) rather
    than raising — malformed input is just dropped.
    """
    if len(report) < 1:
        return None
    msg_type = report[0]
    if msg_type == MSG_HELLO:
        if len(report) < 4:
            return None
        message = {
            "t": "hello",
            "device": report[1],
            "slots": report[2],
            "protocol": report[3],
        }
        if len(report) > 4 and report[4]:
            message["capabilities"] = report[4]
        return message
    if msg_type == MSG_SELECT_BANK and len(report) >= 2:
        return {"t": "select_bank", "bank": report[1]}
    if msg_type == MSG_NEXT_PAGE:
        return {"t": "next_page"}
    if msg_type == MSG_FOCUS_APP and len(report) >= 2:
        return {"t": "focus_app", "app": report[1]}
    if msg_type == MSG_PERMISSION_DECISION and len(report) >= 6 and report[1] in (0, 1):
        return {"t": "permission_decision", "allow": bool(report[1]),
                "token": int.from_bytes(report[2:6], "little")}
    if msg_type == MSG_KEY:
        if len(report) < 2:
            return None
        return {"t": "key", "i": report[1]}
    if msg_type == MSG_KEY_HELD:
        if len(report) < 2:
            return None
        return {"t": "key_held", "i": report[1]}
    return None
