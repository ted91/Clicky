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
#include "voice_agent.h"
#include "esp_ota_ops.h"

// PWR button: single click toggles memo recording on/off (same click sound
// on both start and stop, see recorder.cpp's playClick()). While a Jarvis
// voice command is being captured (BOOT was pressed first), PWR instead
// CANCELS that Jarvis capture -- the two buttons are independent, symmetric
// capture controls, and each one cancels the other's in-progress capture
// rather than being ignored.
// Holding PWR for ~3s to power the board on/off is handled entirely by the
// board's own power circuit before firmware is even running -- nothing to
// do here for that.
//
// BOOT button: dedicated Jarvis button, symmetric with PWR/Record. Single
// click with nothing in progress starts a Jarvis voice-command capture
// (drawJarvis() scene); single click again finishes it, same
// start/stop-on-same-button behavior as PWR/Record. While a memo recording
// is live instead (PWR was pressed first), BOOT cancels that memo -- the
// audio is discarded entirely (SD file deleted / PSRAM never offered, see
// recorder_cancel()), with a descending tone instead of the save click.
// Status cycling has been dropped from both buttons entirely (custom
// statuses are now Settings-dashboard-only, not physically cycled) -- BOOT
// no longer has an idle-state fallback action beyond starting Jarvis.

enum class AppState { IDLE, RECORDING, SYNCING };

static epaper_driver_display *s_epd = nullptr;
static board_power_bsp_t s_power(EPD_PWR_PIN, Audio_PWR_PIN, VBAT_PWR_PIN);
static AppState s_state = AppState::IDLE;
// Which kind of capture AppState::RECORDING currently means -- lets PWR and
// BOOT tell a memo apart from a Jarvis command so each button can cancel
// the *other* button's in-progress capture correctly (see recorder_start's
// isCommand param and both button tasks below).
static bool s_jarvisActive = false;
// True when the current Jarvis capture is the live Deepgram Voice Agent
// path (voice_agent.cpp) rather than a plain SD/RAM recording -- decided
// once at capture-start time (see bootButtonTask's IDLE branch) based on
// WiFi reachability, and used by both the second-click "finish" handler and
// syncWatchTask's completion poll to call the right stop/is-done functions.
static bool s_jarvisLive = false;

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
    // Timing instrumentation -- added to measure the real wake-from-sleep
    // cost instead of guessing. Every stage below has been suspected (at
    // different points tonight) of causing the multi-second delay users
    // hit on a button press after the device has gone to deep sleep; this
    // prints hard numbers instead of more theories. Cheap (printf only,
    // no control-flow change) -- safe to leave in.
    uint32_t tStart = millis();
    s_power.VBAT_POWER_ON();
    i2c_master_Init();
    uint32_t tI2c = millis();

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
        cfg.buffer_len = (EPD_WIDTH * EPD_HEIGHT) / 8;
        s_epd = new epaper_driver_display(EPD_WIDTH, EPD_HEIGHT, cfg);
        uint32_t tEpdCtor = millis();
        s_epd->EPD_Init();
        uint32_t tEpdInit = millis();
        s_epd->EPD_Clear();
        uint32_t tEpdClear = millis();
        s_epd->EPD_DisplayPartBaseImage();
        uint32_t tEpdBaseImg = millis();
        s_epd->EPD_Init_Partial();
        uint32_t tEpdInitPartial = millis();
        face_init(s_epd);
        face_update(false); // draw the idle smiley immediately
        uint32_t tFaceUpdate = millis();
        Serial.printf("timing: epd ctor=%lums init=%lums clear=%lums baseImg=%lums initPartial=%lums faceUpdate=%lums (total epd block=%lums)\n",
                      (unsigned long)(tEpdCtor - tI2c), (unsigned long)(tEpdInit - tEpdCtor),
                      (unsigned long)(tEpdClear - tEpdInit), (unsigned long)(tEpdBaseImg - tEpdClear),
                      (unsigned long)(tEpdInitPartial - tEpdBaseImg), (unsigned long)(tFaceUpdate - tEpdInitPartial),
                      (unsigned long)(tFaceUpdate - tI2c));
    }
    uint32_t tDisplayDone = millis();

    s_power.POWEER_Audio_ON(); // audio_bsp_init needs the rail up; powered back down below
    user_button_init();

    audio_bsp_init();
    audio_play_init();
    recorder_init();
    uint32_t tAudioDone = millis();

    sdcard_init();
    uint32_t tSdDone = millis();

    wifi_sync_init(); // best-effort; fine if your router won't let this join (2.4GHz-only)
    uint32_t tWifiDone = millis();
    ble_sync_init();  // primary sync path when WiFi STA can't connect
    uint32_t tBleDone = millis();

    Serial.printf("timing: i2c=%lums display_block=%lums audio_block=%lums sd=%lums wifi_init=%lums ble_init=%lums TOTAL=%lums\n",
                  (unsigned long)(tI2c - tStart), (unsigned long)(tDisplayDone - tI2c),
                  (unsigned long)(tAudioDone - tDisplayDone), (unsigned long)(tSdDone - tAudioDone),
                  (unsigned long)(tWifiDone - tSdDone), (unsigned long)(tBleDone - tWifiDone),
                  (unsigned long)(tBleDone - tStart));

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

// Shared by buttonTask's normal PWR-click-while-IDLE path and light
// sleep's post-wake PWR dispatch (sleepWatchTask) -- factored out so the
// two can't drift the way LOW_80/MEDIUM_160 briefly did between two
// near-duplicate call sites earlier tonight.
static void startMemoRecording() {
    s_state = AppState::RECORDING;
    s_jarvisActive = false;
    // Reverted from LOW_80 -- live-confirmed on real hardware (post-flash,
    // on battery) that dropping to 80MHz made button response feel
    // sluggish. I2S capture itself is DMA-driven, not CPU-throughput-bound,
    // so this isn't about dropped audio samples -- but general task/
    // interrupt scheduling latency at 80MHz is apparently enough to be felt
    // on a button press. 160MHz as the battery-conscious middle ground
    // between that and the previous always-240MHz.
    power_mgr_set_profile(PowerProfile::MEDIUM_160, "recording");
    recorder_start(false);
}

// Polls the PWR button's event group (set by button_bsp's ISR-driven multi
// click detector) and drives the recording state machine. Single click
// toggles: IDLE -> RECORDING -> (stop) -> SYNCING -> IDLE.
static void buttonTask(void *arg) {
    for (;;) {
        EventBits_t bits = xEventGroupWaitBits(pwr_groups, set_bit_all, pdTRUE, pdFALSE, pdMS_TO_TICKS(200));

        if (bits) power_mgr_note_activity();
        if (get_bit_button(bits, 0)) { // single click
            // Timing instrumentation -- testing the hypothesis that an
            // active WiFi sync transfer (handleGetFile streaming a whole
            // recording synchronously inside wifiTask on core 0, see
            // wifi_sync.cpp) degrades button response even though buttons
            // run on core 1. Logs whether a transfer/radio was active at
            // the moment of this click, and how long the click->action
            // work itself took, so this can be measured rather than
            // assumed either way.
            uint32_t tClick = millis();
            bool wifiXferAtClick = wifi_sync_is_transferring();
            bool wifiOnAtClick = wifi_sync_radio_is_on();
            if (s_state == AppState::IDLE) {
                Serial.println("main: PWR click -> start recording");
                startMemoRecording();
            } else if (s_state == AppState::RECORDING && !s_jarvisActive) {
                Serial.println("main: PWR click -> stop recording");
                recorder_stop();
                s_state = AppState::SYNCING;
            } else if (s_state == AppState::RECORDING && s_jarvisActive) {
                // A Jarvis capture is live (BOOT started it) -- PWR cancels
                // it instead of being ignored, symmetric with BOOT
                // cancelling a live memo below.
                Serial.println("main: PWR click -> cancel Jarvis capture");
                recorder_cancel();
                s_state = AppState::SYNCING; // syncWatchTask returns to IDLE once the task winds down
            } else {
                Serial.printf("timing: PWR click ignored (state=SYNCING) -- wifiOn=%d wifiXfer=%d\n",
                              wifiOnAtClick, wifiXferAtClick);
            }
            Serial.printf("timing: PWR click handled in %lums (wifiOnAtClick=%d wifiXferAtClick=%d)\n",
                          (unsigned long)(millis() - tClick), wifiOnAtClick, wifiXferAtClick);
            // A click while SYNCING is ignored -- wait for the current
            // recording to finish saving before starting another.
        }
    }
}

// Polls the BOOT button's event group: dedicated Jarvis button. IDLE ->
// starts a Jarvis voice-command capture; RECORDING (Jarvis) -> finishes it;
// RECORDING (memo, started by PWR) -> cancels the memo instead, symmetric
// with PWR cancelling a live Jarvis capture above. A showing notification
// still claims the click first (dismiss), same as before. Status cycling
// has been dropped entirely -- statuses are Settings-dashboard-only now.
static void bootButtonTask(void *arg) {
    for (;;) {
        EventBits_t bits = xEventGroupWaitBits(boot_groups, set_bit_all, pdTRUE, pdFALSE, pdMS_TO_TICKS(200));

        if (bits) power_mgr_note_activity();
        if (get_bit_button(bits, 0)) { // single click
            // See buttonTask's identical instrumentation comment.
            uint32_t tClick = millis();
            bool wifiXferAtClick = wifi_sync_is_transferring();
            bool wifiOnAtClick = wifi_sync_radio_is_on();
            if (s_state == AppState::RECORDING && !s_jarvisActive) {
                // A live memo recording claims the click: cancel and
                // discard (unchanged from before Jarvis existed).
                Serial.println("main: BOOT click -> cancel recording (discard)");
                recorder_cancel();
                s_state = AppState::SYNCING; // syncWatchTask returns to IDLE once the task winds down
            } else if (s_state == AppState::RECORDING && s_jarvisActive) {
                Serial.println("main: BOOT click -> finish Jarvis capture");
                // Both are safe to call regardless of which path actually
                // ended up running: voice_agent_start_command() can itself
                // fall back to recorder_start(true) internally (no
                // Deepgram key configured, or the connection failed) without
                // main.cpp finding out which -- each stop function is a
                // no-op if its own task isn't the one active.
                voice_agent_stop();
                recorder_stop();
                s_state = AppState::SYNCING;
            } else if (face_notification_active()) {
                // A showing notification claims the click: dismiss it.
                Serial.println("main: BOOT click -> dismiss notification");
                face_dismiss_notification();
            } else if (s_state == AppState::IDLE) {
                s_state = AppState::RECORDING;
                s_jarvisActive = true;
                // Reverted from LOW_80 -- live-confirmed on real hardware
                // (post-flash) that dropping to 80MHz made both buttons feel
                // sluggish on battery, Jarvis noticeably worse (it does more
                // work per capture: live-mode reachability/connection setup
                // and, on the fallback path, more state juggling than a
                // plain memo). 160MHz as the battery-conscious middle
                // ground -- see recorder's identical comment above.
                power_mgr_set_profile(PowerProfile::MEDIUM_160, "jarvis capture");
                // Live Deepgram Voice Agent when reachable (see
                // voice_agent.cpp -- answers questions immediately,
                // independent of the Mac) -- else today's record-to-SD/RAM
                // path, picked up and executed later by poller.py once the
                // Mac is reachable.
                // voice_agent_live_enabled() is off by default and reproduced
                // a real hardware hang on its first live test -- see
                // voice_agent.cpp's top comment. Do not remove this gate
                // until that's root-caused and fixed with a serial monitor
                // attached.
                if (wifi_sync_http_proven_reachable() && voice_agent_live_enabled()) {
                    Serial.println("main: BOOT click -> start Jarvis capture (live Deepgram Voice Agent)");
                    s_jarvisLive = true;
                    voice_agent_start_command();
                } else {
                    Serial.println("main: BOOT click -> start Jarvis capture (recording, no live connection)");
                    s_jarvisLive = false;
                    recorder_start(true);
                }
            }
            Serial.printf("timing: BOOT click handled in %lums (wifiOnAtClick=%d wifiXferAtClick=%d)\n",
                          (unsigned long)(millis() - tClick), wifiOnAtClick, wifiXferAtClick);
        }
    }
}

// Waits for the recorder task to finish writing its WAV file, then flips
// back to IDLE. The actual upload happens passively: wifi_sync's HTTP
// server just serves whatever is on the card whenever the phone asks.
static void syncWatchTask(void *arg) {
    for (;;) {
        // Did recorder.cpp actually do something for the command just
        // finished? True for every memo recording (s_jarvisActive false --
        // s_jarvisLive is irrelevant/stale there, ignore it) and every
        // Jarvis capture that wasn't live (or fell back to SD/RAM from a
        // live attempt) -- false only for a Jarvis capture that streamed
        // live end to end, where recorder_last_was_sd()/
        // recorder_was_cancelled() would otherwise still reflect a stale,
        // unrelated previous recording.
        bool recorderPathActive = !s_jarvisActive || !s_jarvisLive || voice_agent_used_recorder_fallback();
        if (s_state == AppState::SYNCING && !recorder_is_recording() && !voice_agent_is_active()) {
            // Recording (and its final WAV flush) is done -- drop back to
            // the 80 MHz baseline before the cosmetic syncing-face pause.
            // On external power there's no reason to throttle down at all.
            if (!power_mgr_on_external_power()) {
                power_mgr_set_profile(PowerProfile::LOW_80, "recording finished");
            }
            if (!recorderPathActive) {
                // A successful live Deepgram Voice Agent session never
                // touches recorder.cpp at all -- recorder_was_cancelled()/
                // recorder_last_was_sd() would otherwise still reflect
                // whatever the PREVIOUS recording was, incorrectly
                // re-triggering a WiFi sync window for old/nonexistent
                // data. Nothing to sync for this command; it already ran
                // live end to end.
                Serial.println("main: Jarvis live session finished, nothing to sync");
            } else if (recorder_was_cancelled()) {
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
            // Brief "syncing" face before going back to idle, purely
            // cosmetic -- the file is already on disk and downloadable at
            // this point regardless of WiFi state. Was 4000/1500ms --
            // live-confirmed on real hardware that this reads as "the
            // device is slow to respond": button presses during this
            // window are silently ignored (buttonTask/bootButtonTask both
            // no-op on a click while SYNCING), so a press that landed
            // inside the old 4s window looked like the button itself was
            // laggy. Cut down to the minimum that still reads as a visible
            // state change rather than a flicker.
            vTaskDelay(pdMS_TO_TICKS(wifi_sync_is_connected() ? 800 : 500));
            s_state = AppState::IDLE;
            s_jarvisActive = false;
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
        face_update(s_state == AppState::RECORDING, s_jarvisActive);
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

// Two-tier idle sleep (battery). Tier 1 (routine, ~2min idle): real light
// sleep -- near-instant wake, no reboot, no visible display change, tasks/
// RAM/PSRAM all survive. Tier 2 (genuinely long idle, 20min of nothing --
// see power_mgr_deep_sleep_fallback_due()): the original deep sleep,
// unmodified, since light sleep's battery draw isn't proven equal to deep
// sleep's on this board and nobody needs instant wake after 20+ minutes of
// no interaction. Eligibility (nothing recording, no active WiFi sync
// session, no BLE central connected, not on external power) is identical
// for both tiers.
static void sleepWatchTask(void *arg) {
    for (;;) {
        // !wifi_sync_radio_is_on() is already the real "is sync done"
        // signal, not a fixed timer: the radio only turns off once the Mac
        // actually confirms sync (POST /synced), or -- as an intentional
        // bailout, not a bug -- after SYNC_INACTIVITY_MS of no HTTP
        // traffic at all (device unreachable / Mac app not running).
        // Deliberately NOT also gating on wifi_sync_has_pending_recordings()
        // here: that bailout case can leave recordings still pending with
        // the radio legitimately off, and blocking sleep on that would mean
        // the device never sleeps again until it happens to reach the Mac
        // -- draining the battery instead of just retrying on the next
        // scheduled wake, which is the correct behavior for "can't sync
        // right now" (bad credentials, out of range, laptop asleep).
        // !power_mgr_usb_host_attached() -- live-confirmed bug: sleeping
        // while USB is attached leaves the device enumerated but silently
        // unresponsive (this board's USB Serial/JTAG peripheral shares the
        // clock esp_light_sleep_start() gates), bricking flashing/serial
        // until a full power cycle. Nothing previously gated sleep on real
        // USB presence -- power_mgr_on_external_power() only infers
        // charging from battery voltage, a different (and known-fragile)
        // signal. !power_mgr_boot_grace_period_active() is belt-and-braces
        // so an early BLE connect can't arm the countdown before there's a
        // real chance to intervene.
        // !power_mgr_external_power_override_active() -- NOT the raw
        // power_mgr_on_external_power() signal. Live-confirmed incident: a
        // fully-charged battery can read "on external power" for hours
        // after actually being unplugged (see that function's doc
        // comment), which permanently blocked sleep here and drained
        // ~80% of the battery overnight. The bounded override still
        // blocks sleep for a real charging session, just not forever off
        // a stale reading.
        bool eligible = s_state == AppState::IDLE &&
                        !wifi_sync_radio_is_on() &&
                        !ble_sync_is_connected() &&
                        !power_mgr_external_power_override_active() &&
                        !power_mgr_usb_host_attached() &&
                        !power_mgr_boot_grace_period_active() &&
                        !face_notification_active();

        // Computed once per iteration -- drives both the deep-sleep-fallback
        // gate and the light-sleep TIMER-wake behavior below. Cheap: a
        // local SD opendir scan, no radio cost either way.
        bool pending = wifi_sync_has_pending_recordings();

        if (eligible && !pending && power_mgr_deep_sleep_fallback_due()) {
            Serial.println("main: 20min genuinely idle, nothing pending -- falling back to deep sleep");
            // "Sleeping..." draw is deliberately ONLY on this rare, long-idle
            // path, not on the routine light-sleep tier below -- light sleep
            // is meant to stay instant/invisible; a ~580ms refresh here is a
            // non-issue since this only fires after 20 minutes of nothing.
            face_show_notification("Sleeping...", "");
            face_update(false); // synchronous, on this task's own stack --
                                 // must complete before the chip halts, and
                                 // faceTask's own core halts too during sleep.
            // Leave whatever's currently on the panel (idle smiley, status
            // face) as the last image -- e-paper holds it unpowered, and
            // drawing something new here would cost a visible refresh for
            // a state nobody's necessarily watching happen.
            if (s_epd) s_power.POWEER_EPD_OFF();
            audio_bsp_power_down();
            s_power.POWEER_Audio_OFF();
            power_mgr_enter_deep_sleep(); // never returns
        } else if (eligible && power_mgr_idle_timeout_reached()) {
            audio_bsp_power_down();
            s_power.POWEER_Audio_OFF();
            // Paused before every light sleep (restored default -- an
            // earlier attempt at leaving BLE advertising on continuously
            // through light sleep was rejected on battery grounds:
            // continuous advertising has a real ongoing cost, and the
            // device now light-sleeps within ~5s of going idle, so "always
            // on" defeated a chunk of that battery win. Auto-sync instead
            // comes from the brief, pending-gated TIMER-wake window below.
            ble_sync_pause_advertising_for_sleep();

            WakeCause cause = power_mgr_enter_light_sleep(pending); // blocks here

            // Live-confirmed bug: sync worked reliably after a cold boot
            // but NOT after a light-sleep wake -- wifi_sync_radio_on() got
            // called, state correctly tracked CONNECTING, but the
            // connection never actually progressed. See
            // wifi_sync_reinit_after_light_sleep()'s doc for why (lwIP's
            // TCP/IP task state doesn't reliably survive the halt). Cheap
            // WiFi mode toggle, no route re-registration -- do this before
            // any dispatch below that might call wifi_sync_radio_on().
            wifi_sync_reinit_after_light_sleep();
            // button_bsp's click detector is poll-based (5ms esp_timer, not
            // a GPIO ISR -- confirmed in button_bsp.c), so it will NOT see
            // the press that woke the chip on its own. PWR gets fast-
            // dispatched explicitly (same shared helper buttonTask's own
            // IDLE branch uses, so the two can't drift).
            if (cause == WakeCause::BUTTON_PWR) {
                Serial.println("main: woke from light sleep via PWR -- resuming recording");
                startMemoRecording();
                ble_sync_resume_advertising_after_sleep();
            } else if (cause == WakeCause::BUTTON_BOOT || cause == WakeCause::BUTTON_BOTH) {
                // A real button press either way -- device is fully awake
                // for user interaction now (normal poll-based button tasks
                // pick up the actual click), so BLE should be available
                // too, not just for the TIMER-wake pending case below.
                face_clear_status();
                ble_sync_resume_advertising_after_sleep();
            } else {
                // TIMER wake. Walk-in-range auto-sync: BLE connecting is a
                // low-cost PRESENCE SIGNAL that triggers a WiFi attempt --
                // ble_sync.cpp's onConnect() is the ONLY thing that turns
                // WiFi on here, and only once a BLE central actually
                // connects (presenceConfirmed=true there, short 10s
                // inactivity window since presence is already known, not
                // hoped for). This code just opens the BLE window and
                // waits -- it must NOT also blindly call
                // wifi_sync_radio_on() itself, which was a live-confirmed
                // bug: that turned WiFi on unconditionally every ~5min
                // whenever anything was pending, with zero confirmation
                // anyone was even in range, directly contributing to an
                // overnight battery drain incident. The two transports
                // are sequential, not concurrent: WiFi and BLE share the
                // same physical radio on this SoC, so running both as
                // active sync channels at once would just make them
                // contend for the same hardware, not truly parallelize.
                // WiFi becomes the sole active transport once it connects;
                // BLE (already open) is only the actual bearer if WiFi
                // fails to connect.
                face_clear_status();
                if (pending) {
                    Serial.println("main: TIMER wake -- pending recordings, opening BLE window to check for a nearby sync partner");
                    ble_sync_resume_advertising_after_sleep();
                    // Brief hold so the window is actually visible to a
                    // scanning Mac -- without this, the very next 1s loop
                    // tick re-evaluates eligibility (idle timeout already
                    // exceeded) and re-enters light sleep almost
                    // immediately, giving advertising ~1s of real
                    // visibility instead of a real window. If a central
                    // connects during this hold, ble_sync.cpp's onConnect()
                    // fires wifi_sync_radio_on() on its own -- nothing
                    // further needed here either way.
                    vTaskDelay(pdMS_TO_TICKS(8000));
                }
                // else: nothing pending -- stay paused, let the idle clock
                // proceed toward the deep-sleep fallback as normal. No
                // reason to hold a window open with nothing to check for.
            }
            // Only a real button wake counts as activity -- a TIMER wake
            // must NOT reset this clock, or power_mgr_deep_sleep_fallback_due()
            // would never trip (every ~10min timer wake would keep pushing
            // the 20min threshold out indefinitely).
            if (cause == WakeCause::BUTTON_PWR || cause == WakeCause::BUTTON_BOOT || cause == WakeCause::BUTTON_BOTH) {
                power_mgr_note_activity();
            }
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

    // Fast-dispatch: a button woke the chip specifically to do something,
    // not to sit at the idle smiley waiting for a second press. PWR wake
    // resumes straight into recording (its normal single-click action);
    // BOOT wake just needs the normal idle boot above -- its own actions
    // (status cycling, cancel) require a live face/BOOT-click context that
    // already exists post-boot, so there's nothing extra to fast-dispatch.
    //
    // power_mgr_note_activity() is deliberately called ONLY on a real
    // button wake, not unconditionally on every boot -- this is the deep-
    // sleep-reboot path, and deep sleep has its own periodic TIMER wake
    // (see power_mgr.cpp's DEEP_SLEEP_TIMER_WAKE_INTERVAL_US). Calling
    // this on a bare TIMER-caused reboot (zero real user activity) would
    // reset the light-sleep tier's idle clock every ~20min purely from the
    // device waking itself up -- meaning it could never settle into deep
    // sleep for a genuinely unattended stretch (e.g. overnight), since its
    // own wake would keep knocking it back to the light-sleep tier.
    if (wake == WakeCause::BUTTON_PWR) {
        Serial.println("main: woke via PWR button -- resuming recording immediately");
        startMemoRecording();
        power_mgr_note_activity();
    } else if (wake == WakeCause::TIMER) {
        // Deep sleep's own periodic wake -- just a brief BLE reconnect
        // window (ble_sync_init() above already re-advertises on this
        // fresh boot), no WiFi attempt here. An earlier attempt at an
        // unconditional WiFi-on-timer-wake was reverted this session after
        // a live hang, but that hang's real cause (traced later, same
        // session) was deep-sleep wake latency stacked with an unrelated
        // e-paper refresh cost, not the SD-card access itself -- SD is on
        // the dedicated SDMMC controller, e-paper is on SPI2, separate
        // buses, no shared lock. The equivalent walk-in-range check now
        // lives in the light-sleep tier instead (sleepWatchTask), which is
        // where routine idle time is actually spent.
        Serial.println("main: woke via duty-cycle timer -- brief BLE reconnect window");
    }

    // Last: drop to the 80 MHz baseline now that boot is done. Recording
    // and WiFi streaming bump back to 240 for exactly their duration.
    power_mgr_init();
}

void loop() {
    vTaskDelay(pdMS_TO_TICKS(1000));
}
