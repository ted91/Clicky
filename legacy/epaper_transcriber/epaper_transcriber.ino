#include <Arduino.h>
#include "user_config.h"
#include "src/power/board_power_bsp.h"
#include "src/i2c_bsp/i2c_bsp.h"
#include "src/display/epaper_driver_bsp.h"
#include "src/button_bsp/button_bsp.h"
#include "audio_bsp.h"
#include "src/sdcard/sdcard_bsp.h"

#include "face.h"
#include "recorder.h"
#include "wifi_sync.h"
#include "ble_sync.h"

// PWR button: single click toggles recording on/off (same click sound on
// both start and stop, see recorder.cpp's playClick()).
// Holding PWR for ~3s to power the board on/off is handled entirely by the
// board's own power circuit before firmware is even running -- nothing to
// do here for that.
//
// BOOT button: single click cycles through the status faces (DND/HI/NOPE/
// BUSY/FOCUS, wrapping back to the default smiley); a long press jumps
// straight back to the default smiley from anywhere.

enum class AppState { IDLE, RECORDING, SYNCING };

static epaper_driver_display *s_epd = nullptr;
static board_power_bsp_t s_power(EPD_PWR_PIN, Audio_PWR_PIN, VBAT_PWR_PIN);
static AppState s_state = AppState::IDLE;

static void initHardware() {
    s_power.POWEER_EPD_ON();
    s_power.POWEER_Audio_ON();
    i2c_master_Init();

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

    user_button_init();

    audio_bsp_init();
    audio_play_init();
    recorder_init();

    sdcard_init();

    wifi_sync_init(); // best-effort; fine if your router won't let this join (2.4GHz-only)
    ble_sync_init();  // primary sync path when WiFi STA can't connect
}

// Polls the PWR button's event group (set by button_bsp's ISR-driven multi
// click detector) and drives the recording state machine. Single click
// toggles: IDLE -> RECORDING -> (stop) -> SYNCING -> IDLE.
static void buttonTask(void *arg) {
    for (;;) {
        EventBits_t bits = xEventGroupWaitBits(pwr_groups, set_bit_all, pdTRUE, pdFALSE, pdMS_TO_TICKS(200));

        if (get_bit_button(bits, 0)) { // single click
            if (s_state == AppState::IDLE) {
                Serial.println("main: PWR click -> start recording");
                s_state = AppState::RECORDING;
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

// Polls the BOOT button's event group: single click cycles the status face,
// long press clears it back to the default smiley.
static void bootButtonTask(void *arg) {
    for (;;) {
        EventBits_t bits = xEventGroupWaitBits(boot_groups, set_bit_all, pdTRUE, pdFALSE, pdMS_TO_TICKS(200));

        if (get_bit_button(bits, 0)) { // single click
            Status s = face_next_status();
            Serial.printf("main: BOOT click -> status %d\n", (int)s);
        } else if (get_bit_button(bits, 1)) { // long-press-start
            Serial.println("main: BOOT long-press -> clear status");
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
            if (recorder_last_was_sd()) {
                Serial.printf("main: recording saved to SD (%s), ready for phone sync\n", recorder_last_file().c_str());
            } else {
                Serial.println("main: recording saved to PSRAM (no SD card), fetch it at /rec?name=ram_recording.wav");
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
        face_update(s_state == AppState::RECORDING);
        vTaskDelay(pdMS_TO_TICKS(200));
    }
}

static void wifiTask(void *arg) {
    for (;;) {
        wifi_sync_tick();
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}

// Keeps the bottom BLE/SYNC checkbox indicators current, independent of
// faceTask's own redraw cycle -- connection/transfer state can change
// without the recording/status state changing at all.
static void indicatorTask(void *arg) {
    for (;;) {
        bool connected = ble_sync_is_connected() || wifi_sync_is_connected();
        bool syncing = ble_sync_is_transferring() || wifi_sync_is_transferring();
        face_update_indicators(connected, syncing);
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

void setup() {
    Serial.begin(115200);
    initHardware();

    xTaskCreatePinnedToCore(buttonTask, "buttonTask", 4 * 1024, NULL, 5, NULL, 1);
    xTaskCreatePinnedToCore(bootButtonTask, "bootButtonTask", 4 * 1024, NULL, 5, NULL, 1);
    xTaskCreatePinnedToCore(syncWatchTask, "syncWatchTask", 3 * 1024, NULL, 3, NULL, 1);
    xTaskCreatePinnedToCore(faceTask, "faceTask", 4 * 1024, NULL, 2, NULL, 1);
    xTaskCreatePinnedToCore(wifiTask, "wifiTask", 4 * 1024, NULL, 2, NULL, 0);
    xTaskCreatePinnedToCore(indicatorTask, "indicatorTask", 3 * 1024, NULL, 1, NULL, 1);
}

void loop() {
    vTaskDelay(pdMS_TO_TICKS(1000));
}
