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
