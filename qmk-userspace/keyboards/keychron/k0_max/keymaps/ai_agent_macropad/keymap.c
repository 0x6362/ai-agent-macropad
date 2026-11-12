// Keychron K0 Max keymap — AI agent session-status pad.
//
// Built against Keychron's official firmware source
// (Keychron/qmk_firmware, 2025q3 branch). Layout, matrix, RGB LED
// indices, VID/PID, and the stock keymap below are all read from
// keyboards/keychron/k0_max/ — nothing here is guessed.
//
// Digits 1–9 and 0 select ten sessions on the visible source/page.
// M1/M2/M3 select Claude, Codex, or Agent Shell; M4 advances the page.
// The shape keys use F13/F14 for app focus and F15/F16 for one-shot
// Agent Shell permission responses. M5 retains the stock FN layer.
// Slot colors use each numbered key's LED; the other keys stay dark
// except the source attention indicators and paging indicator.
//
// 2025q3 already defines a strong via_command_kb() in
// keyboards/keychron/common/keychron_raw_hid.c, so this keymap
// cannot hook that symbol itself. Apply
// qmk-userspace/keyboards/keychron/k0_max/keychron_raw_hid.c.patch
// first — it adds a weak raw_hid_receive_kb() fallthrough, which is
// where this file plugs in.
#include QMK_KEYBOARD_H
#include "keychron_common.h"
#include "ai_agent_macropad.h"
#include "raw_hid.h"

enum layers {
    BASE,
    FN,
};

// NEW_SAFE_RANGE (from keychron_common.h) is this board's own stock
// enum's end — KC_LOPTN..BAT_LVL plus wireless extras. Starting here
// (not a hardcoded offset) tracks that automatically if it ever
// changes. VIA's customKeycodes array in via.json must stay aligned
// with that same QK_KB_0-based numbering.
enum ai_agent_macropad_keycodes {
    AI_AGENT_KEY_0 = NEW_SAFE_RANGE, // digit 1
    AI_AGENT_KEY_1,                  // digit 2
    AI_AGENT_KEY_2,                  // digit 3
    AI_AGENT_KEY_3,                  // digit 4
    AI_AGENT_KEY_4,                  // digit 5
    AI_AGENT_KEY_5,
    AI_AGENT_KEY_6,
    AI_AGENT_KEY_7,
    AI_AGENT_KEY_8,
    AI_AGENT_KEY_9,
    AI_AGENT_KEY_10,
    AI_AGENT_KEY_11,
    AI_BANK_CLAUDE,
    AI_BANK_CODEX,
    AI_BANK_EMACS,
    AI_NEXT_PAGE,
};

#define NUM_MACROPAD_SLOTS AI_AGENT_MACROPAD_MAX_SLOTS
#define DEVICE_ID_K0_MAX 0xC0

// clang-format off
const uint16_t PROGMEM keymaps[][MATRIX_ROWS][MATRIX_COLS] = {
    [BASE] = LAYOUT_tenkey_27(
        KC_MUTE,        KC_F13,         KC_F14,         KC_F15,         KC_F16,
        AI_BANK_CLAUDE, KC_NUM,         KC_PSLS,        KC_PAST,        KC_PMNS,
        AI_BANK_CODEX,  AI_AGENT_KEY_6, AI_AGENT_KEY_7, AI_AGENT_KEY_8,          KC_PPLS,
        AI_BANK_EMACS,  AI_AGENT_KEY_3, AI_AGENT_KEY_4, AI_AGENT_KEY_5,
        AI_NEXT_PAGE,   AI_AGENT_KEY_0, AI_AGENT_KEY_1, AI_AGENT_KEY_2,
        MO(FN),         AI_AGENT_KEY_9,                          KC_PDOT,        KC_PENT ),

    [FN] = LAYOUT_tenkey_27(
        UG_TOGG, BT_HST1, BT_HST2, BT_HST3, P2P4G,
        _______, UG_NEXT, UG_VALU, UG_HUEU, _______,
        _______, UG_PREV, UG_VALD, UG_HUED, _______,
        _______, UG_SATU, UG_SPDU, KC_MPRV,
        _______, UG_SATD, UG_SPDD, KC_MPLY,
        _______, UG_TOGG,          KC_MNXT, _______),
};

// clang-format on
#if defined(ENCODER_MAP_ENABLE)
const uint16_t PROGMEM encoder_map[][NUM_ENCODERS][2] = {
    [BASE] = {ENCODER_CCW_CW(KC_VOLD, KC_VOLU)},
    [FN]   = {ENCODER_CCW_CW(UG_VALD, UG_VALU)},
};
#endif

void keyboard_post_init_user(void) {
    // NULL: a static table can't know where the user has put each key —
    // ai_agent_macropad_scan_slots() (safe here; via_init() already ran
    // and loaded the dynamic keymap EEPROM) builds the real one from
    // the live keymap instead.
    ai_agent_macropad_init(NUM_MACROPAD_SLOTS, NULL);
    ai_agent_macropad_scan_slots(AI_AGENT_KEY_0, NUM_MACROPAD_SLOTS);
}

static uint8_t selected_bank;
static uint8_t bank_attention[3];
static uint8_t bank_pages = 1;
static uint32_t permission_token;

uint8_t ai_agent_macropad_capabilities(void) {
    return AI_AGENT_MACROPAD_CAP_SESSION_BANKS | AI_AGENT_MACROPAD_CAP_PERMISSION_TOKEN;
}

bool process_record_user(uint16_t keycode, keyrecord_t *record) {
    if ((keycode >= AI_BANK_CLAUDE && keycode <= AI_NEXT_PAGE) ||
        (keycode >= KC_F13 && keycode <= KC_F16)) {
        if (record->event.pressed) {
            uint8_t report[AI_AGENT_MACROPAD_REPORT_SIZE] = {0};
            if (keycode == AI_NEXT_PAGE) {
                report[0] = MSG_NEXT_PAGE;
            } else if (keycode >= AI_BANK_CLAUDE && keycode <= AI_BANK_EMACS) {
                report[0] = MSG_SELECT_BANK;
                report[1] = keycode - AI_BANK_CLAUDE;
            } else if (keycode <= KC_F14) {
                report[0] = MSG_FOCUS_APP;
                report[1] = keycode - KC_F13;
            } else {
                report[0] = MSG_PERMISSION_DECISION;
                report[1] = keycode == KC_F15;
                for (uint8_t i = 0; i < 4; i++) report[2 + i] = permission_token >> (8 * i);
            }
            raw_hid_send(report, sizeof(report));
        }
        return false;
    }
    return ai_agent_macropad_process_record(keycode, record, AI_AGENT_KEY_0, NUM_MACROPAD_SLOTS);
}

void matrix_scan_user(void) {
    ai_agent_macropad_task(NUM_MACROPAD_SLOTS);
}

// raw_hid_receive_kb() only exists once keychron_raw_hid.c.patch is
// applied — see this file's top comment. Peeks at VIA remaps, then
// claims this protocol's own MSG_* reports. Returning true fully
// claims the report (kc_raw_hid_rx passes this straight back to
// via_command_kb / via.c); false lets VIA's normal handling run.
bool raw_hid_receive_kb(uint8_t *data, uint8_t length) {
    if (length >= 11 && data[0] == MSG_BANK_STATE) {
        if (data[1] < 3 && data[3] > 0 && data[4] <= 2 && data[5] <= 2 && data[6] <= 2) {
            selected_bank = data[1];
            bank_pages = data[3];
            for (uint8_t i = 0; i < 3; i++) bank_attention[i] = data[4 + i];
            permission_token = 0;
            for (uint8_t i = 0; i < 4; i++) permission_token |= (uint32_t)data[7 + i] << (8 * i);
        }
        return true;
    }
    ai_agent_macropad_track_via_remap(data, length, AI_AGENT_KEY_0, NUM_MACROPAD_SLOTS);
    return ai_agent_macropad_raw_hid_receive(data, length, DEVICE_ID_K0_MAX, NUM_MACROPAD_SLOTS);
}

bool rgb_matrix_indicators_user(void) {
    rgb_matrix_set_color_all(0, 0, 0);
    ai_agent_macropad_paint_indicators(NUM_MACROPAD_SLOTS);
    const uint8_t macro_leds[3] = {4, 9, 14};
    uint8_t value = rgb_matrix_get_val();
    bool blink_on = (timer_read32() / 500) % 2 == 0;
    for (uint8_t i = 0; i < 3; i++) {
        bool selected = selected_bank == i;
        if (bank_attention[i] == 2 && blink_on) {
            rgb_matrix_set_color(macro_leds[i], value, value / 2, 0);
        } else if (bank_attention[i] == 1) {
            uint8_t level = selected ? value : value / 2;
            rgb_matrix_set_color(macro_leds[i], level, (uint16_t)level * 2 / 3, 0);
        } else if (selected) {
            rgb_matrix_set_color(macro_leds[i], value / 3, value / 3, value / 3);
        }
    }
    if (bank_pages > 1) rgb_matrix_set_color(18, 0, value / 3, value / 2);
    return false;
}
