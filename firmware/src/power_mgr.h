#ifndef POWER_MGR_H
#define POWER_MGR_H

#include <Arduino.h>

// Central CPU-frequency + battery-telemetry policy. Radios keep their own
// on/off logic (wifi_sync/ble_sync); this module only owns "how fast should
// the CPU run right now" and "what's the battery at" -- both of which every
// other module can ask without knowing about each other.
//
// The floor is 80 MHz, never lower: both radios (WiFi and BLE share the
// one physical radio) require >=80 MHz APB timing; 40 MHz would silently
// break them.
enum class PowerProfile {
    LOW_80,    // idle / everything not listed below
    HIGH_240,  // recording (codec+SD writes), WiFi file streaming, L2CAP transfers
};

void power_mgr_init();

// Applies the CPU frequency for the profile (no-op if already there).
// `why` lands in the serial log so transitions are auditable.
void power_mgr_set_profile(PowerProfile p, const char *why);

// Battery voltage via GPIO4 through the board's 200K/200K divider (real
// voltage = 2x the ADC reading -- Waveshare ESP32-S3-ePaper-1.54). Median
// of several samples; voltage-only estimate (no fuel gauge on this board),
// so the derived percentage is approximate and sags under load -- callers
// should prefer reading it while idle, not mid-recording.
uint32_t power_mgr_battery_mv();
int power_mgr_battery_pct(); // 0-100, 4.2V=100 down a LiPo curve to 3.3V=0

// Best-effort "is this device on external power (charging/plugged into a
// laptop) right now" -- there's no dedicated VBUS/charge-status GPIO
// documented for this board, so this infers it from the battery voltage
// itself: a charging LiPo is held pinned near its top-of-charge voltage by
// the charge IC, where a discharging one under normal use only visits that
// range briefly right after a full charge. Debounced (must hold above the
// threshold for a sustained window) to avoid a momentary reading flipping
// this mid-use. Not a substitute for real VBUS sensing -- if this board
// grows a wired charge-status pin later, replace this with that.
//
// When true, callers should skip their own battery-saving behavior (WiFi
// radio-off, codec power-down, CPU throttling, deep sleep) -- there's no
// reason to save battery power that isn't being spent from the battery.
bool power_mgr_on_external_power();

// Call periodically (e.g. from indicatorTask's 1s tick) to update the
// external-power debounce state.
void power_mgr_tick();

// --- duty-cycled deep sleep (Phase 2, battery) ----------------------------
// Flag-gated -- set to 0 to fully disable and fall back to Phase 1 behavior
// only (never sleeps).
#define POWER_MGR_ENABLE_DEEP_SLEEP 1

// Call from any button click, BLE connect, or HTTP hit to reset the idle
// clock -- mirrors wifi_sync's noteHttpActivity() but for the sleep
// decision rather than the radio-session one.
void power_mgr_note_activity();

// True once IDLE_SLEEP_TIMEOUT_MS has passed with no activity noted. The
// caller (main.cpp) is responsible for also checking it's safe to sleep
// right now (not recording, not mid radio-session, nothing unsynced) --
// this function only knows about the idle clock, not app state.
bool power_mgr_idle_timeout_reached();

// Configures both wake sources (BOOT/PWR buttons via ext1, and a timer for
// the duty-cycled BLE reconnect window) and calls esp_deep_sleep_start().
// Never returns -- the next code that runs is setup() after reboot.
// Caller should already have drawn the sleeping face and powered down the
// EPD/audio rails before calling this.
[[noreturn]] void power_mgr_enter_deep_sleep();

// Distinguishes what woke the chip -- call once at the very top of
// setup(), before any hardware re-init, so main.cpp can fast-dispatch
// (button wake -> go straight into that button's action; timer wake ->
// skip the EPD re-init entirely and just open a short BLE window) instead
// of running the full cold-boot idle sequence every time.
enum class WakeCause { COLD_BOOT, TIMER, BUTTON_PWR, BUTTON_BOOT, BUTTON_BOTH };
WakeCause power_mgr_wake_cause();

// True once it's time to go back to sleep: either the normal 30-min idle
// timeout (any boot not woken by the duty-cycle timer), or -- for a TIMER
// wake specifically -- a much shorter ~60s reconnect window, since that
// wake's whole purpose was "give BLE a brief chance to reconnect," not
// "stay awake." Caller (main.cpp) still gates this on app state (not
// recording, no BLE central connected, radio not mid-session, not on
// external power) -- this function only knows about elapsed time.
bool power_mgr_should_return_to_sleep();

#endif
