"""Talks to the ESP32 device's own HTTP server (see epaper_transcriber/wifi_sync.cpp)."""
import requests
import config
import status

TIMEOUT_SECONDS = 15  # small request/response calls (/list, /notify, deletes)

# Downloads use (connect, per-chunk-read) timeouts instead of one blanket
# deadline -- TIMEOUT_SECONDS=15 for a whole multi-MB transfer was too
# tight (a ~3MB recording at pre-optimization firmware speeds took longer
# than that, so the download timed out and restarted from scratch every
# poll cycle, which read as "sync takes forever" on the dashboard). 30s
# with zero bytes arriving is a genuinely dead transfer; a slow-but-moving
# one can take as long as it needs.
DOWNLOAD_TIMEOUT = (5, 30)


def list_recordings():
    """Returns [{"name": str, "size": int}, ...] from the device's GET /list."""
    resp = requests.get(f"{config.DEVICE_BASE_URL}/list", timeout=TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json()


def download_recording(name: str) -> bytes:
    """Fetches the raw WAV bytes for a recording via GET /rec?name=<name>,
    streamed in chunks with live byte-progress reported to status.py (same
    fields the BLE client updates) so the dashboard's sync %% works on the
    WiFi path too. The try/finally ALWAYS clears the progress fields --
    mirroring ble_device_client's fix for the stuck-percentage bug, where a
    cleanup exception could leave a stale %% on the dashboard forever."""
    resp = requests.get(
        f"{config.DEVICE_BASE_URL}/rec",
        params={"name": name},
        stream=True,
        timeout=DOWNLOAD_TIMEOUT,
    )
    resp.raise_for_status()
    total = int(resp.headers.get("Content-Length") or 0) or None
    buffer = bytearray()
    status.update(sync_progress_name=name, sync_progress_bytes=0, sync_progress_total=total)
    try:
        for chunk in resp.iter_content(chunk_size=16384):
            buffer.extend(chunk)
            status.update(sync_progress_bytes=len(buffer))
        return bytes(buffer)
    finally:
        try:
            resp.close()
        except Exception:
            pass
        status.update(sync_progress_name=None, sync_progress_bytes=None, sync_progress_total=None)


def send_notification(title: str, body: str):
    """Pushes an AI-pager notification via POST /notify (see wifi_sync.cpp's
    handleNotify) -- same click+e-paper behavior as the BLE path."""
    resp = requests.post(
        f"{config.DEVICE_BASE_URL}/notify",
        data={"title": (title or "")[:40], "body": (body or "")[:120]},
        timeout=TIMEOUT_SECONDS,
    )
    resp.raise_for_status()


def delete_recording(name: str):
    """Confirms a successful sync via DELETE /rec?name=<name>. For the RAM
    fallback recording this actually clears the device's PSRAM buffer; for
    SD-card recordings the firmware ignores it (they're a permanent archive
    — see wifi_sync.cpp's handleDeleteFile()). poller.py only calls this
    for the RAM-named file, but it's harmless either way.
    """
    resp = requests.delete(
        f"{config.DEVICE_BASE_URL}/rec",
        params={"name": name},
        timeout=TIMEOUT_SECONDS,
    )
    resp.raise_for_status()


def signal_sync_complete():
    """Tells the device it can turn its WiFi radio off now (see
    wifi_sync.cpp's handleSynced()/wifi_sync_radio_off()) -- the firmware
    now gates the radio off by default and only turns it on for the
    duration of a sync session (either this explicit confirmation or a
    120s no-HTTP-traffic fallback on the device side). Best-effort: the
    fallback timeout covers a missed/failed call, so callers should not
    treat a failure here as fatal to the sync cycle."""
    resp = requests.post(f"{config.DEVICE_BASE_URL}/synced", timeout=TIMEOUT_SECONDS)
    resp.raise_for_status()


def delete_recording_from_sd(name: str):
    """Real, irreversible SD-card delete via DELETE /rec?name=<name>&force=true
    (see wifi_sync.cpp's handleDeleteFile()) -- for the dashboard's explicit
    "delete from device" action, distinct from the routine sync-confirm
    delete_recording() above (which never touches SD files)."""
    resp = requests.delete(
        f"{config.DEVICE_BASE_URL}/rec",
        params={"name": name, "force": "true"},
        timeout=TIMEOUT_SECONDS,
    )
    resp.raise_for_status()


# --- WiFi status/scan/connect over the device's own WiFi HTTP server -------
# Mirrors ble_device_client's equivalents, same endpoints wifi_sync.cpp
# already exposes for exactly this. Added so Settings' WiFi actions don't
# have to go over BLE when WiFi is already up and reachable -- see
# app.py's routes, which now try this module first via
# poller.get_device_firmware_version()'s same reachability check, falling
# back to BLE only when WiFi genuinely isn't reachable. This is the other
# half of "BLE is backup-only, not a second always-on connection" (see
# ble_sync.cpp's resumeIdleAdvertising(), which now only advertises while
# WiFi is NOT connected).
WIFI_SCAN_POLL_TIMEOUT_SECONDS = 10
WIFI_SCAN_POLL_INTERVAL_SECONDS = 0.5


def get_wifi_status() -> dict:
    resp = requests.get(f"{config.DEVICE_BASE_URL}/wifi/status", timeout=TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json()


def scan_wifi_networks() -> list:
    import time
    resp = requests.post(f"{config.DEVICE_BASE_URL}/wifi/scan", timeout=TIMEOUT_SECONDS)
    resp.raise_for_status()
    deadline = time.monotonic() + WIFI_SCAN_POLL_TIMEOUT_SECONDS
    while True:
        resp = requests.get(f"{config.DEVICE_BASE_URL}/wifi/scan", timeout=TIMEOUT_SECONDS)
        resp.raise_for_status()
        result = resp.json()
        if not result.get("scanning"):
            return sorted(result.get("networks", []), key=lambda n: n.get("rssi", -999), reverse=True)
        if time.monotonic() > deadline:
            raise RuntimeError("WiFi scan did not complete in time")
        time.sleep(WIFI_SCAN_POLL_INTERVAL_SECONDS)


def set_wifi_credentials(ssid: str, password: str):
    resp = requests.post(
        f"{config.DEVICE_BASE_URL}/wifi/connect",
        data={"ssid": ssid, "password": password},
        timeout=TIMEOUT_SECONDS,
    )
    resp.raise_for_status()


def get_device_info(base_url: str = None) -> dict:
    """{"chip_id": str, "name": str, "version": str} -- chip_id is the
    ESP32's stable factory-programmed identity (see wifi_sync.cpp's
    chipIdHex()), name is the user-set friendly label (NVS). Takes an
    explicit base_url (unlike most functions in this module, which default
    to config.DEVICE_BASE_URL) so the admin fleet page (app.py's /admin)
    can query any device on the LAN by IP, not just the one currently
    paired to this app install."""
    resp = requests.get(f"{base_url or config.DEVICE_BASE_URL}/device/info", timeout=TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json()


def set_device_name(name: str, base_url: str = None):
    resp = requests.post(
        f"{base_url or config.DEVICE_BASE_URL}/device/name",
        data={"name": name},
        timeout=TIMEOUT_SECONDS,
    )
    resp.raise_for_status()


def send_jarvis_audio(wav_bytes: bytes, base_url: str = None):
    """Uploads a Jarvis spoken reply for on-device playback via POST
    /jarvis/audio (see wifi_sync.cpp's handleJarvisAudioUpload/Complete) --
    same multipart/form-data idiom as update_check.py's firmware push.
    WiFi-only in Phase 1 (no BLE equivalent -- see the firmware endpoint's
    own doc on payload size), so base_url is required rather than falling
    back to config.DEVICE_BASE_URL silently; jarvis.send_audio_reply()
    already checked WiFi reachability before calling this."""
    resp = requests.post(
        f"{base_url or config.DEVICE_BASE_URL}/jarvis/audio",
        files={"audio": ("reply.wav", wav_bytes, "audio/wav")},
        timeout=60,
    )
    resp.raise_for_status()
