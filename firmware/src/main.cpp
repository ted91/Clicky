#include <Arduino.h>
#include "user_config.h"
#include "src/power/board_power_bsp.h"
#include "src/i2c_bsp/i2c_bsp.h"
#include "src/display/epaper_driver_bsp.h"
#include "src/button_bsp/button_bsp.h"
#include "audio_bsp.h"
#include "sdcard/sdcard_bsp.h"

#include "face.h"
#include "recorder.h"
#include "wifi_sync.h"
#include "ble_sync.h"
#include "power_mgr.h"
#include "esp_ota_ops.h"

// PWR button: single click toggles recording on/off (same click sound on
// both start and stop, see recorder.cpp's playClick()).
// Holding PWR for ~3s to power the board on/off is handled entirely by the
// board's own power circuit before firmware is even running -- nothing to
// do here for that.
//
// BOOT button: while a recording is live, single click CANCELS it -- the
// audio is discarded entirely (SD file deleted / PSRAM never offered, see
// recorder_cancel()), with a descending tone instead of the save click.
// Otherwise, single click cycles through the status faces (DND/HI/NOPE/
// BUSY/FOCUS, wrapping back to the default smiley); a long press jumps
// straight back to the default smiley from anywhere.

enum class AppState { IDLE, RECORDING, SYNCING };

static epaper_driver_display *s_epd = nullptr;
static board_power_bsp_t s_power(EPD_PWR_PIN, Audio_PWR_PIN, VBAT_PWR_PIN);
static AppState s_state = AppState::IDLE;

// initDisplay=false skips the e-paper hardware entirely -- used for a
// timer-triggered wake from deep sleep (see power_mgr's duty-cycled
// sleep), whose only purpose is a brief BLE reconnect window. The panel
// was left powered-off with its last real content still visible (e-paper
// holds an image unpowered), so re-initializing it would cost a visible
// full-refresh flash for a wake nobody's necessarily even looking at.
static void initHardware(bool initDisplay) {
    // Latches battery power on -- until this runs, the PWR button's own
    // hold-to-boot circuit is the only thing keeping the board alive; this
    // is what lets it stay powered after the button is released. Was never
    // called before (board happened to stay powered anyway on this
    // particular boot path) -- calling it explicitly here is defensive,
    // not a behavior change under normal use.
    s_power.VBAT_POWER_ON();
    i2c_master_Init();

    if (initDisplay) {
        s_power.POWEER_EPD_ON();
        custom_lcd_spi_t cfg = {};
        cfg.cs = EPD_CS_PIN;
        cfg.dc = EPD_DC_PIN;
        cfg.rst = EPD_RST_PIN;
        cfg.busy = EPD_BUSY_PIN;
        cfg.mosi = EPD_MOSI_PIN;
        cfg.scl = EPD_SCK_PIN;
        cfg.spi_host = EPD_SPI_NUM;
        cfg.buffer_len = 5000;
        s_epd = new epaper_driver_display(EPD_WIDTH, EPD_HEIGHT, cfg);
        s_epd->EPD_Init();
        s_epd->EPD_Clear();
        s_epd->EPD_DisplayPartBaseImage();
        s_epd->EPD_Init_Partial();
        face_init(s_epd);
        face_update(false); // draw the idle smiley immediately
    }

    s_power.POWEER_Audio_ON(); // audio_bsp_init needs the rail up; powered back down below
    user_button_init();

    audio_bsp_init();
    audio_play_init();
    recorder_init();

    sdcard_init();

    wifi_sync_init(); // best-effort; fine if your router won't let this join (2.4GHz-only)
    ble_sync_init();  // primary sync path when WiFi STA can't connect

    // First-time setup: an unpaired device shows the setup screen (and
    // starts fast/discoverable BLE advertising) right from boot, instead
    // of sitting at the idle smiley waiting for someone to know to press
    // BOOT. Only meaningful with a display up (initHardware(initDisplay))
    // -- a timer-wake-from-sleep boot never has one, but a device that's
    // still unpaired never reaches battery-saving sleep anyway (see
    // sleepWatchTask's !ble_sync_is_connected() eligibility check racing
    // against fast pairing advertising).
    if (s_epd && !ble_sync_is_paired()) {
        face_show_pairing_setup();
        ble_sync_start_pairing();
        face_update(false);
    }

    // Codec channels were opened by audio_play_init above (needed once so
    // volume/gain get programmed) -- close them until something actually
    // records or plays a click (battery, see audio_bsp_power_down).
    audio_bsp_power_down();
}

// Polls the PWR button's event group (set by button_bsp's ISR-driven multi
// click detector) and drives the recording state machine. Single click
// toggles: IDLE -> RECORDING -> (stop) -> SYNCING -> IDLE.
static void buttonTask(void *arg) {
    for (;;) {
        EventBits_t bits = xEventGroupWaitBits(pwr_groups, set_bit_all, pdTRUE, pdFALSE, pdMS_TO_TICKS(200));

        if (bits) power_mgr_note_activity();
        if (get_bit_button(bits, 0)) { // single click
            if (s_state == AppState::IDLE) {
                Serial.println("main: PWR click -> start recording");
                s_state = AppState::RECORDING;
                // I2S capture is DMA-driven (not CPU-throughput-bound), and
                // SD writes already go through a 16KB buffered writer (see
                // recorder.cpp's setvbuf), which is exactly the mitigation
                // against dropped samples community reports point to for
                // this chip/SD combo -- CPU frequency itself isn't the
                // bottleneck for either. Recording is the one state that
                // previously stayed at 240MHz for its whole duration (unlike
                // WiFi/BLE transfers below, already scoped to just the
                // active transfer); dropping it to the same 80MHz floor as
                // idle. Needs a real recording to confirm no audio quality
                // regression -- revert to HIGH_240 here if one turns up.
                power_mgr_set_profile(PowerProfile::LOW_80, "recording");
                recorder_start();
            } else if (s_state == AppState::RECORDING) {
                Serial.println("main: PWR click -> stop recording");
                recorder_stop();
                s_state = AppState::SYNCING;
            }
            // A click while SYNCING is ignored -- wait for the current
            // recording to finish saving before starting another.
        }
    }
}

// Polls the BOOT button's event group: while recording, single click
// cancels the recording (audio discarded entirely); otherwise single click
// cycles the status face, long press clears it back to the default smiley.
static void bootButtonTask(void *arg) {
    for (;;) {
        EventBits_t bits = xEventGroupWaitBits(boot_groups, set_bit_all, pdTRUE, pdFALSE, pdMS_TO_TICKS(200));

        if (bits) power_mgr_note_activity();
        if (get_bit_button(bits, 0)) { // single click
            if (s_state == AppState::RECORDING) {
                // A live recording claims the click: cancel and discard.
                // Status cycling resumes once the recording is over.
                Serial.println("main: BOOT click -> cancel recording (discard)");
                recorder_cancel();
                s_state = AppState::SYNCING; // syncWatchTask returns to IDLE once the task winds down
            } else if (face_notification_active()) {
                // A showing notification claims the click: dismiss it and
                // return to whatever face was underneath -- status cycling
                // only resumes once nothing is showing.
                Serial.println("main: BOOT click -> dismiss notification");
                face_dismiss_notification();
            } else {
                Status prev = face_current_status();
                if (prev == Status::PAIRING) ble_sync_stop_pairing();
                Status s = face_next_status();
                if (s == Status::PAIRING) ble_sync_start_pairing();
                Serial.printf("main: BOOT click -> status %d\n", (int)s);
            }
        } else if (get_bit_button(bits, 1)) { // long-press-start
            Serial.println("main: BOOT long-press -> clear status");
            if (face_current_status() == Status::PAIRING) ble_sync_stop_pairing();
            face_dismiss_notification();
            face_clear_status();
        }
    }
}

// Waits for the recorder task to finish writing its WAV file, then flips
// back to IDLE. The actual upload happens passively: wifi_sync's HTTP
// server just serves whatever is on the card whenever the phone asks.
static void syncWatchTask(void *arg) {
    for (;;) {
        if (s_state == AppState::SYNCING && !recorder_is_recording()) {
            // Recording (and its final WAV flush) is done -- drop back to
            // the 80 MHz baseline before the cosmetic syncing-face pause.
            // On external power there's no reason to throttle down at all.
            if (!power_mgr_on_external_power()) {
                power_mgr_set_profile(PowerProfile::LOW_80, "recording finished");
            }
            if (recorder_was_cancelled()) {
                Serial.println("main: recording cancelled, nothing kept");
            } else if (recorder_last_was_sd()) {
                Serial.printf("main: recording saved to SD (%s), ready for phone sync\n", recorder_last_file().c_str());
                // A new file exists -- open the WiFi sync window so the
                // Mac's 3s poll can find and pull it. Radio turns itself
                // off again on /synced or after 120s of silence.
                wifi_sync_radio_on("recording saved");
            } else {
                Serial.println("main: recording saved to PSRAM (no SD card), fetch it at /rec?name=ram_recording.wav");
                wifi_sync_radio_on("recording saved to PSRAM");
            }
            // Give the phone a few seconds of "syncing" face before going
            // back to idle, purely cosmetic — the file is already on disk
            // and downloadable at this point regardless of WiFi state.
            vTaskDelay(pdMS_TO_TICKS(wifi_sync_is_connected() ? 4000 : 1500));
            s_state = AppState::IDLE;
        }
        vTaskDelay(pdMS_TO_TICKS(200));
    }
}

static void faceTask(void *arg) {
    for (;;) {
        if (face_current_status() == Status::PAIRING && ble_sync_pairing_timed_out()) {
            Serial.println("main: pairing window timed out, back to normal");
            ble_sync_stop_pairing();
            face_clear_status();
        }
        face_update(s_state == AppState::RECORDING);
        vTaskDelay(pdMS_TO_TICKS(200));
    }
}

// 10ms tick only while the radio is actually on (HTTP responsiveness
// during a sync session); blocks indefinitely -- zero CPU -- while the
// radio is off, woken by wifi_sync_radio_on()'s xTaskNotifyGive.
static void wifiTask(void *arg) {
    for (;;) {
        if (!wifi_sync_radio_is_on()) {
            ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        }
        wifi_sync_tick();
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}

// Keeps the bottom BLE/SYNC checkbox indicators current, independent of
// faceTask's own redraw cycle -- connection/transfer state can change
// without the recording/status state changing at all.
static void indicatorTask(void *arg) {
    bool wasOnExternalPower = power_mgr_on_external_power();
    int batterySampleCounter = 0;
    for (;;) {
        power_mgr_tick(); // updates the debounced external-power reading
        ble_sync_reconcile_advertising(); // BLE is a backup -- stay silent while WiFi is actually up
        bool syncing = ble_sync_is_transferring() || wifi_sync_is_transferring();
        face_update_indicators(ble_sync_is_connected(), wifi_sync_is_connected(), syncing);

        // Battery % (top-right badge, see face_update_battery()) sampled
        // every 30s, not every 1s tick like BLE/sync -- power_mgr.h notes
        // the ADC reading "sags under load" and is only a voltage estimate,
        // so sampling less often avoids the badge jittering by a percent or
        // two and triggering an e-paper partial refresh for no real reason.
        if (batterySampleCounter == 0) {
            face_update_battery(power_mgr_battery_pct());
        }
        batterySampleCounter = (batterySampleCounter + 1) % 30;

        // Plugged in (charging/on a laptop): there's no battery to save,
        // so run like the device used to before any of this power work --
        // WiFi connected continuously instead of session-gated. Only acts
        // on the *transition* so it doesn't fight a session the user (or a
        // recording) is legitimately using.
        bool onExternalPower = power_mgr_on_external_power();
        if (onExternalPower && !wasOnExternalPower) {
            wifi_sync_radio_on("external power connected -- staying on like mains");
        }
        wasOnExternalPower = onExternalPower;

        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

// Duty-cycled deep sleep (battery, Phase 2 -- see power_mgr.h). Fires the
// actual sleep once every eligibility condition holds: nothing recording,
// no active WiFi sync session, no BLE central connected (paired devices
// keep advertising for exactly this reconnect check), not on external
// power (no battery to save there), and the idle clock says it's time --
// either the normal 30-min timeout, or (after a TIMER-triggered wake) the
// much shorter ~60s reconnect-check window.
static void sleepWatchTask(void *arg) {
    for (;;) {
        bool eligible = s_state == AppState::IDLE &&
                        !wifi_sync_radio_is_on() &&
                        !ble_sync_is_connected() &&
                        !power_mgr_on_external_power() &&
                        !face_notification_active();
        if (eligible && power_mgr_should_return_to_sleep()) {
            Serial.println("main: idle timeout -- entering deep sleep");
            // Leave whatever's currently on the panel (idle smiley, status
            // face) as the last image -- e-paper holds it unpowered, and
            // drawing something new here would cost a visible refresh for
            // a state nobody's necessarily watching happen.
            if (s_epd) s_power.POWEER_EPD_OFF();
            audio_bsp_power_down();
            s_power.POWEER_Audio_OFF();
            power_mgr_enter_deep_sleep(); // never returns
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

void setup() {
    Serial.begin(115200);
    // Firmware OTA safety net (see wifi_sync.cpp's /ota handler and
    // partitions.csv's comment): a freshly-flashed OTA slot boots in
    // "pending verify" state (CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE is on
    // for this board) -- reaching this line at all means the new image got
    // far enough to run real code, so mark it good. Never explicitly
    // triggering a rollback ourselves here (no self-test beyond "did we
    // get this far") -- if something further into setup() hangs/crashes
    // before this, the bootloader's own crash-loop detection is still the
    // backstop, just a slightly later one. No-op (harmless) on a normal
    // boot that was never pending verification in the first place.
    esp_ota_mark_app_valid_cancel_rollback();
    WakeCause wake = power_mgr_wake_cause();
    // Timer wake's only job is a brief BLE reconnect check -- skip the
    // e-paper hardware entirely (see initHardware's initDisplay doc) so it
    // doesn't cost a visible refresh for a wake nobody's necessarily
    // looking at. Any other wake (cold boot, a button) gets the normal
    // full display init.
    initHardware(wake != WakeCause::TIMER);

    xTaskCreatePinnedToCore(buttonTask, "buttonTask", 4 * 1024, NULL, 5, NULL, 1);
    xTaskCreatePinnedToCore(bootButtonTask, "bootButtonTask", 4 * 1024, NULL, 5, NULL, 1);
    xTaskCreatePinnedToCore(syncWatchTask, "syncWatchTask", 3 * 1024, NULL, 3, NULL, 1);
    xTaskCreatePinnedToCore(faceTask, "faceTask", 4 * 1024, NULL, 2, NULL, 1);
    TaskHandle_t wifiHandle = nullptr;
    xTaskCreatePinnedToCore(wifiTask, "wifiTask", 4 * 1024, NULL, 2, &wifiHandle, 0);
    wifi_sync_set_task_handle(wifiHandle);
    xTaskCreatePinnedToCore(indicatorTask, "indicatorTask", 3 * 1024, NULL, 1, NULL, 1);
    xTaskCreatePinnedToCore(sleepWatchTask, "sleepWatchTask", 3 * 1024, NULL, 1, NULL, 1);

    power_mgr_note_activity(); // fresh idle clock for this boot

    // Fast-dispatch: a button woke the chip specifically to do something,
    // not to sit at the idle smiley waiting for a second press. PWR wake
    // resumes straight into recording (its normal single-click action);
    // BOOT wake just needs the normal idle boot above -- its own actions
    // (status cycling, cancel) require a live face/BOOT-click context that
    // already exists post-boot, so there's nothing extra to fast-dispatch.
    if (wake == WakeCause::BUTTON_PWR) {
        Serial.println("main: woke via PWR button -- resuming recording immediately");
        s_state = AppState::RECORDING;
        power_mgr_set_profile(PowerProfile::LOW_80, "recording (resumed from sleep)");
        recorder_start();
    } else if (wake == WakeCause::TIMER) {
        Serial.println("main: woke via duty-cycle timer -- brief BLE reconnect window, no display refresh");
    }

    // Last: drop to the 80 MHz baseline now that boot is done. Recording
    // and WiFi streaming bump back to 240 for exactly their duration.
    power_mgr_init();
}

void loop() {
    vTaskDelay(pdMS_TO_TICKS(1000));
}
