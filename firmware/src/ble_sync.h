#ifndef BLE_SYNC_H
#define BLE_SYNC_H

// BLE peripheral exposing recordings for sync, alongside (not instead of)
// wifi_sync's HTTP server. Point of this: a background process on your Mac
// can pull recordings over BLE without your Mac ever having to join the
// device's network or give up its own WiFi/internet connection, unlike
// WiFi-AP-mode sync — useful when your home router won't let the ESP32
// join over 2.4GHz cleanly. Trade-off: much lower throughput than WiFi, but
// fine for short voice memos.
//
// GATT layout (see ble_sync.cpp for exact UUIDs):
//   LIST    (read)   -> JSON array of {"name","size"}, same shape as
//                        wifi_sync's GET /list, computed fresh on every read
//   CONTROL (write)  -> write ASCII "GET <name>" to start a transfer
//   DATA    (notify) -> first packet: 4-byte little-endian total length;
//                        subsequent packets: raw file bytes, chunked to fit
//                        the negotiated MTU, paced with a small delay
//                        between packets. Tried indicate() for BLE-protocol-
//                        level acknowledgment (fixes rare first-packet-loss
//                        corruption) but measured ~250 bytes/sec on real
//                        hardware -- unusably slow (hours for a large
//                        recording), since indicate()'s per-chunk round-trip
//                        is bound by the connection interval, and reliably
//                        shortening that from the peripheral side is a
//                        known-flaky operation on this library. Back to
//                        notify() for real throughput; the pipeline
//                        validates the RIFF/WAVE header after download and
//                        silently retries next poll cycle if corrupt --
//                        occasional retries beat every transfer taking
//                        hours.

void ble_sync_init();

// True while a BLE central (the pipeline) is actively connected.
bool ble_sync_is_connected();

// True while a GET transfer is actively streaming data out.
bool ble_sync_is_transferring();

// --- pairing / advertising power policy (battery) -----------------------
// "Paired" (an NVS flag, see ble_sync.cpp) persists forever once set --
// it just means "a Mac has connected to this device before," not a BLE
// bond. Advertising behavior differs by state:
//   unpaired  -> silent at boot; only advertises (fast interval, general
//                discoverable) while Status::PAIRING is showing
//   paired    -> advertises continuously whenever not connected, but at a
//                slow interval (~10-20x less radio duty than the old
//                always-fast advertising), so a dropped connection
//                (Mac reboot/sleep/out of range) can still reconnect on
//                its own -- no need to re-pair.
bool ble_sync_is_paired();

// Called by main.cpp when BOOT-cycling into/out of Status::PAIRING.
void ble_sync_start_pairing();  // fast advertising, ~120s auto-timeout
void ble_sync_stop_pairing();   // back to normal (slow-adv if paired, silent if not)

// BLE is a backup sync/control path, not a peer to WiFi -- call
// periodically (e.g. indicatorTask's 1s tick) so idle BLE advertising
// stops the moment WiFi is actually connected, and resumes automatically
// once WiFi disconnects or its radio session ends. No-op during active
// pairing or an active BLE connection -- those already own their own
// advertising state.
void ble_sync_reconcile_advertising();

// True once a fast-pairing window's ~120s timeout has elapsed without a
// connection -- main.cpp polls this to know when to drop Status::PAIRING
// back to NONE on its own (independent of BOOT-button activity).
bool ble_sync_pairing_timed_out();

#endif
