#include "power_mgr.h"
#include "user_config.h"
#include "esp_sleep.h"
#include <HWCDC.h> // power_mgr_usb_host_attached() -- see its own doc comment

// GPIO4 = VBAT sense through a 200K/200K divider (board schematic:
// Waveshare ESP32-S3-ePaper-1.54). Not in user_config.h upstream -- defined
// here since this is the only module that reads it.
static const int VBAT_ADC_PIN = 4;

static PowerProfile s_profile = PowerProfile::HIGH_240; // boot default is 240
static uint32_t s_bootMs = 0; // stamped in power_mgr_init(), every boot -- see power_mgr_boot_grace_period_active()

// External-power inference: a charging LiPo is held near the top of its
// charge curve by the charge IC; a discharging one under normal use only
// passes through that range briefly. Debounced over a sustained window so
// a momentary high reading (just came off a full charge, still above the
// threshold for a bit) doesn't flip this before the pack actually starts
// dropping. Not real VBUS sensing -- see power_mgr.h.
static const uint32_t EXTERNAL_POWER_THRESHOLD_MV = 4150;
static const uint32_t EXTERNAL_POWER_DEBOUNCE_MS = 20000;
static bool s_onExternalPower = false;
static uint32_t s_aboveThresholdSinceMs = 0;
static uint32_t s_belowThresholdSinceMs = 0;

// Live-confirmed incident: a fully-charged LiPo rests near/above this
// threshold for a long time after actually being unplugged (LiPo voltage
// sags very slowly at rest right after a full charge, unlike under load) --
// a user left the device on battery overnight and it stayed on WiFi with
// sleep completely blocked the entire time, draining ~80% of the battery.
// Both the sleep-eligibility gate (main.cpp's sleepWatchTask) and the
// WiFi-radio-off gate (wifi_sync.cpp's radioTick) were checking the raw
// power_mgr_on_external_power() signal, which has no way to self-correct
// once the reading gets stuck above threshold. This ceiling bounds that:
// once "on external power" has read continuously true for this long, stop
// trusting it for gating purposes (battery-saving resumes) even though the
// voltage may still read high -- see power_mgr_external_power_override_active().
static const uint32_t EXTERNAL_POWER_OVERRIDE_CEILING_MS = 30 * 60 * 1000; // 30min
static uint32_t s_onExternalPowerSinceMs = 0;

void power_mgr_init() {
    s_bootMs = millis();
    analogReadResolution(12);
    // Drop to the 80 MHz baseline once boot is done -- everything cycle-
    // hungry (recording, WiFi streaming) bumps back up explicitly.
    power_mgr_set_profile(PowerProfile::LOW_80, "boot complete");
}

void power_mgr_set_profile(PowerProfile p, const char *why) {
    if (p == s_profile) return;
    s_profile = p;
    int mhz = (p == PowerProfile::HIGH_240) ? 240 : (p == PowerProfile::MEDIUM_160) ? 160 : 80;
    setCpuFrequencyMhz(mhz);
    Serial.printf("power: cpu -> %d MHz (%s)\n", mhz, why ? why : "");
}

uint32_t power_mgr_battery_mv() {
    // Median-of-9 to reject ADC noise spikes; the divider halves VBAT, so
    // double the reading back.
    uint32_t samples[9];
    for (int i = 0; i < 9; i++) samples[i] = analogReadMilliVolts(VBAT_ADC_PIN);
    // insertion sort -- 9 elements
    for (int i = 1; i < 9; i++) {
        uint32_t v = samples[i];
        int j = i - 1;
        while (j >= 0 && samples[j] > v) { samples[j + 1] = samples[j]; j--; }
        samples[j + 1] = v;
    }
    return samples[4] * 2;
}

bool power_mgr_on_external_power() {
    return s_onExternalPower;
}

bool power_mgr_external_power_override_active() {
    if (!s_onExternalPower) return false;
    if (s_onExternalPowerSinceMs == 0) return true; // just flipped this tick, not yet timed
    return millis() - s_onExternalPowerSinceMs < EXTERNAL_POWER_OVERRIDE_CEILING_MS;
}

void power_mgr_tick() {
    uint32_t mv = power_mgr_battery_mv();
    uint32_t now = millis();
    if (mv >= EXTERNAL_POWER_THRESHOLD_MV) {
        s_belowThresholdSinceMs = 0;
        if (s_aboveThresholdSinceMs == 0) s_aboveThresholdSinceMs = now;
        if (!s_onExternalPower && now - s_aboveThresholdSinceMs > EXTERNAL_POWER_DEBOUNCE_MS) {
            s_onExternalPower = true;
            s_onExternalPowerSinceMs = now;
            Serial.println("power: external power detected (battery voltage held high) -- disabling battery-saving behavior");
        }
    } else {
        s_aboveThresholdSinceMs = 0;
        if (s_belowThresholdSinceMs == 0) s_belowThresholdSinceMs = now;
        if (s_onExternalPower && now - s_belowThresholdSinceMs > EXTERNAL_POWER_DEBOUNCE_MS) {
            s_onExternalPower = false;
            s_onExternalPowerSinceMs = 0;
            Serial.println("power: external power no longer detected -- resuming battery-saving behavior");
        }
    }
}

// --- duty-cycled deep sleep ------------------------------------------------
static uint32_t s_lastActivityMs = 0;
// Real light sleep (see below) wakes near-instantly with no reboot, so
// there's no real cost to sleeping the moment nothing's happening -- per
// explicit direction, the device should be in light sleep whenever idle,
// not just after a multi-minute wait. 5s (not 0) so a brief pause between
// two quick actions doesn't cost a sleep/wake cycle for no reason.
static const uint32_t IDLE_SLEEP_TIMEOUT_MS = 5 * 1000; // 5 sec
// Light sleep's own periodic TIMER wake -- used for the routine "check for
// pending sync opportunities" cycle (see main.cpp's sleepWatchTask). Kept
// separate from deep sleep's own interval below: light sleep's continuous
// standby baseline (order of a few hundred uA on this SoC) already
// dominates its energy cost, so the exact wake cadence matters far less
// than how long the device stays in this tier at all -- 10min is cheap to
// add on top of that baseline.
static const uint64_t TIMER_WAKE_INTERVAL_US = 10ULL * 60 * 1000000; // 10 min

// Shorter cadence used ONLY while wifi_sync_has_pending_recordings() is
// true -- there's an actual reason to be eager about finding a sync
// opportunity, so check more often than the general 10min cadence. Falls
// back to TIMER_WAKE_INTERVAL_US once nothing's pending.
static const uint64_t PENDING_TIMER_WAKE_INTERVAL_US = 5ULL * 60 * 1000000; // 5 min

// Deep sleep's OWN periodic wake interval -- deliberately longer than
// light sleep's. Reasoned comparison: deep sleep's baseline (~10-25uA) is
// already near-zero, so shortening its *sleeping* time buys almost
// nothing, but each deep-sleep wake is a full reboot (display/BLE
// re-init), objectively more expensive per-event than light sleep's
// near-instant resume -- so the real lever for this tier is making the
// wake events themselves rarer, not more frequent. (ESP32-S3 general
// figures, not measured on this exact board -- see the real battery test
// this was reasoned alongside.)
static const uint64_t DEEP_SLEEP_TIMER_WAKE_INTERVAL_US = 20ULL * 60 * 1000000; // 20 min

// Light sleep (routine idle tier) falls back to a real deep sleep after
// this much continuous idle time -- explicit threshold, not a guess: light
// sleep's actual battery draw isn't proven equal to deep sleep's on this
// board, so don't pay it indefinitely once nobody's using the device.
static const uint32_t DEEP_SLEEP_FALLBACK_MS = 20 * 60 * 1000; // 20 min

// Never sleep within this long of boot, regardless of activity -- belt-
// and-braces for the battery-only case: a BLE connect from the Mac's own
// poll cycle (see ble_sync.cpp's onConnect(), which calls
// power_mgr_note_activity()) could otherwise arm the 5s idle countdown
// before there's any real chance to intervene.
static const uint32_t BOOT_GRACE_PERIOD_MS = 30 * 1000; // 30 sec

void power_mgr_note_activity() {
    s_lastActivityMs = millis();
}

// Real "is a USB host physically attached" check -- live-confirmed bug:
// esp_light_sleep_start() gates the clock this board's native USB Serial/
// JTAG peripheral runs on (this board has no separate UART bridge chip --
// see wifi_sync_reinit_after_light_sleep()'s sibling comments elsewhere in
// this codebase for the same "no bridge chip" fact applied to lwIP), so
// the device stays USB-enumerated but silently stops answering
// mid-flash/mid-monitor. Nothing was gating sleep on USB presence at all
// before this. HWCDC::isPlugged() (not isConnected()/isCDC_Connected(),
// and NOT `if (Serial)`) is deliberately used -- it wraps IDF's timer-
// based usb_serial_jtag_is_connected(), a physical-attach check, not
// "has a host actually opened the CDC port yet." The Arduino core's own
// HWCDC.h carries a comment that the ISR-based alternative interferes
// with esptool uploads -- this is the IDF-5.1 timer-based one specifically
// chosen to avoid that class of bug.
bool power_mgr_usb_host_attached() {
    return HWCDC::isPlugged();
}

bool power_mgr_boot_grace_period_active() {
    return millis() - s_bootMs < BOOT_GRACE_PERIOD_MS;
}

bool power_mgr_idle_timeout_reached() {
#if !POWER_MGR_ENABLE_DEEP_SLEEP
    return false;
#else
    if (s_lastActivityMs == 0) return false; // not yet initialized this boot
    return millis() - s_lastActivityMs > IDLE_SLEEP_TIMEOUT_MS;
#endif
}

bool power_mgr_deep_sleep_fallback_due() {
    if (s_lastActivityMs == 0) return false; // not yet initialized this boot
    return millis() - s_lastActivityMs > DEEP_SLEEP_FALLBACK_MS;
}

void power_mgr_enter_deep_sleep() {
    // BOOT (GPIO0) and PWR (GPIO18) both wake the chip -- ANY_LOW means
    // either pin going low (they're active-low with pull-ups) triggers a
    // wake, matching their normal pressed behavior.
    esp_sleep_enable_ext1_wakeup((1ULL << 0) | (1ULL << 18), ESP_EXT1_WAKEUP_ANY_LOW);
    esp_sleep_enable_timer_wakeup(DEEP_SLEEP_TIMER_WAKE_INTERVAL_US);
    Serial.println("power: entering deep sleep (wake on BOOT/PWR button or in ~20min)");
    Serial.flush();
    esp_deep_sleep_start();
}

WakeCause power_mgr_wake_cause() {
    esp_sleep_wakeup_cause_t cause = esp_sleep_get_wakeup_cause();
    if (cause == ESP_SLEEP_WAKEUP_TIMER) return WakeCause::TIMER;
    if (cause == ESP_SLEEP_WAKEUP_EXT1) {
        uint64_t pins = esp_sleep_get_ext1_wakeup_status();
        bool pwr = pins & (1ULL << 18);
        bool boot = pins & (1ULL << 0);
        if (pwr && boot) return WakeCause::BUTTON_BOTH;
        if (pwr) return WakeCause::BUTTON_PWR;
        if (boot) return WakeCause::BUTTON_BOOT;
    }
    return WakeCause::COLD_BOOT;
}

bool power_mgr_should_return_to_sleep() {
#if !POWER_MGR_ENABLE_DEEP_SLEEP
    return false;
#else
    static const uint32_t TIMER_WAKE_WINDOW_MS = 60000;
    if (power_mgr_wake_cause() == WakeCause::TIMER) {
        return millis() > TIMER_WAKE_WINDOW_MS;
    }
    return power_mgr_idle_timeout_reached();
#endif
}

WakeCause power_mgr_enter_light_sleep(bool pendingSync) {
#if !POWER_MGR_ENABLE_LIGHT_SLEEP
    return WakeCause::COLD_BOOT; // flag off -- caller should not have called this
#else
    // Same wake sources as deep sleep -- ext1/RTC-IO wakeup is documented
    // as reusable verbatim across both sleep modes (esp_sleep.h's own doc
    // comments), no separate GPIO-wake API needed for light sleep here.
    esp_sleep_enable_ext1_wakeup((1ULL << 0) | (1ULL << 18), ESP_EXT1_WAKEUP_ANY_LOW);
    uint64_t interval = pendingSync ? PENDING_TIMER_WAKE_INTERVAL_US : TIMER_WAKE_INTERVAL_US;
    esp_sleep_enable_timer_wakeup(interval);
    Serial.printf("power: entering light sleep (wake on BOOT/PWR button or in ~%lumin)\n",
                  (unsigned long)(interval / 60000000ULL));
    Serial.flush();
    uint32_t t0 = millis();
    esp_err_t rc = esp_light_sleep_start(); // BLOCKS here (whole chip halts) until a wake source fires
    uint32_t elapsedMs = millis() - t0;
    WakeCause cause = power_mgr_wake_cause();
    Serial.printf("timing: light_sleep returned rc=%d cause=%d elapsed_ms=%lu\n",
                  (int)rc, (int)cause, (unsigned long)elapsedMs);
    return cause;
#endif
}

int power_mgr_battery_pct() {
    uint32_t mv = power_mgr_battery_mv();
    // Piecewise-linear LiPo discharge curve (open-circuit-ish, light load).
    // Voltage-only estimate -- honest to within ~10%, worse under load.
    struct { uint32_t mv; int pct; } curve[] = {
        {4200, 100}, {4060, 90}, {3980, 80}, {3920, 70}, {3870, 60},
        {3820, 50}, {3790, 40}, {3770, 30}, {3730, 20}, {3660, 10}, {3300, 0},
    };
    if (mv >= curve[0].mv) return 100;
    for (size_t i = 1; i < sizeof(curve) / sizeof(curve[0]); i++) {
        if (mv >= curve[i].mv) {
            // interpolate between curve[i-1] and curve[i]
            uint32_t hiMv = curve[i - 1].mv, loMv = curve[i].mv;
            int hiPct = curve[i - 1].pct, loPct = curve[i].pct;
            return loPct + (int)((mv - loMv) * (uint32_t)(hiPct - loPct) / (hiMv - loMv));
        }
    }
    return 0;
}
