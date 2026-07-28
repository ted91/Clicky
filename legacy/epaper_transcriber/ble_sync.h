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

#endif
