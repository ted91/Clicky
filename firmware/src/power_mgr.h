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
    LOW_80,     // idle / everything not listed below
    MEDIUM_160, // recording + Jarvis capture -- live-confirmed 80MHz felt
                // sluggish for button response on battery; 240MHz works but
                // costs more battery than this needs -- see main.cpp's
                // button handlers.
    HIGH_240,   // WiFi file streaming, L2CAP transfers
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

// Same signal as power_mgr_on_external_power(), but with a safety ceiling
// (30min) on how long it can override battery-saving behavior -- use THIS,
// not the raw signal, for anything that gates sleep or the WiFi radio-off
// logic. Live-confirmed incident: a fully-charged LiPo can rest above the
// detection threshold for hours after actually being unplugged (voltage
// sags very slowly at rest right after a full charge), during which the
// raw signal has no way to self-correct -- a user's device stayed on WiFi
// with sleep permanently blocked all night, draining ~80% of the battery.
// This bounds the worst case: once "on external power" has read
// continuously true past the ceiling, sleep/radio-off resume regardless of
// what the voltage still reads, until a genuine subsequent charge cycle
// (voltage actually drops below threshold, then rises again) re-arms it.
// Purely for gating -- power_mgr_on_external_power() itself is unchanged
// and still the right call for telemetry/UI (dashboard "on power" badge),
// where continuing to report the raw reading is correct even past the
// ceiling.
bool power_mgr_external_power_override_active();

// Call periodically (e.g. from indicatorTask's 1s tick) to update the
// external-power debounce state.
void power_mgr_tick();

// True if a USB host is physically attached RIGHT NOW -- live-confirmed
// bug: esp_light_sleep_start() gates the clock this board's native USB
// Serial/JTAG peripheral runs on (no separate UART bridge chip exists on
// this board), so sleeping while a debugger/flasher is attached leaves the
// device USB-enumerated but silently unresponsive (esptool: "No serial
// data received"). Callers (sleepWatchTask) must gate BOTH sleep tiers on
// this. Backed by HWCDC::isPlugged() -> IDF's timer-based
// usb_serial_jtag_is_connected() -- a physical-attach check, not "has a
// host opened the port" (that's isConnected()/isCDC_Connected(), which
// this deliberately does NOT use).
bool power_mgr_usb_host_attached();

// True for BOOT_GRACE_PERIOD_MS after power_mgr_init() runs (i.e. after
// every boot, cold or otherwise) -- belt-and-braces so an early BLE
// connect (Mac's own poll cycle, see ble_sync.cpp's onConnect()) can't arm
// the idle-sleep countdown before there's a real chance to intervene.
bool power_mgr_boot_grace_period_active();

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

// True once a much longer stretch (DEEP_SLEEP_FALLBACK_MS, 20min) has
// passed with no REAL activity noted -- deliberately the same idle clock
// power_mgr_note_activity() drives, NOT reset by a routine light-sleep
// TIMER wake (see sleepWatchTask -- only a genuine button wake calls
// power_mgr_note_activity() again). Used to decide when to fall back from
// the routine light-sleep tier to a real deep sleep for a genuinely long
// idle period, since light sleep's battery draw isn't proven equal to
// deep sleep's on this board.
bool power_mgr_deep_sleep_fallback_due();

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

// --- light sleep (real esp_light_sleep_start(), blocking, non-rebooting) --
// Flag-gated the same way as deep sleep, for a one-line revert if this
// misbehaves on real hardware.
#define POWER_MGR_ENABLE_LIGHT_SLEEP 1

// Configures the SAME wake sources as power_mgr_enter_deep_sleep() (ext1 on
// BOOT/PWR, plus a timer) and calls esp_light_sleep_start(). Unlike deep
// sleep, this RETURNS -- the whole chip halts (both cores) until a wake
// source fires, then execution resumes right here, at this call site, with
// every FreeRTOS task/RAM/PSRAM exactly as it was (no reboot, no re-run of
// setup()). Returns the wake cause (same enum/mechanism as
// power_mgr_wake_cause(), read via hardware registers immediately after
// waking) so the caller can dispatch without a second, possibly-stale
// read. If esp_light_sleep_start() itself rejects the sleep request (e.g.
// some driver vetoed it, ESP_ERR_SLEEP_REJECT) the chip never actually
// slept -- callers should NOT assume time passed just because this
// returned; check the logged elapsed time if that matters.
//
// button_bsp's click detector is poll-based (a 5ms esp_timer, NOT a GPIO
// interrupt -- confirmed in button_bsp.c), so it will NOT see the button
// press that woke the chip on its own. Callers must dispatch on the
// returned WakeCause explicitly rather than relying on the normal button
// tasks to notice.
//
// pendingSync: true if wifi_sync_has_pending_recordings() -- uses a
// shorter timer-wake interval (5min vs the normal 10min) since there's an
// actual reason to be eager about finding a sync opportunity while
// something's waiting to go out.
WakeCause power_mgr_enter_light_sleep(bool pendingSync);

#endif
