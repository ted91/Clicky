#include "power_mgr.h"
#include "user_config.h"
#include "esp_sleep.h"

// GPIO4 = VBAT sense through a 200K/200K divider (board schematic:
// Waveshare ESP32-S3-ePaper-1.54). Not in user_config.h upstream -- defined
// here since this is the only module that reads it.
static const int VBAT_ADC_PIN = 4;

static PowerProfile s_profile = PowerProfile::HIGH_240; // boot default is 240

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

void power_mgr_init() {
    analogReadResolution(12);
    // Drop to the 80 MHz baseline once boot is done -- everything cycle-
    // hungry (recording, WiFi streaming) bumps back up explicitly.
    power_mgr_set_profile(PowerProfile::LOW_80, "boot complete");
}

void power_mgr_set_profile(PowerProfile p, const char *why) {
    if (p == s_profile) return;
    s_profile = p;
    int mhz = (p == PowerProfile::HIGH_240) ? 240 : 80;
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

void power_mgr_tick() {
    uint32_t mv = power_mgr_battery_mv();
    uint32_t now = millis();
    if (mv >= EXTERNAL_POWER_THRESHOLD_MV) {
        s_belowThresholdSinceMs = 0;
        if (s_aboveThresholdSinceMs == 0) s_aboveThresholdSinceMs = now;
        if (!s_onExternalPower && now - s_aboveThresholdSinceMs > EXTERNAL_POWER_DEBOUNCE_MS) {
            s_onExternalPower = true;
            Serial.println("power: external power detected (battery voltage held high) -- disabling battery-saving behavior");
        }
    } else {
        s_aboveThresholdSinceMs = 0;
        if (s_belowThresholdSinceMs == 0) s_belowThresholdSinceMs = now;
        if (s_onExternalPower && now - s_belowThresholdSinceMs > EXTERNAL_POWER_DEBOUNCE_MS) {
            s_onExternalPower = false;
            Serial.println("power: external power no longer detected -- resuming battery-saving behavior");
        }
    }
}

// --- duty-cycled deep sleep ------------------------------------------------
static uint32_t s_lastActivityMs = 0;
static const uint32_t IDLE_SLEEP_TIMEOUT_MS = 2 * 60 * 1000; // 2 min
static const uint64_t TIMER_WAKE_INTERVAL_US = 10ULL * 60 * 1000000; // 10 min

void power_mgr_note_activity() {
    s_lastActivityMs = millis();
}

bool power_mgr_idle_timeout_reached() {
#if !POWER_MGR_ENABLE_DEEP_SLEEP
    return false;
#else
    if (s_lastActivityMs == 0) return false; // not yet initialized this boot
    return millis() - s_lastActivityMs > IDLE_SLEEP_TIMEOUT_MS;
#endif
}

void power_mgr_enter_deep_sleep() {
    // BOOT (GPIO0) and PWR (GPIO18) both wake the chip -- ANY_LOW means
    // either pin going low (they're active-low with pull-ups) triggers a
    // wake, matching their normal pressed behavior.
    esp_sleep_enable_ext1_wakeup((1ULL << 0) | (1ULL << 18), ESP_EXT1_WAKEUP_ANY_LOW);
    esp_sleep_enable_timer_wakeup(TIMER_WAKE_INTERVAL_US);
    Serial.println("power: entering deep sleep (wake on BOOT/PWR button or in ~10min)");
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
