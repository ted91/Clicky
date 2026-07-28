#include "wifi_sync.h"
#include <WiFi.h>
#include <WebServer.h>
#include <dirent.h>
#include <sys/stat.h>
#include "secrets.h"
#include "recorder.h"

static const char *SDCARD_DIR = "/sdcard";
static const char *RAM_RECORDING_NAME = "ram_recording.wav";

static WebServer s_server(80);
static bool s_wifiConnecting = false;
static volatile bool s_transferInProgress = false;

static bool isWavFile(const char *name) {
    size_t len = strlen(name);
    return len > 4 && strcasecmp(name + len - 4, ".wav") == 0;
}

static void handleRoot() {
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
        s_server.send_P(200, "audio/wav", (const char *)data, len);
        s_transferInProgress = false;
        return;
    }

    char path[300];
    if (!sanitizedPath(path, sizeof(path))) {
        s_server.send(400, "text/plain", "bad name");
        return;
    }
    FILE *f = fopen(path, "rb");
    if (!f) {
        s_server.send(404, "text/plain", "not found");
        return;
    }
    fseek(f, 0, SEEK_END);
    long size = ftell(f);
    fseek(f, 0, SEEK_SET);

    s_server.setContentLength(size);
    s_server.send(200, "audio/wav", "");

    s_transferInProgress = true;
    static uint8_t buf[1024];
    size_t n;
    while ((n = fread(buf, 1, sizeof(buf), f)) > 0) {
        s_server.client().write(buf, n);
    }
    s_transferInProgress = false;
    fclose(f);
}

static void handleDeleteFile() {
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
    // SD-card recordings are a permanent archive -- the pipeline only ever
    // sends DELETE for the RAM-named file, but refuse it here too on
    // principle (matches ble_sync.cpp's DELETE handling) rather than
    // actually removing an SD file just because something asked to.
    if (!s_server.hasArg("name") || s_server.arg("name").isEmpty()) {
        s_server.send(400, "text/plain", "bad name");
        return;
    }
    Serial.printf("wifi_sync: DELETE requested for SD file '%s' (ignored -- SD recordings are kept)\n",
                  s_server.arg("name").c_str());
    s_server.send(200, "text/plain", "ok (sd recordings are kept, not deleted)");
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

void wifi_sync_init() {
    Serial.printf("wifi_sync: connecting to SSID \"%s\"...\n", WIFI_SSID);
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    s_wifiConnecting = true;

    s_server.on("/", HTTP_GET, handleRoot);
    s_server.on("/list", HTTP_GET, handleList);
    s_server.on("/rec", HTTP_GET, handleGetFile);
    s_server.on("/rec", HTTP_DELETE, handleDeleteFile);
    s_server.begin();
}

// How long to keep retrying a WiFi STA connection before giving up. WiFi is
// a best-effort sync path (BLE is the reliable fallback — see ble_sync.cpp)
// so there's no point letting a failed connection spam Serial forever or
// keep the WiFi radio active fighting BLE for airtime.
static const uint32_t WIFI_GIVEUP_MS = 20000;

void wifi_sync_tick() {
    if (s_wifiConnecting) {
        wl_status_t status = WiFi.status();
        static uint32_t connectStartMs = millis();

        if (status == WL_CONNECTED) {
            s_wifiConnecting = false;
            Serial.printf("wifi_sync: connected, IP=%s\n", WiFi.localIP().toString().c_str());
        } else if (millis() - connectStartMs > WIFI_GIVEUP_MS) {
            s_wifiConnecting = false;
            Serial.printf("wifi_sync: giving up after %lus (status=%s) -- turning off WiFi radio, use BLE sync instead\n",
                          (unsigned long)(WIFI_GIVEUP_MS / 1000), wifiStatusName(status));
            WiFi.disconnect(true);
            WiFi.mode(WIFI_OFF);
        } else {
            // Print current status every ~5s while still trying, so a
            // stuck connection is visible instead of silent -- but only
            // during this bounded retry window, not forever.
            static uint32_t lastStatusPrintMs = 0;
            uint32_t now = millis();
            if (now - lastStatusPrintMs > 5000) {
                lastStatusPrintMs = now;
                Serial.printf("wifi_sync: still connecting... status=%s\n", wifiStatusName(status));
            }
        }
    }
    s_server.handleClient();
}

bool wifi_sync_is_connected() {
    return WiFi.status() == WL_CONNECTED;
}

bool wifi_sync_is_transferring() {
    return s_transferInProgress;
}
