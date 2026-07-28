#ifndef WIFI_SYNC_H
#define WIFI_SYNC_H

#include <Arduino.h>

// Joins WiFi (credentials from NVS, settable at runtime -- see
// wifi_sync_set_credentials()/the BLE SETWIFI command in ble_sync.cpp; falls
// back to secrets.h's compile-time defaults if nothing's been saved yet) and
// starts an HTTP server exposing the SD card's recordings so a phone/laptop
// on the same network can pull them:
//   GET /                 -> simple HTML list of recordings with links
//   GET /list             -> JSON array of {"name","size"} for scripting
//   GET /rec?name=<file>  -> raw WAV bytes for a given recording
//   DELETE /rec?name=<file> -> remove a recording once it's been synced
//   POST /notify (title=,body=) -> AI-pager push (parity with ble_sync's
//     NOTIFY command), for the wifi sync-transport path
//   GET /wifi/status      -> JSON {configured,connected,ssid,ip}
//   POST /wifi/connect (ssid=,password=) -> save new credentials + reconnect
//   POST /wifi/scan       -> kick off an async network scan
//   GET /wifi/scan        -> JSON {scanning,networks:[{ssid,rssi}]} -- poll after POST
//
// Call once from setup() after sdcard_init(). Non-blocking: WiFi connects
// (and reconnects, indefinitely, with backoff -- see wifi_sync_tick()) in
// the background; the server simply won't serve anything until connected.
void wifi_sync_init();

// Call frequently from loop()/a task; handles incoming HTTP requests and
// drives the connection/reconnection state machine.
void wifi_sync_tick();

bool wifi_sync_is_connected();

// True if WiFi credentials have ever been saved (NVS or secrets.h
// fallback) -- used by ble_sync.cpp as a migration heuristic: a device
// already in real use before the BLE-pairing feature existed shouldn't be
// stranded unpaired (silent, unadvertised) after a reflash.
bool wifi_sync_has_credentials();

// True while a GET /rec transfer is actively streaming data out.
bool wifi_sync_is_transferring();

// --- radio session control (battery) ------------------------------------
// The radio is OFF by default. wifi_sync_radio_on() starts a bounded "sync
// session" (called by main.cpp when a recording finishes, and internally
// when credentials change); it ends when the Mac POSTs /synced or after
// 120s with no HTTP traffic. `why` is for the serial log.
void wifi_sync_radio_on(const char *why);
void wifi_sync_radio_off(const char *why);
bool wifi_sync_radio_is_on();

// main.cpp hands over its wifiTask handle so wifi_sync_radio_on() can wake
// the task out of its radio-off block (ulTaskNotifyTake) immediately
// instead of waiting for a timeout.
void wifi_sync_set_task_handle(TaskHandle_t handle);

// Saves new credentials to NVS and kicks off a connection attempt with
// them immediately (drops any current connection first). Callable from
// ble_sync.cpp's SETWIFI command handler -- BLE is the reliable
// configuration channel since it works even when the device has never
// joined any network.
void wifi_sync_set_credentials(const char *ssid, const char *password);

// JSON status blob for the BLE WIFI_STATUS characteristic / HTTP
// /wifi/status: {"configured":bool,"connected":bool,"ssid":"...","ip":"..."}
String wifi_sync_status_json();

// Kicks off an async network scan (non-blocking -- WiFi.scanNetworks(true)
// under the hood) if one isn't already running. Safe to call regardless of
// current connect/backoff state. Poll wifi_sync_scan_json() for results.
void wifi_sync_start_scan();

// JSON blob for the BLE WIFI_SCAN characteristic / HTTP /wifi/scan:
// {"scanning":bool,"networks":[{"ssid":"...","rssi":N}, ...]} -- networks
// is only populated once scanning is false and a scan has actually
// completed at least once since boot.
String wifi_sync_scan_json();

#endif
