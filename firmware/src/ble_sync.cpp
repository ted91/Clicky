#include "ble_sync.h"
#include <Arduino.h>
// NimBLE-Arduino (h2zero/NimBLE-Arduino), not the classic Bluedroid-based
// Arduino BLE library -- switched specifically to unlock L2CAP CoC support
// for a future speed upgrade (Bluedroid has no L2CAP CoC API at all, and
// the two host stacks can't run simultaneously on the same radio, so this
// is a full stack swap, not an addition). This migration is intended to be
// behavior-identical to the previous Bluedroid version -- see the classic
// vs NimBLE mapping notes throughout this file for what changed and why.
#include <NimBLEDevice.h>
#include <vector>
#include <dirent.h>
#include <sys/stat.h>
#include <stdio.h>
#include "esp_heap_caps.h"
#include "esp_mac.h"
#include "recorder.h"
#include "face.h"
#include "power_mgr.h"
#include <Preferences.h>
#include "wifi_sync.h"

static const char *SDCARD_DIR = "/sdcard";
static const char *RAM_RECORDING_NAME = "ram_recording.wav";

// Custom 128-bit UUIDs — arbitrary, just need to be consistent between here
// and the Python bleak client (pipeline/ble_device_client.py).
#define SERVICE_UUID     "e9a10000-1000-4000-8000-00805f9b34fb"
#define LIST_CHAR_UUID   "e9a10001-1000-4000-8000-00805f9b34fb"
#define CONTROL_CHAR_UUID "e9a10002-1000-4000-8000-00805f9b34fb"
#define DATA_CHAR_UUID   "e9a10003-1000-4000-8000-00805f9b34fb"
#define WIFI_STATUS_CHAR_UUID "e9a10004-1000-4000-8000-00805f9b34fb"
#define WIFI_SCAN_CHAR_UUID "e9a10005-1000-4000-8000-00805f9b34fb"

static const size_t CHUNK_BYTES = 244;      // MTU(247) - 3 bytes ATT overhead, see setMTU() in ble_sync_init()
// Tunable trade-off, not a hard requirement: shorter delay = faster
// transfer, at higher risk of overrunning the BLE stack's internal notify
// queue and dropping a packet. Safe to push lower than it otherwise would
// be because the pipeline validates the RIFF/WAVE header after every
// download and silently retries next poll cycle if corrupt (see
// poller.py's _is_valid_wav()) -- an occasional retry is cheap.
//
// Was lowered from the original untested default of 8ms to 2ms for speed,
// but live testing at 2ms showed exactly the failure mode this comment
// warned about: a ~2.3MB recording kept re-transferring in full,
// completing firmware-side every time, but never appearing in the
// pipeline -- i.e. poller.py's WAV validation was silently rejecting it
// as corrupt (dropped/reordered packets) and retrying from scratch,
// forever, on every poll cycle. Backed off to 5ms as a middle ground
// between the original conservative 8ms and the too-aggressive 2ms. Still
// meaningfully faster than the original (a 60s recording's chunk delay
// drops from ~62s to ~39s), but if pipeline logs still show repeated "not
// a valid WAV file... will retry next cycle" warnings at 5ms, raise this
// back toward 8ms -- there's still no way to verify the right value
// without live hardware, and this value has now failed once already at 2ms.
static const uint32_t CHUNK_DELAY_MS = 5;

// Larger read chunk for the L2CAP path -- write() fragments to the
// negotiated MTU internally and blocks until sent, so there's no notify-
// queue-overflow risk to pace against here; this is just a reasonable
// buffer size for reading from SD/RAM per write() call.
static const size_t L2CAP_READ_CHUNK_BYTES = 4096;

// Fixed PSM in the BT SIG's dynamic/app-assigned range. Hardcoded
// identically on both ends (see pipeline/ble_l2cap_client.py) since we
// control both the firmware and the Mac client -- no need for the
// publish-PSM-via-GATT-characteristic convention meant for interop with
// devices that don't already know their peer's PSM.
static const uint16_t L2CAP_PSM = 0x0081;

static NimBLECharacteristic *s_listChar = nullptr;
static NimBLECharacteristic *s_controlChar = nullptr;
static NimBLECharacteristic *s_dataChar = nullptr;
static NimBLECharacteristic *s_wifiStatusChar = nullptr;
static NimBLECharacteristic *s_wifiScanChar = nullptr;
static NimBLEServer *s_server = nullptr;
static NimBLEL2CAPChannel *s_l2capChannel = nullptr;

static volatile bool s_centralConnected = false;
static volatile bool s_transferInProgress = false;

static bool isWavFile(const char *name) {
    size_t len = strlen(name);
    return len > 4 && strcasecmp(name + len - 4, ".wav") == 0;
}

// Same JSON shape as wifi_sync's GET /list, duplicated here rather than
// shared since the two sync paths are meant to be independent modules.
static String buildListJson() {
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
    return json;
}

// Same SD-first-then-RAM priority as buildListJson(), but just the first
// name found -- used by the L2CAP auto-push path (see
// L2CAPTransferCallbacks::onConnect()) to pick which file to send without
// needing a GET command at all. Only meaningfully ambiguous if there are
// multiple pending SD recordings; the common case (RAM fallback, or a
// single pending SD file) is always unambiguous.
static bool pickPendingFileName(String &outName) {
    DIR *dir = opendir(SDCARD_DIR);
    if (dir) {
        struct dirent *entry;
        while ((entry = readdir(dir)) != nullptr) {
            if (!isWavFile(entry->d_name)) continue;
            outName = String(entry->d_name);
            closedir(dir);
            return true;
        }
        closedir(dir);
    }
    size_t ramLen = 0;
    if (recorder_ram_wav_data(&ramLen)) {
        outName = String(RAM_RECORDING_NAME);
        return true;
    }
    return false;
}

// Sends length-prefixed, chunked data over the DATA characteristic via
// notify. We tried indicate() for its BLE-protocol-level acknowledgment
// (fixes rare first-packet-loss corruption -- a dropped/reordered length
// prefix silently corrupts everything after it), but measured it live at
// ~250 bytes/sec on real hardware -- a ~1.8MB recording would take over two
// hours. That's because indicate()'s per-chunk round-trip is bound by the
// connection interval, and reliably shortening that from the peripheral
// side is a known-flaky operation on this exact library (multiple open
// arduino-esp32 issues: connection-parameter-update requests "not working
// as intended" / "working intermittently"). Not something to build
// reliability on. So: back to notify() for real throughput, with the
// pipeline's RIFF/WAVE header validation (ble_device_client.py /
// poller.py's _is_valid_wav()) as the safety net -- an occasional corrupt
// transfer gets silently caught and retried next poll cycle, which is a
// far better tradeoff than every transfer taking hours. Runs in its own
// task so it doesn't block the BLE stack's callback context while pacing.
static void transferTask(void *arg) {
    s_transferInProgress = true;

    String *namePtr = (String *)arg;
    String name = *namePtr;
    delete namePtr;

    const uint8_t *data = nullptr;
    size_t len = 0;
    FILE *f = nullptr;
    uint8_t *sdBuf = nullptr;

    if (name == RAM_RECORDING_NAME) {
        data = recorder_ram_wav_data(&len);
    } else {
        char path[300];
        snprintf(path, sizeof(path), "%s/%s", SDCARD_DIR, name.c_str());
        f = fopen(path, "rb");
        if (f) {
            fseek(f, 0, SEEK_END);
            len = ftell(f);
            fseek(f, 0, SEEK_SET);
            sdBuf = (uint8_t *)heap_caps_malloc(len, MALLOC_CAP_SPIRAM);
            if (sdBuf) {
                fread(sdBuf, 1, len, f);
                data = sdBuf;
            }
            fclose(f);
        }
    }

    if (!data) {
        Serial.printf("ble_sync: transfer requested for unknown/unreadable file '%s'\n", name.c_str());
        s_transferInProgress = false;
        vTaskDelete(NULL);
        return;
    }

    // L2CAP path only used if a channel is actually connected right now --
    // otherwise falls straight through to the original GATT notify() path,
    // completely unchanged. write() fragments to the negotiated MTU
    // internally and blocks until sent, so no CHUNK_DELAY_MS pacing is
    // needed here -- that pacing was purely a workaround for GATT's notify
    // queue, which doesn't apply to L2CAP CoC.
    bool useL2CAP = (s_l2capChannel != nullptr && s_l2capChannel->isConnected());

    Serial.printf("ble_sync: transferring %s (%u bytes) via %s\n", name.c_str(), (unsigned)len,
                  useL2CAP ? "L2CAP" : "GATT notify");

    uint32_t lenLE = (uint32_t)len;

    if (useL2CAP) {
        // Name-prefixed unlike the GATT path: the L2CAP channel auto-pushes
        // whatever file pickPendingFileName() picked (see
        // L2CAPTransferCallbacks::onConnect()) rather than being told a
        // name via a GET command, so the Mac side needs to be told what
        // it's actually receiving to avoid assuming it matches whatever it
        // originally asked for. 1-byte length (names are always short) +
        // raw UTF-8 name bytes, then the existing 4-byte little-endian
        // length + data framing, unchanged.
        uint8_t nameLen = (uint8_t)min((size_t)255, name.length());
        std::vector<uint8_t> nameFrame;
        nameFrame.push_back(nameLen);
        nameFrame.insert(nameFrame.end(), name.c_str(), name.c_str() + nameLen);
        s_l2capChannel->write(nameFrame);

        std::vector<uint8_t> lenBytes((uint8_t *)&lenLE, (uint8_t *)&lenLE + sizeof(lenLE));
        s_l2capChannel->write(lenBytes);

        size_t offset = 0;
        while (offset < len) {
            size_t n = min(L2CAP_READ_CHUNK_BYTES, len - offset);
            std::vector<uint8_t> chunk(data + offset, data + offset + n);
            if (!s_l2capChannel->write(chunk)) {
                Serial.printf("ble_sync: L2CAP write failed for %s, aborting transfer\n", name.c_str());
                break;
            }
            offset += n;
        }
    } else {
        s_dataChar->setValue((uint8_t *)&lenLE, sizeof(lenLE));
        s_dataChar->notify();
        vTaskDelay(pdMS_TO_TICKS(CHUNK_DELAY_MS));

        size_t offset = 0;
        while (offset < len) {
            size_t n = min(CHUNK_BYTES, len - offset);
            s_dataChar->setValue((uint8_t *)(data + offset), n);
            s_dataChar->notify();
            offset += n;
            vTaskDelay(pdMS_TO_TICKS(CHUNK_DELAY_MS));
        }
    }

    if (sdBuf) heap_caps_free(sdBuf);
    Serial.printf("ble_sync: transfer of %s complete\n", name.c_str());
    s_transferInProgress = false;
    vTaskDelete(NULL);
}

// Bluedroid stops advertising as soon as a central connects, and does NOT
// resume automatically once it disconnects -- without this, the device
// becomes permanently invisible to future scans after the very first
// connection (e.g. the pipeline's background poller connecting to fetch
// recordings), which is exactly what was happening before this existed.
// Kept unconditionally under NimBLE too even though its auto-resume
// behavior wasn't independently re-verified -- calling startAdvertising()
// again when already advertising is harmless/idempotent, so keeping this
// explicit call carries no risk either way.
//
// Callback signatures below are the actual NimBLE-Arduino 2.5.0 API
// (verified against github.com/h2zero/NimBLE-Arduino/blob/2.5.0/src/
// NimBLEServer.h and NimBLECharacteristic.h) -- both take an extra
// NimBLEConnInfo& parameter versus the classic library, and onDisconnect
// additionally takes an int reason code. Kept as `override` (not just a
// matching name) so a wrong signature is a hard compile error, not a
// silently-never-called callback.
//
// --- pairing / advertising power policy (battery) -------------------------
// "Paired" persists in NVS forever once set -- it's not a BLE bond, just
// "a Mac has connected to this device before." Migration: a device that's
// already had WiFi credentials configured (i.e. already in real use before
// this firmware) is treated as paired by default, so a reflash never
// silently strands an existing setup in a mode that won't auto-reconnect.
static Preferences s_pairPrefs;
static bool s_paired = false;
static bool s_pairingActive = false;
static uint32_t s_pairingStartedMs = 0;
static const uint32_t PAIRING_TIMEOUT_MS = 120000;

// Fast (general-discoverable) interval while actively pairing: the whole
// point is to be found quickly. Slow interval once paired: BLE's own spec
// ceiling is 10.24s; this sits near it (~1.2s in 0.625ms units = 0x0780)
// so a dropped connection still reconnects well inside the ~1min
// notification-latency the user said was fine, at a fraction of the
// advertising duty cycle of the old always-fast behavior.
static const uint16_t FAST_ADV_MIN = 0x20, FAST_ADV_MAX = 0x40;   // 20-40ms
static const uint16_t SLOW_ADV_MIN = 0x0640, SLOW_ADV_MAX = 0x0780; // ~1.0-1.2s

static void applyAdvertisingInterval(uint16_t minInterval, uint16_t maxInterval) {
    NimBLEAdvertising *adv = NimBLEDevice::getAdvertising();
    adv->setMinInterval(minInterval);
    adv->setMaxInterval(maxInterval);
}

// Called after boot and after every disconnect: paired devices keep
// reconnectability alive (slow adv); unpaired devices stay silent until
// the user explicitly enters pairing via the BOOT-button status cycle.
//
// BLE is the backup sync/control path (see ble_sync.h's module docstring)
// -- only one radio needs to be actively reachable at a time, and WiFi
// wins whenever it's up (higher throughput, longer range, see
// poller._get_transport()'s own preference). A prior attempt at this
// exact suppression broke WiFi scan/reconfigure from the Settings page,
// because at the time that only ever went over BLE regardless of WiFi
// state -- fixed now (see device_client.py's WiFi-HTTP scan/connect/status,
// used by app.py whenever WiFi is reachable; BLE is the fallback for
// "device isn't on WiFi at all yet"), so suppressing idle BLE advertising
// while WiFi is connected no longer creates a dead end.
static void resumeIdleAdvertising() {
    if (s_pairingActive) return; // pairing's own fast-adv window owns this
    if (s_paired && !wifi_sync_is_connected()) {
        applyAdvertisingInterval(SLOW_ADV_MIN, SLOW_ADV_MAX);
        NimBLEDevice::startAdvertising();
    } else {
        NimBLEDevice::stopAdvertising();
    }
}

// Call periodically (indicatorTask's 1s tick) -- WiFi's connection state
// can change independent of any BLE-side event (connect, disconnect, pair,
// unpair), so nothing else would otherwise notice "WiFi just connected,
// stop advertising" or "WiFi just dropped, resume as backup" in between
// those events.
void ble_sync_reconcile_advertising() {
    if (s_pairingActive || s_centralConnected) return; // those own advertising state themselves
    resumeIdleAdvertising();
}

bool ble_sync_is_paired() { return s_paired; }

void ble_sync_start_pairing() {
    s_pairingActive = true;
    s_pairingStartedMs = millis();
    applyAdvertisingInterval(FAST_ADV_MIN, FAST_ADV_MAX);
    NimBLEDevice::startAdvertising();
    Serial.println("ble_sync: pairing mode -- fast advertising, 120s timeout");
}

void ble_sync_stop_pairing() {
    if (!s_pairingActive) return;
    s_pairingActive = false;
    resumeIdleAdvertising();
}

bool ble_sync_pairing_timed_out() {
    return s_pairingActive && (millis() - s_pairingStartedMs > PAIRING_TIMEOUT_MS);
}

class ServerCallbacks : public NimBLEServerCallbacks {
    void onConnect(NimBLEServer *server, NimBLEConnInfo &connInfo) override {
        s_centralConnected = true;
        power_mgr_note_activity(); // a real connection resets the idle-sleep clock
        Serial.println("ble_sync: central connected");
        if (s_pairingActive) {
            s_pairingActive = false;
            if (!s_paired) {
                s_paired = true;
                s_pairPrefs.begin("blesync", /*readOnly=*/false);
                s_pairPrefs.putBool("paired", true);
                s_pairPrefs.end();
                face_set_paired(true);
                Serial.println("ble_sync: paired for the first time");
            }
        }
    }
    void onDisconnect(NimBLEServer *server, NimBLEConnInfo &connInfo, int reason) override {
        s_centralConnected = false;
        Serial.println("ble_sync: central disconnected, resuming advertising");
        resumeIdleAdvertising();
    }
};

class ListReadCallbacks : public NimBLECharacteristicCallbacks {
    void onRead(NimBLECharacteristic *chr, NimBLEConnInfo &connInfo) override {
        chr->setValue(buildListJson().c_str());
    }
};

// Lets the dashboard (via the pipeline's BLE client) show live WiFi status
// -- configured/connected/SSID/IP -- without needing the WiFi sync
// transport itself to be up (BLE works regardless of WiFi state, which is
// the whole point of having both paths).
class WifiStatusReadCallbacks : public NimBLECharacteristicCallbacks {
    void onRead(NimBLECharacteristic *chr, NimBLEConnInfo &connInfo) override {
        chr->setValue(wifi_sync_status_json().c_str());
    }
};

// Poll target for a network scan kicked off via the SCANWIFI command
// below -- returns whatever wifi_sync_scan_json() currently has (scanning
// in progress, or the last completed result set).
class WifiScanReadCallbacks : public NimBLECharacteristicCallbacks {
    void onRead(NimBLECharacteristic *chr, NimBLEConnInfo &connInfo) override {
        chr->setValue(wifi_sync_scan_json().c_str());
    }
};

// Rejects path traversal / subdirectories -- recordings are always flat
// files directly under /sdcard. Mirrors wifi_sync.cpp's sanitizedPath().
static bool sanitizedSdPath(const String &name, char *out, size_t outLen) {
    if (name.indexOf('/') >= 0 || name.indexOf("..") >= 0 || name.isEmpty()) return false;
    snprintf(out, outLen, "%s/%s", SDCARD_DIR, name.c_str());
    return true;
}

// Shared by both command sources below (GATT CONTROL characteristic write,
// and -- once the L2CAP path was found to disconnect the channel before
// ever reaching this point, see the L2CAP command comment further down --
// the L2CAP channel's own onRead). Same "GET <name>" / "DELETE <name>"
// text protocol either way.
static void handleCommand(const String &cmd) {
    if (cmd.startsWith("GET ")) {
        String *namePtr = new String(cmd.substring(4));
        xTaskCreatePinnedToCore(transferTask, "bleTransfer", 8 * 1024, namePtr, 3, NULL, 1);
    } else if (cmd.startsWith("NOTIFY ")) {
        // "NOTIFY <title>|<body>" -- the AI-pager push. Shows on the
        // e-paper until BOOT-dismissed and announces with the short click
        // (skipped while recording -- shared I2S; the notification still
        // displays). Payload fits one CONTROL write (MTU 247).
        String payload = cmd.substring(7);
        int sep = payload.indexOf('|');
        String title = (sep >= 0) ? payload.substring(0, sep) : payload;
        String body = (sep >= 0) ? payload.substring(sep + 1) : String("");
        Serial.printf("ble_sync: NOTIFY '%s' / '%s'\n", title.c_str(), body.c_str());
        face_show_notification(title.c_str(), body.c_str());
        recorder_notify_click();
    } else if (cmd.startsWith("SETWIFI ")) {
        // "SETWIFI <ssid>|<password>" -- BLE is the reliable configuration
        // channel for this since it works even when the device has never
        // joined any network at all (unlike the dashboard's own /wifi
        // HTTP endpoint, which needs WiFi already up to be reachable).
        String payload = cmd.substring(8);
        int sep = payload.indexOf('|');
        String ssid = (sep >= 0) ? payload.substring(0, sep) : payload;
        String password = (sep >= 0) ? payload.substring(sep + 1) : String("");
        if (ssid.isEmpty()) {
            Serial.println("ble_sync: SETWIFI with empty SSID, ignored");
        } else {
            wifi_sync_set_credentials(ssid.c_str(), password.c_str());
        }
    } else if (cmd == "CLEARSTATUSES") {
        // Always sent right before a fresh batch of SETSTATUS commands
        // (see ble_device_client.py's send_custom_statuses) -- wipes any
        // stale trailing slots left over from a previously longer list.
        face_clear_custom_statuses();
    } else if (cmd.startsWith("SETSTATUS ")) {
        // "SETSTATUS <index> <icon>|<text>" -- one user-defined custom
        // status slot (Settings dashboard, synced on save). icon is a
        // small int matching CustomStatusIcon in face.h. Persists to NVS
        // immediately (see face_set_custom_status).
        String rest = cmd.substring(10);
        int sp = rest.indexOf(' ');
        if (sp < 0) {
            Serial.println("ble_sync: malformed SETSTATUS, ignored");
        } else {
            int index = rest.substring(0, sp).toInt();
            String payload = rest.substring(sp + 1);
            int sep = payload.indexOf('|');
            uint8_t icon = (uint8_t)((sep >= 0) ? payload.substring(0, sep).toInt() : payload.toInt());
            String text = (sep >= 0) ? payload.substring(sep + 1) : String("");
            face_set_custom_status(index, icon, text.c_str());
        }
    } else if (cmd == "SCANWIFI") {
        // Kicks off an async scan; the Mac side polls the WIFI_SCAN
        // characteristic for results (see WifiScanReadCallbacks above) --
        // no reply on this write itself, matching NOTIFY/SETWIFI's
        // fire-and-forget shape.
        wifi_sync_start_scan();
    } else if (cmd.startsWith("DELETE ")) {
        // The pipeline sends this once it's confirmed a successful
        // download -- for the RAM fallback recording this frees it up
        // immediately; SD-card recordings are a permanent archive and
        // are never deleted this way (the pipeline only ever sends
        // DELETE for the RAM-named file, but this path still refuses
        // to delete outside /sdcard on principle).
        String name = cmd.substring(7);
        if (name == RAM_RECORDING_NAME) {
            recorder_clear_ram();
            Serial.println("ble_sync: RAM recording cleared (pipeline confirmed sync)");
        } else {
            char path[300];
            if (sanitizedSdPath(name, path, sizeof(path))) {
                Serial.printf("ble_sync: DELETE requested for SD file '%s' (ignored -- SD recordings are kept)\n", name.c_str());
            } else {
                Serial.printf("ble_sync: DELETE requested with invalid name '%s'\n", name.c_str());
            }
        }
    } else if (cmd.startsWith("DELETEFORCE ")) {
        // Distinct from plain DELETE above: this one actually erases an SD
        // file, for the dashboard's explicit "delete from device" action
        // (as opposed to a normal dashboard delete, which only removes the
        // local copy and tombstones the name so it's never re-synced --
        // see storage.py's delete_recording() vs
        // delete_recording_from_device()). A real, irreversible action, so
        // it only ever runs when the user explicitly asked for it, never
        // as part of the routine sync-confirm flow.
        String name = cmd.substring(12);
        char path[300];
        if (name == RAM_RECORDING_NAME) {
            recorder_clear_ram();
            Serial.println("ble_sync: DELETEFORCE cleared RAM recording");
        } else if (sanitizedSdPath(name, path, sizeof(path))) {
            if (remove(path) == 0) {
                Serial.printf("ble_sync: DELETEFORCE removed SD file '%s'\n", path);
            } else {
                Serial.printf("ble_sync: DELETEFORCE failed to remove '%s' (not found?)\n", path);
            }
        } else {
            Serial.printf("ble_sync: DELETEFORCE requested with invalid name '%s'\n", name.c_str());
        }
    } else {
        Serial.printf("ble_sync: unrecognized command '%s'\n", cmd.c_str());
    }
}

class ControlWriteCallbacks : public NimBLECharacteristicCallbacks {
    void onWrite(NimBLECharacteristic *chr, NimBLEConnInfo &connInfo) override {
        handleCommand(String(chr->getValue().c_str()));
    }
};

// L2CAP channel carries both the "GET <name>" command AND the resulting
// file bytes, no GATT operation involved at all -- confirmed live that
// opening the channel and then writing "GET <name>" to the separate GATT
// CONTROL characteristic (the original design) made NimBLE-Arduino's L2CAP
// CoC implementation disconnect the channel almost immediately, before
// transferTask() ever ran (no "ble_sync: transferring..." line ever
// appeared). Real NimBLE-Arduino L2CAP CoC + concurrent GATT write
// interaction issue, not an application-level ordering bug -- keeping
// GATT completely out of the picture while the channel is open avoids it
// entirely.
class L2CAPTransferCallbacks : public NimBLEL2CAPChannelCallbacks {
    // Auto-pushes whatever's pending the instant the channel connects --
    // no GET command, no round-trip, nothing ever written into the channel
    // from the Mac side at all. Confirmed live on real hardware that any
    // Mac-initiated write into this channel (whether over the separate
    // GATT CONTROL characteristic, or directly into the channel itself)
    // made NimBLE-Arduino's L2CAP CoC server disconnect almost immediately
    // -- "L2CAP COC 0x0081 connected" followed within milliseconds by
    // "disconnected", every time, before transferTask() ever ran. This
    // auto-push design exists specifically to avoid that: minimize the
    // window between connect and data-flowing-out to the best extent
    // possible, since the disconnect appears to happen shortly after
    // connect regardless of what's done afterward.
    void onConnect(NimBLEL2CAPChannel *channel, uint16_t negotiatedMTU) override {
        s_l2capChannel = channel;
        Serial.printf("ble_sync: L2CAP channel connected, negotiated MTU %u\n", negotiatedMTU);
        String pending;
        if (pickPendingFileName(pending)) {
            String *namePtr = new String(pending);
            xTaskCreatePinnedToCore(transferTask, "bleTransfer", 8 * 1024, namePtr, 3, NULL, 1);
        } else {
            Serial.println("ble_sync: L2CAP channel connected but nothing pending to send");
        }
    }
    void onDisconnect(NimBLEL2CAPChannel *channel) override {
        s_l2capChannel = nullptr;
        Serial.println("ble_sync: L2CAP channel disconnected");
    }
};

void ble_sync_init() {
    // Migration: if this device already has WiFi creds saved (real prior
    // use, before pairing existed), default it to paired rather than
    // stranding it silent/unadvertised until someone notices and cycles to
    // Status::PAIRING.
    s_pairPrefs.begin("blesync", /*readOnly=*/true);
    bool hasStoredPairedFlag = s_pairPrefs.isKey("paired");
    bool storedPaired = s_pairPrefs.getBool("paired", false);
    s_pairPrefs.end();
    s_paired = hasStoredPairedFlag ? storedPaired : wifi_sync_has_credentials();
    face_set_paired(s_paired);

    // MAC-suffixed so multiple units are distinguishable over BLE (the
    // pipeline's /pair page scans for the "EpaperTranscriber" prefix and
    // lists whatever it finds) — a single fixed name would make it
    // impossible to tell two nearby devices apart.
    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_BT);
    char deviceName[32];
    snprintf(deviceName, sizeof(deviceName), "EpaperTranscriber-%02X%02X", mac[4], mac[5]);

    NimBLEDevice::init(deviceName);
    // Request a larger MTU so each notify() carries more bytes -- fewer
    // packets for the same data. Actual negotiated MTU is the minimum of
    // this and whatever the central requests; this just raises our own
    // ceiling. CHUNK_BYTES is fixed to match this value rather than reading
    // the real negotiated MTU at runtime -- deliberately still not wired up
    // to an onMtuChanged() callback even under NimBLE (its signature is
    // knowable now, but this stays deferred/unchanged on purpose: it's
    // outside the scope of a zero-behavior-change migration, and the
    // existing best-effort approach -- still correct, just not maximally
    // fast if a central negotiates less than this -- carries no risk).
    NimBLEDevice::setMTU(247);

    s_server = NimBLEDevice::createServer();
    s_server->setCallbacks(new ServerCallbacks());
    NimBLEService *service = s_server->createService(SERVICE_UUID);

    s_listChar = service->createCharacteristic(LIST_CHAR_UUID, NIMBLE_PROPERTY::READ);
    s_listChar->setCallbacks(new ListReadCallbacks());

    s_controlChar = service->createCharacteristic(CONTROL_CHAR_UUID, NIMBLE_PROPERTY::WRITE);
    s_controlChar->setCallbacks(new ControlWriteCallbacks());

    // No addDescriptor(new BLE2902())-equivalent call needed here (deleted,
    // not translated) -- NimBLE-Arduino auto-manages the CCCD for any
    // characteristic created with the NOTIFY property.
    s_dataChar = service->createCharacteristic(DATA_CHAR_UUID, NIMBLE_PROPERTY::NOTIFY);

    s_wifiStatusChar = service->createCharacteristic(WIFI_STATUS_CHAR_UUID, NIMBLE_PROPERTY::READ);
    s_wifiStatusChar->setCallbacks(new WifiStatusReadCallbacks());

    s_wifiScanChar = service->createCharacteristic(WIFI_SCAN_CHAR_UUID, NIMBLE_PROPERTY::READ);
    s_wifiScanChar->setCallbacks(new WifiScanReadCallbacks());

    service->start();

    // L2CAP CoC fast path for the DATA transfer only -- GET/DELETE commands
    // still go through the CONTROL characteristic above, unchanged. If the
    // Mac client never opens this channel (older client, or CONFIG flag not
    // actually enabled), transferTask() falls straight back to notify().
    NimBLEL2CAPServer *l2capServer = NimBLEDevice::createL2CAPServer();
    l2capServer->createService(L2CAP_PSM, /*mtu=*/517, new L2CAPTransferCallbacks());

    NimBLEAdvertising *advertising = NimBLEDevice::getAdvertising();
    advertising->addServiceUUID(SERVICE_UUID);
    // Paired: advertise immediately at the slow/reconnect interval so an
    // already-set-up device keeps working out of the box. Unpaired: stay
    // silent (battery) until the user explicitly enters Status::PAIRING.
    resumeIdleAdvertising();

    Serial.printf("ble_sync: advertising as \"%s\" (paired=%d)\n", deviceName, (int)s_paired);
}

bool ble_sync_is_connected() {
    return s_centralConnected;
}

bool ble_sync_is_transferring() {
    return s_transferInProgress;
}

