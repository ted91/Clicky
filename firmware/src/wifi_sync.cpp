#include "wifi_sync.h"
#include <WiFi.h>
#include <WebServer.h>
#include <Preferences.h>
#include <nvs_flash.h>
#include <dirent.h>
#include "esp_heap_caps.h"
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#include "secrets.h"
#include "recorder.h"
#include "face.h"
#include "power_mgr.h"
#include "fw_version.h"
#include <NimBLEDevice.h>
#include <Update.h>

// ESP32-S3 shares one physical radio between WiFi and BLE (time-division
// coexistence, see beginConnectAttempt()'s own comment on this same
// tradeoff during connection/backoff) -- Espressif's own coexistence docs
// confirm BLE advertising/connection events interrupting an in-progress
// WiFi transfer can cut its throughput to 30-50% of the uncontested rate.
// This device's BLE role (see ble_sync.h) is only ever (a) initial
// pairing/dashboard access, or (b) the fallback transfer path when WiFi
// isn't reachable at all -- never needed *during* an active WiFi transfer,
// so pausing advertising for that window is free throughput with no
// downside. NimBLEDevice::startAdvertising() is already established
// elsewhere in this codebase (ble_sync.cpp's onDisconnect) as safe/
// idempotent to call redundantly, so no guard needed around resuming it
// even if something else already restarted it in the meantime. Does NOT
// touch an already-connected BLE central (if one somehow exists mid-WiFi-
// transfer) -- stopping advertising only blocks NEW connections, an
// existing one is unaffected.
static void pauseBleAdvertisingForTransfer() {
    NimBLEDevice::stopAdvertising();
}

static void resumeBleAdvertisingAfterTransfer() {
    NimBLEDevice::startAdvertising();
}

static const char *SDCARD_DIR = "/sdcard";
static const char *RAM_RECORDING_NAME = "ram_recording.wav";

static WebServer s_server(80);
static volatile bool s_transferInProgress = false;

// --- radio session gating -------------------------------------------------
// The radio is OFF by default and only turned on for a bounded "sync
// session": when a recording finishes (main.cpp) or credentials change
// (SETWIFI). It turns back off when the Mac explicitly confirms it's done
// (POST /synced) or after SYNC_INACTIVITY_MS with no HTTP traffic --
// whichever comes first. This kills the always-connected,
// modem-sleep-disabled idle draw that dominated battery life.
static TaskHandle_t s_wifiTaskHandle = nullptr; // for waking the blocked wifiTask
static uint32_t s_radioOnMs = 0;                // when the current session started
static uint32_t s_lastHttpMs = 0;               // last served HTTP request
static volatile bool s_syncedRequested = false; // POST /synced arrived
static uint32_t s_syncedAtMs = 0;
static const uint32_t SYNC_INACTIVITY_MS = 120000; // no-HTTP fallback timeout
static const uint32_t SYNCED_LINGER_MS = 2000;     // let the /synced response flush

static void noteHttpActivity() { s_lastHttpMs = millis(); }

// Credentials live in NVS (Preferences), not just secrets.h's compile-time
// defaults -- so a user can (re)configure WiFi from the dashboard/BLE
// without reflashing. secrets.h is still the fallback for a fresh device
// that's never been configured via /settings.
static Preferences s_prefs;
static String s_ssid;
static String s_password;

static bool isWavFile(const char *name) {
    size_t len = strlen(name);
    return len > 4 && strcasecmp(name + len - 4, ".wav") == 0;
}

static void handleRoot() {
    noteHttpActivity();
    String html = "<html><body><h1>Recordings</h1><ul>";
    DIR *dir = opendir(SDCARD_DIR);
    if (dir) {
        struct dirent *entry;
        while ((entry = readdir(dir)) != nullptr) {
            if (!isWavFile(entry->d_name)) continue;
            html += "<li><a href=\"/rec?name=" + String(entry->d_name) + "\">" +
                    String(entry->d_name) + "</a></li>";
        }
        closedir(dir);
    }
    size_t ramLen = 0;
    if (recorder_ram_wav_data(&ramLen)) {
        html += "<li><a href=\"/rec?name=" + String(RAM_RECORDING_NAME) + "\">" +
                String(RAM_RECORDING_NAME) + "</a> (no SD card was present — recorded to PSRAM, capped length)</li>";
    }
    html += "</ul></body></html>";
    s_server.send(200, "text/html", html);
}

static void handleList() {
    noteHttpActivity();
    String json = "[";
    bool first = true;
    DIR *dir = opendir(SDCARD_DIR);
    if (dir) {
        struct dirent *entry;
        char path[300];
        while ((entry = readdir(dir)) != nullptr) {
            if (!isWavFile(entry->d_name)) continue;
            snprintf(path, sizeof(path), "%s/%s", SDCARD_DIR, entry->d_name);
            struct stat st;
            long size = (stat(path, &st) == 0) ? st.st_size : -1;
            if (!first) json += ",";
            first = false;
            json += "{\"name\":\"" + String(entry->d_name) + "\",\"size\":" + String(size) + "}";
        }
        closedir(dir);
    }
    size_t ramLen = 0;
    if (recorder_ram_wav_data(&ramLen)) {
        if (!first) json += ",";
        first = false;
        json += "{\"name\":\"" + String(RAM_RECORDING_NAME) + "\",\"size\":" + String((long)ramLen) + "}";
    }
    json += "]";
    s_server.send(200, "application/json", json);
}

static bool sanitizedPath(char *out, size_t outLen) {
    if (!s_server.hasArg("name")) return false;
    String name = s_server.arg("name");
    // Reject path traversal / subdirectories — recordings are always
    // flat files directly under /sdcard.
    if (name.indexOf('/') >= 0 || name.indexOf("..") >= 0 || name.isEmpty()) return false;
    snprintf(out, outLen, "%s/%s", SDCARD_DIR, name.c_str());
    return true;
}

// The card is mounted via the vendored esp_vfs_fat_sdmmc_mount() call in
// sdcard_bsp.cpp (not the Arduino SD_MMC library), so files are plain POSIX
// FILE* under /sdcard — stream them by hand rather than via fs::File.
static void handleGetFile() {
    noteHttpActivity();
    if (!s_server.hasArg("name")) {
        s_server.send(400, "text/plain", "bad name");
        return;
    }
    if (s_server.arg("name") == RAM_RECORDING_NAME) {
        size_t len = 0;
        const uint8_t *data = recorder_ram_wav_data(&len);
        if (!data) {
            s_server.send(404, "text/plain", "no ram recording available");
            return;
        }
        s_transferInProgress = true;
        pauseBleAdvertisingForTransfer();
        s_server.send_P(200, "audio/wav", (const char *)data, len);
        resumeBleAdvertisingAfterTransfer();
        s_transferInProgress = false;
        return;
    }

    char path[300];
    if (!sanitizedPath(path, sizeof(path))) {
        s_server.send(400, "text/plain", "bad name");
        return;
    }
    // POSIX open()/read() instead of fopen()/fread() -- confirmed via
    // Espressif's own newlib on ESP32: fread() internally fragments every
    // read into 128-byte esp_vfs_read() calls regardless of the buffer
    // size requested (a small fixed stdio buffer under the hood), which is
    // almost certainly why raising our own buffer to 32KB earlier made
    // zero measurable difference -- the real ceiling was one layer below
    // it. read() on the raw fd bypasses that stdio layer entirely and
    // actually honors the requested chunk size against the VFS/SD driver.
    int fd = open(path, O_RDONLY);
    if (fd < 0) {
        s_server.send(404, "text/plain", "not found");
        return;
    }
    long size = lseek(fd, 0, SEEK_END);
    lseek(fd, 0, SEEK_SET);

    s_server.setContentLength(size);
    s_server.send(200, "audio/wav", "");

    // Nagle's algorithm (on by default) batches small writes waiting for
    // an ACK before sending more -- combined with the receiver's own
    // delayed-ACK timer (typically ~40ms), that interaction alone can cap
    // throughput to a few dozen KB/s independent of buffer size, since TCP
    // still fragments/paces the actual segments underneath. Disabling it
    // is the standard fix for "small buffered writes crawl over WiFi".
    s_server.client().setNoDelay(true);

    // 32KB PSRAM buffer per request -- now actually meaningful now that
    // read() honors it, unlike fread() above. Falls back to a small
    // static buffer if PSRAM is momentarily unavailable (still correct,
    // just slower).
    static uint8_t fallbackBuf[4096];
    const size_t bigLen = 32 * 1024;
    uint8_t *big = (uint8_t *)heap_caps_malloc(bigLen, MALLOC_CAP_SPIRAM);
    uint8_t *buf = big ? big : fallbackBuf;
    size_t bufLen = big ? bigLen : sizeof(fallbackBuf);

    s_transferInProgress = true;
    pauseBleAdvertisingForTransfer();
    // Full radio wakefulness + full CPU only for the streaming window --
    // shortens the transfer, which nets less energy than a slow trickle at
    // low power (see wifi_sync_tick's CONNECTED comment).
    WiFi.setSleep(false);
    power_mgr_set_profile(PowerProfile::HIGH_240, "wifi file streaming");
    ssize_t n;
    while ((n = read(fd, buf, bufLen)) > 0) {
        s_server.client().write(buf, n);
    }
    power_mgr_set_profile(PowerProfile::LOW_80, "wifi file streamed");
    WiFi.setSleep(true);
    resumeBleAdvertisingAfterTransfer();
    s_transferInProgress = false;
    if (big) heap_caps_free(big);
    close(fd);
}

static void handleDeleteFile() {
    noteHttpActivity();
    if (s_server.hasArg("name") && s_server.arg("name") == RAM_RECORDING_NAME) {
        // The pipeline calls this once it's confirmed a successful download
        // of the RAM fallback recording -- clears it immediately rather
        // than waiting for the next recording to silently overwrite it, so
        // PSRAM is freed up right away and the device doesn't keep
        // re-offering already-synced audio via /list.
        recorder_clear_ram();
        s_server.send(200, "text/plain", "ok (ram recording cleared)");
        return;
    }
    if (!s_server.hasArg("name") || s_server.arg("name").isEmpty()) {
        s_server.send(400, "text/plain", "bad name");
        return;
    }
    // ?force=true is the dashboard's explicit "delete from device" action
    // (see ble_sync.cpp's DELETEFORCE for the BLE equivalent and why this
    // is deliberately separate from the routine sync-confirm DELETE below,
    // which only ever clears the RAM fallback and otherwise refuses on
    // principle -- SD recordings are a permanent archive by default).
    if (s_server.hasArg("force") && s_server.arg("force") == "true") {
        char path[300];
        if (!sanitizedPath(path, sizeof(path))) {
            s_server.send(400, "text/plain", "bad name");
            return;
        }
        if (remove(path) == 0) {
            Serial.printf("wifi_sync: force-deleted SD file '%s'\n", path);
            s_server.send(200, "text/plain", "ok (deleted)");
        } else {
            Serial.printf("wifi_sync: force-delete failed for '%s' (not found?)\n", path);
            s_server.send(404, "text/plain", "not found");
        }
        return;
    }
    // SD-card recordings are a permanent archive -- the pipeline only ever
    // sends plain DELETE for the RAM-named file, but refuse it here too on
    // principle (matches ble_sync.cpp's DELETE handling) rather than
    // actually removing an SD file just because something asked to.
    Serial.printf("wifi_sync: DELETE requested for SD file '%s' (ignored -- SD recordings are kept, use ?force=true)\n",
                  s_server.arg("name").c_str());
    s_server.send(200, "text/plain", "ok (sd recordings are kept, not deleted)");
}

// Parity with ble_sync.cpp's NOTIFY command -- same face_show_notification
// + click, just reached over HTTP instead of a GATT write, for whichever
// sync_transport the pipeline is currently configured to use.
static void handleNotify() {
    noteHttpActivity();
    if (!s_server.hasArg("title")) {
        s_server.send(400, "text/plain", "missing title");
        return;
    }
    String title = s_server.arg("title");
    String body = s_server.hasArg("body") ? s_server.arg("body") : "";
    face_show_notification(title.c_str(), body.c_str());
    recorder_notify_click();
    s_server.send(200, "text/plain", "ok");
}

static void handleWifiStatus() {
    noteHttpActivity();
    s_server.send(200, "application/json", wifi_sync_status_json());
}

// Firmware auto-update -- see FW_VERSION's own docstring. The paired
// pipeline app compares this against its own bundled firmware version and
// only pushes a new image via POST /ota when this is older.
static void handleVersion() {
    noteHttpActivity();
    s_server.send(200, "application/json", String("{\"version\":\"") + FW_VERSION + "\"}");
}

// Receives a raw firmware.bin body (Content-Type: application/octet-stream,
// no multipart wrapping -- this is the app pushing a file it already has,
// not an HTML upload form) and flashes it to the inactive OTA partition
// (see partitions.csv). WebServer buffers a non-form/non-multipart POST
// body into server.arg("plain") in one shot -- fine here since it fits
// comfortably in PSRAM (firmware is ~1.4MB, 8MB PSRAM available) and this
// is a rare, one-shot operation, not something worth streaming incrementally.
//
// Safety net: CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE is on for this board
// (confirmed in the framework's sdkconfig.h, no patching needed) -- the
// bootloader treats a freshly-flashed OTA slot as "pending verify" and
// will automatically revert to the previous slot if the new image never
// calls esp_ota_mark_app_valid_cancel_rollback() (see main.cpp's setup())
// before crash-looping. A bad push can't brick a device left in the field.
static void handleOta() {
    noteHttpActivity();
    if (!s_server.hasArg("plain") || s_server.arg("plain").length() == 0) {
        s_server.send(400, "text/plain", "empty body");
        return;
    }
    String body = s_server.arg("plain");
    Serial.printf("wifi_sync: OTA push received, %u bytes\n", (unsigned)body.length());

    if (!Update.begin(body.length(), U_FLASH)) {
        Serial.printf("wifi_sync: Update.begin failed: %s\n", Update.errorString());
        s_server.send(500, "text/plain", String("Update.begin failed: ") + Update.errorString());
        return;
    }
    size_t written = Update.write((uint8_t *)body.c_str(), body.length());
    bool ok = (written == body.length()) && Update.end(true);
    if (!ok) {
        Serial.printf("wifi_sync: OTA write/end failed (wrote %u of %u): %s\n",
                      (unsigned)written, (unsigned)body.length(), Update.errorString());
        s_server.send(500, "text/plain", String("OTA failed: ") + Update.errorString());
        return;
    }

    Serial.println("wifi_sync: OTA flashed successfully, rebooting into new firmware");
    s_server.send(200, "text/plain", "ok (rebooting)");
    s_server.client().flush();
    delay(500); // let the response actually reach the app before the radio drops
    ESP.restart();
}

// The Mac's poller calls this once a WiFi sync cycle finishes (nothing
// left to download) -- the explicit "you can turn the radio off now"
// signal. GET accepted too for curl-ability. The actual power-down happens
// in wifi_sync_tick() a couple seconds later so this response can flush.
static void handleSynced() {
    noteHttpActivity();
    s_server.send(200, "text/plain", "ok (radio powering down)");
    s_syncedRequested = true;
    s_syncedAtMs = millis();
}

static void handleWifiConnect() {
    noteHttpActivity();
    if (!s_server.hasArg("ssid") || s_server.arg("ssid").isEmpty()) {
        s_server.send(400, "text/plain", "missing ssid");
        return;
    }
    String ssid = s_server.arg("ssid");
    String password = s_server.hasArg("password") ? s_server.arg("password") : "";
    // Respond before the connect attempt starts -- the client's own request
    // is riding this same WiFi connection, and it may drop as soon as
    // wifi_sync_set_credentials() disconnects to apply new credentials.
    s_server.send(200, "application/json", "{\"ok\":true}");
    wifi_sync_set_credentials(ssid.c_str(), password.c_str());
}

static void handleWifiScanStart() {
    noteHttpActivity();
    wifi_sync_start_scan();
    s_server.send(200, "application/json", "{\"ok\":true}");
}

static void handleWifiScanStatus() {
    noteHttpActivity();
    s_server.send(200, "application/json", wifi_sync_scan_json());
}

// Human-readable form of WiFi.status() so failures show up as something
// more useful than a bare number in Serial Monitor.
static const char *wifiStatusName(wl_status_t status) {
    switch (status) {
        case WL_IDLE_STATUS:     return "IDLE_STATUS (not yet started)";
        case WL_NO_SSID_AVAIL:   return "NO_SSID_AVAIL (SSID not found -- check name/2.4GHz band)";
        case WL_SCAN_COMPLETED:  return "SCAN_COMPLETED";
        case WL_CONNECTED:       return "CONNECTED";
        case WL_CONNECT_FAILED:  return "CONNECT_FAILED (likely wrong password)";
        case WL_CONNECTION_LOST: return "CONNECTION_LOST";
        case WL_DISCONNECTED:    return "DISCONNECTED";
        default:                 return "UNKNOWN";
    }
}

// --- connection state machine -------------------------------------------
// Unlike the old "try for 20s, then turn the radio off forever" behavior,
// credentials are now user-supplied at runtime (not just a build-time
// guess baked into secrets.h), so a failure here doesn't mean "this will
// never work" -- it might just mean the router was mid-reboot, or the user
// is about to fix a typo'd password from the dashboard. Keeps retrying
// indefinitely with a backoff between attempts instead of giving up.
enum class WifiState { OFF, IDLE, CONNECTING, CONNECTED, BACKOFF };
static WifiState s_state = WifiState::OFF;
static uint32_t s_stateChangedMs = 0;
static const uint32_t CONNECT_TIMEOUT_MS = 20000; // per-attempt ceiling
// Confirmed live: an ESP32-S3 shares one radio between WiFi and Bluetooth
// (time-division coexistence) -- a WiFi STA that's continuously
// scanning/associating (as a naive "retry forever" loop would do) measurably
// starves BLE's advertising window, to the point the device stopped being
// discoverable over BLE at all while WiFi kept cycling CONNECTING->BACKOFF
// with the radio left in WIFI_STA mode the whole time. The old (pre-retry)
// code avoided this by calling WiFi.mode(WIFI_OFF) once it gave up, fully
// freeing the radio for BLE from then on. BACKOFF now does the same between
// attempts -- WiFi.mode(WIFI_OFF) below -- so BLE gets real radio time
// during the pause, not just a state-machine pause with the radio still
// live. 30s (vs. the old code's single 20s attempt) balances "WiFi
// eventually reconnects on its own" against "BLE stays reliably reachable
// most of the time" when there's no valid network configured yet.
static const uint32_t BACKOFF_MS = 30000;          // pause between attempts (radio fully off)

static void beginConnectAttempt() {
    Serial.printf("wifi_sync: connecting to SSID \"%s\"...\n", s_ssid.c_str());
    WiFi.disconnect(true);
    WiFi.mode(WIFI_STA);
    // Confirmed live (Serial showing NO_AP_FOUND/STA_LEAVING every ~5s,
    // completely independent of this file's own 20s CONNECT_TIMEOUT_MS):
    // arduino-esp32's WiFi driver auto-reconnects on its own by default
    // whenever a connection attempt fails, on its own ~5s cadence, totally
    // bypassing this state machine's timing. That kept the radio
    // continuously busy re-scanning even during what this code considered
    // its own BACKOFF/off period, which starved BLE advertising badly
    // enough that the device stopped being discoverable over BLE at all.
    // Disabling it hands 100% of retry timing to this file's own state
    // machine (CONNECTING -> BACKOFF -> retry), which is the only thing
    // that actually calls WiFi.mode(WIFI_OFF) between attempts.
    WiFi.setAutoReconnect(false);
    // Band-steering/dual-band routers are the documented failure mode here
    // (see secrets.h/README) -- a lower, steadier TX power and disabling
    // modem sleep both measurably help association reliability on these,
    // at the cost of a bit more idle power draw (acceptable on a mains- or
    // battery-with-frequent-charging device like this one).
    WiFi.setTxPower(WIFI_POWER_15dBm);
    WiFi.setSleep(false);
    WiFi.begin(s_ssid.c_str(), s_password.c_str());
    s_state = WifiState::CONNECTING;
    s_stateChangedMs = millis();
}

// Confirmed live: Preferences::begin() can crash outright (assert failed:
// xQueueSemaphoreTake, deep inside the NVS component, before any of our own
// error-checking code runs) rather than cleanly returning false, when the
// NVS partition itself was never initialized -- as opposed to just "this
// namespace doesn't exist yet", which IS handled gracefully. Arduino-esp32
// normally calls nvs_flash_init() during its own startup before setup()
// runs, but this board's flash apparently reached a state where that
// either didn't happen or didn't succeed (e.g. a prior full-chip erase, or
// a partition-table change since it was last formatted). Calling
// nvs_flash_init() ourselves first is idempotent if it's already done, and
// is the standard ESP-IDF fix for exactly this failure mode -- if the
// partition needs reformatting (NO_FREE_PAGES / NEW_VERSION_FOUND), erase
// and retry once.
static void ensureNvsReady() {
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        Serial.println("wifi_sync: NVS partition needs erasing (corrupt/outdated) -- reformatting");
        nvs_flash_erase();
        err = nvs_flash_init();
    }
    if (err != ESP_OK) {
        Serial.printf("wifi_sync: nvs_flash_init failed (err=%d) -- WiFi credentials won't persist across reboots\n", (int)err);
    }
}

static void loadCredentials() {
    // On a fresh device the "wifi" NVS namespace doesn't exist yet (nothing
    // has ever been written to it), and begin(readOnly=true) fails with
    // NOT_FOUND in that case -- confirmed live that calling getString()/
    // end() on that failed handle anyway corrupts Preferences' internal
    // state and crashes (assert failed: xQueueSemaphoreTake), which then
    // repeats on every reboot since the namespace still doesn't exist.
    // Bail out to the compile-time fallback immediately instead.
    if (!s_prefs.begin("wifi", /*readOnly=*/true)) {
        s_ssid = WIFI_SSID;
        s_password = WIFI_PASSWORD;
        return;
    }
    s_ssid = s_prefs.getString("ssid", "");
    s_password = s_prefs.getString("password", "");
    s_prefs.end();
    if (s_ssid.isEmpty()) {
        // Namespace existed but nothing saved yet -- same fallback.
        s_ssid = WIFI_SSID;
        s_password = WIFI_PASSWORD;
    }
}

void wifi_sync_set_credentials(const char *ssid, const char *password) {
    s_prefs.begin("wifi", /*readOnly=*/false);
    s_prefs.putString("ssid", ssid);
    s_prefs.putString("password", password);
    s_prefs.end();
    s_ssid = ssid;
    s_password = password;
    Serial.printf("wifi_sync: new credentials saved for SSID \"%s\", reconnecting\n", ssid);
    // Route through the session API so the timeout bookkeeping starts fresh
    // -- a provisioning session ends the same way a sync session does
    // (/synced or the inactivity fallback), rather than staying on forever.
    s_state = WifiState::IDLE; // force radio_on to actually (re)connect with the new creds
    wifi_sync_radio_on("credentials changed");
}

String wifi_sync_status_json() {
    bool connected = (WiFi.status() == WL_CONNECTED);
    String json = "{\"configured\":" + String(s_ssid.isEmpty() ? "false" : "true");
    json += ",\"connected\":" + String(connected ? "true" : "false");
    json += ",\"ssid\":\"" + s_ssid + "\"";
    json += ",\"ip\":\"" + (connected ? WiFi.localIP().toString() : String("")) + "\"";
    // Battery is surfaced here (rather than a dedicated route/characteristic)
    // since this JSON blob is already polled over both HTTP and BLE
    // (WIFI_STATUS characteristic) regardless of WiFi connection state.
    json += ",\"batteryPct\":" + String(power_mgr_battery_pct());
    json += ",\"batteryMv\":" + String(power_mgr_battery_mv());
    json += ",\"externalPower\":" + String(power_mgr_on_external_power() ? "true" : "false");
    json += "}";
    return json;
}

// A single quote inside an SSID would otherwise corrupt the JSON payload
// (SSIDs are attacker-influenced input in the loosest sense -- anyone
// nearby can broadcast whatever they want) -- escape it like the other
// user-supplied strings this file emits.
static String jsonEscape(const String &s) {
    String out;
    out.reserve(s.length());
    for (size_t i = 0; i < s.length(); i++) {
        char c = s[i];
        if (c == '"' || c == '\\') out += '\\';
        out += c;
    }
    return out;
}

void wifi_sync_start_scan() {
    // scanNetworks(true) is async and non-blocking -- needs STA mode to
    // scan at all, so this also (harmlessly) pulls the radio out of
    // WIFI_OFF if a scan is requested mid-BACKOFF; the state machine's own
    // beginConnectAttempt() sets the mode again on its own schedule
    // regardless, so this doesn't fight it.
    if (WiFi.scanComplete() == WIFI_SCAN_RUNNING) return; // already in flight
    WiFi.mode(WIFI_STA);
    WiFi.scanNetworks(/*async=*/true);
}

String wifi_sync_scan_json() {
    int8_t result = WiFi.scanComplete();
    if (result == WIFI_SCAN_RUNNING) {
        return "{\"scanning\":true,\"networks\":[]}";
    }
    if (result == WIFI_SCAN_FAILED) {
        return "{\"scanning\":false,\"networks\":[]}";
    }

    // Dedup by SSID keeping the strongest signal -- the same network's
    // multiple APs (mesh/extenders) would otherwise clutter the dropdown
    // with duplicate entries the user can't usefully distinguish between.
    //
    // MAX_UNIQUE is capped small deliberately: NimBLECharacteristic's
    // value buffer has a hard ceiling of BLE_ATT_ATTR_MAX_LEN (512 bytes --
    // the ATT protocol's own maximum attribute length, not something this
    // characteristic's creation can raise). Confirmed live: with a higher
    // cap (24) in a real dense-network environment, the resulting JSON
    // exceeded 512 bytes and NimBLEAttValue::append() silently discarded
    // the entire value (logs "val > max" via its own NIMBLE_LOGE, which
    // isn't routed to this project's Serial output) -- leaving the
    // characteristic completely empty with no error surfaced to the BLE
    // client at all, which is what made this look like a connectivity bug
    // rather than a payload-size bug. 8 entries worst-case (32-byte SSID,
    // the WiFi spec's own max) stays safely under the limit.
    static const int MAX_UNIQUE = 8;
    static String ssids[MAX_UNIQUE];
    static int32_t rssis[MAX_UNIQUE];
    int uniqueCount = 0;

    for (int i = 0; i < result; i++) {
        String ssid = WiFi.SSID(i);
        if (ssid.isEmpty()) continue; // hidden network -- nothing to show/select
        int32_t rssi = WiFi.RSSI(i);
        bool found = false;
        for (int j = 0; j < uniqueCount; j++) {
            if (ssids[j] == ssid) {
                found = true;
                if (rssi > rssis[j]) rssis[j] = rssi;
                break;
            }
        }
        if (!found && uniqueCount < MAX_UNIQUE) {
            ssids[uniqueCount] = ssid;
            rssis[uniqueCount] = rssi;
            uniqueCount++;
        }
    }

    String json = "{\"scanning\":false,\"networks\":[";
    for (int i = 0; i < uniqueCount; i++) {
        if (i) json += ",";
        json += "{\"ssid\":\"" + jsonEscape(ssids[i]) + "\",\"rssi\":" + String(rssis[i]) + "}";
    }
    json += "]}";
    return json;
}

void wifi_sync_init() {
    ensureNvsReady();
    loadCredentials();

    // Must happen before s_server.begin() below: WebServer::begin() opens a
    // listening socket through lwIP immediately, which needs the TCP/IP
    // task's own mutex already initialized -- that only happens as a side
    // effect of WiFi.mode(). Confirmed live: without this, s_server.begin()
    // crashes on every boot (assert failed: xQueueSemaphoreTake, deep in
    // lwip_socket -> tcpip_send_msg_wait_sem -> sys_mutex_lock) regardless
    // of whether credentials are configured yet -- the old code happened to
    // get this ordering right by calling WiFi.begin() before starting the
    // server; this restores that ordering explicitly rather than relying on
    // beginConnectAttempt() (called conditionally, below) to do it in time.
    WiFi.mode(WIFI_STA);

    s_server.on("/", HTTP_GET, handleRoot);
    s_server.on("/synced", HTTP_POST, handleSynced);
    s_server.on("/synced", HTTP_GET, handleSynced);
    s_server.on("/list", HTTP_GET, handleList);
    s_server.on("/rec", HTTP_GET, handleGetFile);
    s_server.on("/rec", HTTP_DELETE, handleDeleteFile);
    s_server.on("/notify", HTTP_POST, handleNotify);
    s_server.on("/wifi/status", HTTP_GET, handleWifiStatus);
    s_server.on("/wifi/connect", HTTP_POST, handleWifiConnect);
    s_server.on("/wifi/scan", HTTP_POST, handleWifiScanStart);
    s_server.on("/wifi/scan", HTTP_GET, handleWifiScanStatus);
    s_server.on("/version", HTTP_GET, handleVersion);
    s_server.on("/ota", HTTP_POST, handleOta);
    s_server.begin();

    // Radio-off by default: the server socket is registered (lwIP survives
    // WIFI_OFF -- same pattern the BACKOFF state has always relied on) but
    // the radio stays dark until a recording finishes or credentials
    // change. This replaces the old always-connected behavior, which kept
    // the radio associated with modem sleep disabled 24/7 -- the single
    // biggest battery drain on the board.
    WiFi.mode(WIFI_OFF);
    s_state = WifiState::OFF;
    if (s_ssid.isEmpty()) {
        Serial.println("wifi_sync: no credentials configured yet -- set via BLE (SETWIFI) or the dashboard once BLE-connected");
    } else {
        Serial.println("wifi_sync: radio off (battery) -- turns on after each recording, off again once synced");
    }
}

void wifi_sync_set_task_handle(TaskHandle_t handle) {
    s_wifiTaskHandle = handle;
}

void wifi_sync_radio_on(const char *why) {
    s_syncedRequested = false;
    s_radioOnMs = millis();
    s_lastHttpMs = s_radioOnMs;
    if (s_state != WifiState::OFF && s_state != WifiState::IDLE) {
        // Already on (or mid-connect) -- just extend the session window.
        return;
    }
    if (s_ssid.isEmpty()) {
        Serial.println("wifi_sync: radio-on requested but no credentials -- staying off");
        return;
    }
    Serial.printf("wifi_sync: radio on (%s)\n", why ? why : "");
    beginConnectAttempt();
    if (s_wifiTaskHandle) xTaskNotifyGive(s_wifiTaskHandle); // unblock wifiTask
}

void wifi_sync_radio_off(const char *why) {
    if (s_state == WifiState::OFF) return;
    Serial.printf("wifi_sync: radio off (%s)\n", why ? why : "");
    WiFi.disconnect(true);
    WiFi.mode(WIFI_OFF);
    s_state = WifiState::OFF;
    s_syncedRequested = false;
}

bool wifi_sync_radio_is_on() {
    return s_state != WifiState::OFF;
}

void wifi_sync_tick() {
    // On external power (charging/plugged in), there's no battery draw to
    // avoid -- skip the session-end logic entirely and just stay
    // connected, matching how the device behaved before any of this radio
    // gating existed.
    if (s_state != WifiState::OFF && !s_transferInProgress && !power_mgr_on_external_power()) {
        if (s_syncedRequested && millis() - s_syncedAtMs > SYNCED_LINGER_MS) {
            wifi_sync_radio_off("mac confirmed sync complete");
        } else if (millis() - s_lastHttpMs > SYNC_INACTIVITY_MS &&
                   millis() - s_radioOnMs > SYNC_INACTIVITY_MS) {
            wifi_sync_radio_off("sync window timed out with no HTTP activity");
        }
    }

    switch (s_state) {
        case WifiState::OFF:
            return; // radio dark -- skip handleClient too, nothing can arrive

        case WifiState::IDLE:
            break; // nothing configured -- wait for wifi_sync_set_credentials()

        case WifiState::CONNECTING: {
            wl_status_t status = WiFi.status();
            // A short grace period (not the full CONNECT_TIMEOUT_MS) before
            // treating NO_SSID_AVAIL as terminal -- the very first status
            // read after WiFi.begin() can still be transitional (scan not
            // finished yet), but this status is otherwise unambiguous: the
            // network genuinely isn't there right now, no point burning the
            // full 20s (and the radio time that costs BLE) waiting it out.
            bool fastFail = (status == WL_NO_SSID_AVAIL) && (millis() - s_stateChangedMs > 3000);
            if (status == WL_CONNECTED) {
                s_state = WifiState::CONNECTED;
                // Tried raising TX power back to WIFI_POWER_19_5dBm here
                // once connected (reasoning: WIFI_POWER_15dBm in
                // beginConnectAttempt() was chosen for BLE
                // coexistence during scanning/associating, not transfer
                // throughput). Confirmed live this made real transfers
                // MEASURABLY WORSE (0.10 -> 0.02 MB/s, reproduced on a
                // clean retest with no other process contending for the
                // device) -- not a fluke. Reverted; 15dBm stays in effect
                // for the whole connection, not just while connecting.
                //
                // Modem sleep ON while merely connected: the radio only
                // needs full wakefulness while a file is actually streaming
                // (handleGetFile disables sleep for exactly that window and
                // re-enables it after). /list polls are tiny and tolerate
                // DTIM latency fine. This flips the old always-awake policy
                // -- with radio sessions now bounded anyway, the throughput-
                // vs-power tradeoff only matters inside the session, and
                // per-transfer toggling captures both.
                WiFi.setSleep(true);
                Serial.printf("wifi_sync: connected, IP=%s, sleep=%d\n", WiFi.localIP().toString().c_str(), (int)WiFi.getSleep());
            } else if (fastFail || millis() - s_stateChangedMs > CONNECT_TIMEOUT_MS) {
                Serial.printf("wifi_sync: attempt failed after %lus (status=%s) -- releasing radio for BLE, retrying in %lus\n",
                              (unsigned long)(CONNECT_TIMEOUT_MS / 1000), wifiStatusName(status),
                              (unsigned long)(BACKOFF_MS / 1000));
                WiFi.disconnect(true);
                WiFi.mode(WIFI_OFF); // see BACKOFF_MS's comment -- frees the radio for BLE during the pause
                s_state = WifiState::BACKOFF;
                s_stateChangedMs = millis();
            } else {
                static uint32_t lastStatusPrintMs = 0;
                uint32_t now = millis();
                if (now - lastStatusPrintMs > 5000) {
                    lastStatusPrintMs = now;
                    Serial.printf("wifi_sync: still connecting... status=%s\n", wifiStatusName(status));
                }
            }
            break;
        }

        case WifiState::CONNECTED:
            if (WiFi.status() != WL_CONNECTED) {
                Serial.println("wifi_sync: connection dropped, reconnecting");
                beginConnectAttempt();
            }
            break;

        case WifiState::BACKOFF:
            if (millis() - s_stateChangedMs > BACKOFF_MS) {
                beginConnectAttempt();
            }
            break;
    }
    s_server.handleClient();
}

bool wifi_sync_is_connected() {
    return WiFi.status() == WL_CONNECTED;
}

bool wifi_sync_has_credentials() {
    return !s_ssid.isEmpty();
}

bool wifi_sync_is_transferring() {
    return s_transferInProgress;
}
