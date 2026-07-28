#ifndef FACE_H
#define FACE_H

#include "src/display/epaper_driver_bsp.h"

// BOOT-button-selectable status faces, shown when idle (recording always
// takes visual priority regardless of status). NONE = default idle smiley.
enum class Status {
    NONE,
    PAIRING,     // "PAIR"  -- BLE discoverable for first-time pairing (see
                 //           ble_sync_start_pairing(); main.cpp starts/stops
                 //           advertising when this status is entered/left)
    CUSTOM,      // Synthetic marker, not a real face -- means "currently
                 // showing one of the user-defined custom statuses loaded
                 // from NVS" (see face_set_custom_status/
                 // face_current_custom_index). face_next_status() cycles
                 // through these after PAIRING, wrapping back to NONE once
                 // past the last one.
};

// Up to this many user-defined custom statuses (Settings dashboard ->
// synced over BLE, see ble_sync.cpp's "SETSTATUS "/"CLEARSTATUSES"
// commands) persist in NVS across reboots.
static const int MAX_CUSTOM_STATUSES = 5;

// Which eye style a custom status uses -- round/happy, closed/sleepy, X/out,
// narrow/squint, picked at runtime instead of hardcoded per-status.
// Must match the icon_key values
// ble_device_client.py sends ("round"=0, "closed"=1, "x"=2, "narrow"=3).
enum class CustomStatusIcon : uint8_t {
    ROUND = 0,
    CLOSED = 1,
    X = 2,
    NARROW = 3,
};

// Writes one custom-status slot (persisted to NVS immediately) -- called
// from ble_sync.cpp's "SETSTATUS <index> <icon>|<text>" command handler.
// No-ops silently if index is out of [0, MAX_CUSTOM_STATUSES).
void face_set_custom_status(int index, uint8_t icon, const char *text);

// Wipes all custom statuses (both the in-memory cache and NVS) -- called
// from ble_sync.cpp's "CLEARSTATUSES" command, always sent right before a
// fresh batch of SETSTATUS commands so a shrunk list doesn't leave stale
// trailing slots behind.
void face_clear_custom_statuses();

// -1 when face_current_status() isn't Status::CUSTOM; otherwise the index
// into the loaded custom-status list currently being shown.
int face_current_custom_index();

void face_init(epaper_driver_display *driver);

// Call frequently; only actually redraws the panel when something visible
// changed, since e-paper refreshes are slow (~0.3s partial) and there's no
// point re-pushing the same image every tick.
void face_update(bool recording);

// BOOT button: single click advances to the next status in the list
// (wrapping back to NONE after the last one); returns the new status.
Status face_next_status();

// Current status without advancing it (e.g. to check "are we leaving
// PAIRING" before calling face_next_status()).
Status face_current_status();

// BOOT button long-press: jumps straight back to NONE (default smiley)
// regardless of current status.
void face_clear_status();

// Jumps straight to the first-time setup screen (Status::PAIRING) --
// called once at boot when the device isn't paired yet (see main.cpp's
// setup()), so a first-time user sees instructions immediately instead of
// needing to know to press BOOT at all.
void face_show_pairing_setup();

// Called once at boot (ble_sync_init) and again the moment pairing
// succeeds (ble_sync's onConnect) -- while unpaired, Status::PAIRING (the
// first-time setup screen) is included in the BOOT-cycle and shown
// automatically at boot; once paired, it's dropped from the cycle
// entirely so a returning user never lands on it again.
void face_set_paired(bool paired);

// Small always-on-screen "BLE"/"SYNC" checkbox indicators pinned to the
// bottom of the panel, independent of face_update()'s own redraw cycle —
// call this periodically (e.g. every 1-2s) so connection/sync state stays
// current even when nothing else visible has changed.
void face_update_indicators(bool bleConnected, bool syncActive);

// Phone-style battery badge (icon + percentage), pinned to the top-right
// corner, always visible regardless of scene -- unlike the bottom strip's
// battery text (drawIndicatorStrip), which only shows below 30% to avoid
// clutter. Same "call often, only redraws on change" contract as
// face_update_indicators(); pct is 0-100 (see power_mgr_battery_pct()).
// Reads persist across scene redraws the same way the bottom strip does.
void face_update_battery(int pct);

// AI-pager notification surface. face_show_notification() stores the
// message and the next face_update() tick draws it full-screen (recording
// still outranks it). It stays up until face_dismiss_notification() -- no
// auto-dismiss; the BOOT button click is the dismissal gesture while one
// is showing (see main.cpp's bootButtonTask). Title is truncated to ~47
// chars, body to ~127. Safe to call from the BLE callback context: it
// only writes state; all drawing happens on faceTask's own tick.
void face_show_notification(const char *title, const char *body);
bool face_notification_active();
void face_dismiss_notification();

#endif
