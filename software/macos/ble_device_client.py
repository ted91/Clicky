"""BLE counterpart to device_client.py — talks to the ESP32's GATT server
(see epaper_transcriber/ble_sync.cpp) instead of its HTTP server. Use this
when your Mac can't reliably join the device's WiFi network (e.g. a
dual-band router the ESP32 can't negotiate) but you still want automatic
background sync without giving up your Mac's own internet connection.

Keeps ONE persistent connection alive for the pipeline's whole lifetime,
rather than connecting-checking-disconnecting on every poll — the device
only re-advertises/reconnects if the connection actually drops (device
power-cycled, out of range, etc). This runs its own background event loop
in a dedicated thread so the connection object can outlive any single
poll_once() call; list_recordings()/download_recording() below are the
synchronous entry points poller.py actually calls.

SDK: pip install bleak
"""
import asyncio
import concurrent.futures
import json
import logging
import os
import platform
import threading
import time

import config
import status

log = logging.getLogger("ble_device_client")

# Firmware advertises "EpaperTranscriber-XXXX" (MAC-suffixed, see
# epaper_transcriber/ble_sync.cpp) so multiple units can be told apart —
# match by prefix, not exact name.
DEVICE_NAME_PREFIX = "EpaperTranscriber"
SERVICE_UUID = "e9a10000-1000-4000-8000-00805f9b34fb"
LIST_CHAR_UUID = "e9a10001-1000-4000-8000-00805f9b34fb"
CONTROL_CHAR_UUID = "e9a10002-1000-4000-8000-00805f9b34fb"
DATA_CHAR_UUID = "e9a10003-1000-4000-8000-00805f9b34fb"
WIFI_STATUS_CHAR_UUID = "e9a10004-1000-4000-8000-00805f9b34fb"
WIFI_SCAN_CHAR_UUID = "e9a10005-1000-4000-8000-00805f9b34fb"

# How long to poll after kicking off a scan before giving up -- the
# firmware's WiFi.scanNetworks(async) typically finishes in 2-4s; this just
# needs to comfortably exceed that, not match it exactly.
WIFI_SCAN_POLL_TIMEOUT_SECONDS = 10
WIFI_SCAN_POLL_INTERVAL_SECONDS = 0.5

# Transfer uses notify() (see ble_sync.cpp), not indicate() -- indicate()
# was tried first and measured too slow on real hardware (~250 bytes/sec,
# connection-interval-bound per-chunk ACK round-trips) and was reverted in
# favor of the current fire-and-forget notify() + a paced CHUNK_DELAY_MS on
# the firmware side instead. This timeout needs to be generous enough that a
# still-in-progress (but ultimately successful) transfer doesn't get
# discarded and retried from scratch -- that was the actual bug behind
# "transferring... complete... transferring [same file]..." repeating
# indefinitely at the old 60s value; the device really did finish each time,
# the timeout just gave up before receiving confirmation.
#
# Lowered from 300s to 180s now that the firmware retries a dropped notify()
# instead of silently losing those bytes (see ble_sync.cpp's
# notifyWithRetry), AND the Mac side resumes a stalled transfer from where
# it left off instead of restarting (see _partial_downloads below) -- five
# minutes of dead air per stall was the single biggest contributor to
# "nothing is syncing" on a congested link; with resume in place, a shorter
# timeout only costs one retry cycle, not lost progress.
TRANSFER_TIMEOUT_SECONDS = 180
CALL_TIMEOUT_SECONDS = TRANSFER_TIMEOUT_SECONDS + 15  # covers connect + transfer

# list_recordings()/delete_recording() are quick GATT read/writes, not file
# transfers -- they have no business sharing CALL_TIMEOUT_SECONDS (315s).
# Confirmed live: after the physical device power-cycled, this persistent
# connection's _client.is_connected stayed truthy (bleak/CoreBluetooth
# doesn't always fire a prompt disconnect callback for an abrupt reset, as
# opposed to a graceful disconnect) -- _ensure_connected() trusted that
# stale flag and reused the dead connection, so the next list_recordings()
# call just hung against a connection that would never respond, silently,
# for the FULL 315s before finally timing out and reconnecting. The whole
# poll loop was stuck for that entire window with zero log output, which
# looked exactly like "nothing synced, no error either." A short timeout
# here means a stale connection gets detected and cleared within seconds,
# not minutes.
QUICK_CALL_TIMEOUT_SECONDS = 15

# Hard ceiling on the whole L2CAP fast-path attempt (connect + transfer),
# independent of ble_l2cap_client's own internal timeouts -- see the
# comment at its call site in download_recording() for why this exists.
L2CAP_HARD_TIMEOUT_SECONDS = 60

# --- background event loop, started once, lives for the pipeline's lifetime ---
_loop: asyncio.AbstractEventLoop = None
_loop_thread: threading.Thread = None
_loop_lock = threading.Lock()

_client = None  # persistent bleak.BleakClient, reused across calls
_client_lock = None  # asyncio.Lock, created inside the loop thread

# name -> bytes already received from a stalled transfer, so the next
# attempt can resume instead of restarting from zero (see
# _download_recording_async and ble_sync.cpp's GET <name> <offset>).
_partial_downloads = {}


def _ensure_loop_running():
    global _loop, _loop_thread
    with _loop_lock:
        if _loop is not None:
            return
        _loop = asyncio.new_event_loop()
        _loop_thread = threading.Thread(target=_loop.run_forever, name="ble-loop", daemon=True)
        _loop_thread.start()


def _run_coro(coro, timeout: float = CALL_TIMEOUT_SECONDS):
    _ensure_loop_running()
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result(timeout=timeout)


async def _discover_raw():
    """Returns actual BLEDevice objects (bleak's own type) matching the
    prefix — internal use, so callers can hand them straight to
    BleakClient() without a second scan."""
    from bleak import BleakScanner
    devices = await BleakScanner.discover(timeout=config.BLE_SCAN_TIMEOUT_SECONDS)
    log.info("BLE scan saw %d device(s) total: %s", len(devices), [d.name for d in devices if d.name])
    return [d for d in devices if d.name and d.name.startswith(DEVICE_NAME_PREFIX)]


async def _discover_devices_async():
    matches = await _discover_raw()
    return [{"name": d.name, "address": d.address} for d in matches]


async def _discover_devices_with_diagnostics_async():
    from bleak import BleakScanner
    devices = await BleakScanner.discover(timeout=config.BLE_SCAN_TIMEOUT_SECONDS)
    matches = [d for d in devices if d.name and d.name.startswith(DEVICE_NAME_PREFIX)]
    return [{"name": d.name, "address": d.address} for d in matches], len(devices)


def discover_devices():
    """Used by /pair — a one-off scan, unrelated to the persistent sync
    connection below."""
    return _run_coro(_discover_devices_async())


def discover_devices_with_diagnostics():
    return _run_coro(_discover_devices_with_diagnostics_async())


async def _find_device():
    from bleak import BleakScanner

    # Once paired via /pair, connect straight to the known address — faster
    # and unambiguous even if multiple units are in range. Falls back to a
    # fresh prefix scan (picking the first match) if nothing's paired yet,
    # which is also the common case for a single-unit owner who never
    # bothered with /pair at all.
    if config.PAIRED_BLE_ADDRESS:
        device = await BleakScanner.find_device_by_address(config.PAIRED_BLE_ADDRESS, timeout=config.BLE_SCAN_TIMEOUT_SECONDS)
        if device is not None:
            return device
        log.warning("paired device %s not found, falling back to name scan", config.PAIRED_BLE_ADDRESS)

    matches = await _discover_raw()
    if not matches:
        raise RuntimeError(f"No BLE device matching '{DEVICE_NAME_PREFIX}*' found — is it powered on and in range?")
    if len(matches) > 1:
        log.warning("multiple devices found (%s) and none paired via /pair — using the first one", [d.name for d in matches])
    return matches[0]


async def _wait_for_services(client, timeout_seconds: float = 20.0):
    """bleak 3.x removed get_services() with no replacement that actually
    awaits discovery completion (open upstream issue, no clean async API
    exists) -- connect() returning doesn't guarantee GATT service discovery
    has finished, so the very next characteristic read/write can race it and
    fail with "Service Discovery has not been performed yet". The documented
    community workaround is exactly this: poll client.services (which raises
    until discovery completes) in a short loop.
    """
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    last_exc = None
    attempts = 0
    while True:
        attempts += 1
        try:
            # BleakGATTServiceCollection itself has no __len__ (only
            # __iter__) -- that was the actual bug here (confirmed live:
            # every poll failed with "object of type
            # 'BleakGATTServiceCollection' has no len()", not a real
            # discovery delay). Its .services dict is the right thing to
            # measure.
            n = len(client.services.services)
            if n > 0:
                log.info("service discovery done after %d polls: %d service(s)", attempts, n)
                return
            last_exc = f"services collection is empty (len=0), no exception raised"
        except Exception as e:
            last_exc = f"{type(e).__name__}: {e}"
        if asyncio.get_event_loop().time() > deadline:
            raise RuntimeError(f"BLE service discovery did not complete in time after {attempts} polls -- last state: {last_exc}")
        await asyncio.sleep(0.1)


async def _ensure_connected():
    """Reuses the existing connection if still alive; only scans+connects
    again if there's no connection yet or it's actually dropped."""
    global _client, _client_lock
    if _client_lock is None:
        _client_lock = asyncio.Lock()

    async with _client_lock:
        if _client is not None and _client.is_connected:
            return
        from bleak import BleakClient
        device = await _find_device()
        _client = BleakClient(device)
        await _client.connect()
        await _wait_for_services(_client)
        log.info("ble_device_client: connected (persistent) to %s", device.name)


async def _list_recordings_async():
    await _ensure_connected()
    raw = await _client.read_gatt_char(LIST_CHAR_UUID)
    return json.loads(raw.decode("utf-8"))


async def _download_recording_async(name: str) -> bytes:
    await _ensure_connected()

    # Resume support: a prior attempt at this exact name may have stalled
    # partway (see ble_sync.cpp's GET <name> <offset> handling). Rather than
    # re-downloading bytes already received, pick up where it left off --
    # on a congested link a full restart can lose to the timeout every time,
    # while resuming makes steady forward progress across attempts. Keyed by
    # name only (not content) since a genuinely new recording under a reused
    # name doesn't happen in practice -- device filenames are permanently
    # unique/incrementing (see poller.py's is_known_by_size comment).
    buffer = bytearray(_partial_downloads.get(name, b""))
    resume_offset = len(buffer)
    total_len = None
    done = asyncio.Event()
    last_log = time.monotonic()

    def on_notify(_handle, data: bytearray):
        nonlocal total_len, last_log
        if total_len is None:
            # First packet is always the 4-byte little-endian total length --
            # NOTE: when resuming, this is the REMAINING length (see
            # ble_sync.cpp's transferTask), not the file's full size.
            total_len = resume_offset + int.from_bytes(data[:4], "little")
            if len(data) > 4:
                buffer.extend(data[4:])
            log.info("downloading %s: expecting %d bytes%s", name, total_len,
                      f" (resuming from {resume_offset})" if resume_offset else "")
            status.update(sync_progress_name=name, sync_progress_bytes=len(buffer), sync_progress_total=total_len)
        else:
            buffer.extend(data)
        # Indicate-based transfer can legitimately take minutes on a large
        # file (each chunk is a full ack round-trip) -- log periodically so
        # a slow-but-working transfer is distinguishable from a stuck one
        # instead of just silence until either success or a 300s timeout.
        now = time.monotonic()
        if now - last_log > 5:
            last_log = now
            pct = (len(buffer) / total_len * 100) if total_len else 0
            log.info("downloading %s: %d/%d bytes (%.0f%%)", name, len(buffer), total_len or 0, pct)
        status.update(sync_progress_bytes=len(buffer))
        if total_len is not None and len(buffer) >= total_len:
            done.set()

    await _client.start_notify(DATA_CHAR_UUID, on_notify)
    try:
        cmd = f"GET {name} {resume_offset}" if resume_offset else f"GET {name}"
        await _client.write_gatt_char(CONTROL_CHAR_UUID, cmd.encode("utf-8"))
        await asyncio.wait_for(done.wait(), timeout=TRANSFER_TIMEOUT_SECONDS)
        # Completed cleanly -- nothing left to resume from next time.
        _partial_downloads.pop(name, None)
    except asyncio.TimeoutError:
        # Keep whatever we actually received so the NEXT attempt (this
        # function is only ever called again by poller.py's own retry loop
        # on the next poll cycle) resumes instead of restarting at zero.
        # Only worth keeping if we got the length prefix at all -- otherwise
        # there's no valid resume point (offset 0 is just "start over").
        #
        # Safety valve: if THIS attempt was itself a resume and made zero
        # additional progress (buffer unchanged from resume_offset), don't
        # keep resuming forever -- something's wrong (e.g. the file on
        # device no longer matches what we think, or the link is fully
        # dead), and a stuck resume point would otherwise loop indefinitely.
        # Drop back to a fresh restart instead.
        if buffer and len(buffer) > resume_offset:
            _partial_downloads[name] = bytes(buffer)
            log.info("keeping %d bytes of %s for resume on next attempt", len(buffer), name)
        elif resume_offset:
            log.warning("resume of %s made no progress -- discarding partial, will restart from scratch", name)
            _partial_downloads.pop(name, None)
        raise
    finally:
        # Confirmed live: stop_notify() itself can raise (e.g. the
        # connection already dropped mid-transfer) -- when it does, that
        # exception was skipping the status.update() right after it,
        # leaving the dashboard's sync-progress percentage stuck forever
        # at whatever byte count it last reached, surviving even future
        # successful downloads (which only update sync_progress_bytes/total
        # while a transfer is actively in flight, never touch it
        # otherwise). Clearing progress must happen regardless of whether
        # the notify unsubscribe itself succeeded.
        try:
            await _client.stop_notify(DATA_CHAR_UUID)
        except Exception as e:
            log.debug("stop_notify failed while cleaning up %s transfer: %s", name, e)
        status.update(sync_progress_name=None, sync_progress_bytes=None, sync_progress_total=None)

    log.info("finished downloading %s: %d bytes", name, len(buffer))
    return bytes(buffer[:total_len]) if total_len is not None else bytes(buffer)


async def _send_notification_async(title: str, body: str):
    await _ensure_connected()
    # One CONTROL write must fit the payload (MTU 247 => ~244 usable).
    # '|' is the firmware's title/body separator, so strip it from the
    # fields themselves; the blocky 5x7 font upcases everything anyway.
    title = (title or "").replace("|", " ").strip()[:40]
    body = (body or "").replace("|", " ").strip()[:120]
    await _client.write_gatt_char(
        CONTROL_CHAR_UUID, f"NOTIFY {title}|{body}".encode("utf-8")[:240])


# Must match CustomStatusIcon/MAX_CUSTOM_STATUSES in firmware/src/face.h exactly.
CUSTOM_STATUS_ICON_KEYS = {"round": 0, "closed": 1, "x": 2, "narrow": 3}
MAX_CUSTOM_STATUSES = 5


async def _send_custom_statuses_async(statuses: list):
    await _ensure_connected()
    # CLEARSTATUSES first, always -- wipes any stale trailing slots left
    # over from a previously longer list (see face.cpp's
    # face_clear_custom_statuses(), called from this same command).
    await _client.write_gatt_char(CONTROL_CHAR_UUID, b"CLEARSTATUSES")
    for i, status in enumerate(statuses[:MAX_CUSTOM_STATUSES]):
        icon_key = CUSTOM_STATUS_ICON_KEYS.get(status.get("icon"), 0)
        # '|' is the firmware's icon/text separator (see ble_sync.cpp's
        # SETSTATUS handling) -- same strip-don't-escape posture as NOTIFY.
        text = (status.get("text") or "").replace("|", " ").strip()[:60]
        payload = f"SETSTATUS {i} {icon_key}|{text}".encode("utf-8")[:240]
        await _client.write_gatt_char(CONTROL_CHAR_UUID, payload)


async def _get_wifi_status_async() -> dict:
    await _ensure_connected()
    raw = await _client.read_gatt_char(WIFI_STATUS_CHAR_UUID)
    return json.loads(raw.decode("utf-8"))


async def _set_wifi_credentials_async(ssid: str, password: str):
    await _ensure_connected()
    # '|' is the firmware's ssid/password separator (see ble_sync.cpp's
    # SETWIFI handling) -- WiFi SSIDs/passwords essentially never contain
    # it in practice, so no escaping beyond a straight strip.
    payload = f"SETWIFI {ssid.replace('|', '')}|{password}".encode("utf-8")
    await _client.write_gatt_char(CONTROL_CHAR_UUID, payload)


async def _scan_wifi_networks_async() -> list:
    await _ensure_connected()
    await _client.write_gatt_char(CONTROL_CHAR_UUID, b"SCANWIFI")
    deadline = asyncio.get_event_loop().time() + WIFI_SCAN_POLL_TIMEOUT_SECONDS
    while True:
        raw = await _client.read_gatt_char(WIFI_SCAN_CHAR_UUID)
        result = json.loads(raw.decode("utf-8"))
        if not result.get("scanning"):
            # Strongest signal first -- most useful networks at the top of
            # the dropdown without the dashboard needing its own sort logic.
            return sorted(result.get("networks", []), key=lambda n: n.get("rssi", -999), reverse=True)
        if asyncio.get_event_loop().time() > deadline:
            raise RuntimeError("WiFi scan did not complete in time")
        await asyncio.sleep(WIFI_SCAN_POLL_INTERVAL_SECONDS)


async def _delete_recording_async(name: str):
    await _ensure_connected()
    await _client.write_gatt_char(CONTROL_CHAR_UUID, f"DELETE {name}".encode("utf-8"))


async def _delete_recording_from_sd_async(name: str):
    await _ensure_connected()
    await _client.write_gatt_char(CONTROL_CHAR_UUID, f"DELETEFORCE {name}".encode("utf-8"))


async def _disconnect_for_l2cap_async():
    """The firmware stops advertising the moment a central connects (see
    ble_sync.cpp's onConnect/onDisconnect) and only accepts one connection
    at a time -- so as long as this module's persistent bleak connection is
    holding the device, a separate CoreBluetooth session in
    ble_l2cap_client.py can never discover it via scanning (confirmed live:
    it just times out, the device genuinely isn't advertising). Release the
    bleak connection first so the device resumes advertising and the L2CAP
    client's own scan can find it. _ensure_connected() already lazily
    reconnects bleak on the next call (checks `_client is None or not
    is_connected`), so no explicit reconnect is needed here."""
    global _client
    if _client is not None and _client.is_connected:
        await _client.disconnect()
    _client = None


def is_connected() -> bool:
    """Cheap, non-blocking check of whether the persistent connection is
    currently open -- reads the module state directly, no event-loop
    round-trip. Used by poller._get_transport() to decide whether
    release_connection() has anything to do."""
    return _client is not None and getattr(_client, "is_connected", False)


def release_connection():
    """Fully closes the persistent BLE connection. Called by the poller
    whenever WiFi is the active transport: the ESP32-S3's single radio is
    time-sliced between BLE and WiFi, so holding even an idle BLE
    connection during multi-MB WiFi transfers measurably cuts WiFi
    throughput (same coexistence physics as the BLE-starvation issue in
    the other direction -- see wifi_sync.cpp's BACKOFF_MS comment). One
    connection at a time, BLE strictly as backup. Safe to call anytime:
    the next BLE call lazily reconnects via _ensure_connected(). Never
    raises -- a failed disconnect just abandons the handle (cleared
    regardless), which CoreBluetooth times out on its own."""
    global _client
    if not is_connected():
        _client = None
        return
    try:
        _run_coro(_disconnect_for_l2cap_async(), timeout=QUICK_CALL_TIMEOUT_SECONDS)
        log.info("released persistent BLE connection (WiFi transport active)")
    except Exception as e:
        log.debug("release_connection: disconnect failed (%s) -- clearing handle anyway", e)
        _client = None


def list_recordings():
    """Sync wrapper matching device_client.list_recordings()'s interface.
    Reuses the persistent connection — cheap enough to call on a short poll
    interval since there's no connect/scan overhead once already connected.
    """
    global _client
    try:
        return _run_coro(_list_recordings_async(), timeout=QUICK_CALL_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError:
        # A quick GATT read hanging this long almost certainly means the
        # persistent connection is stale (device rebooted/lost power but
        # bleak never got a disconnect callback -- see
        # QUICK_CALL_TIMEOUT_SECONDS's comment). Clear it so the next call
        # reconnects fresh instead of hanging again on the same dead
        # connection.
        _client = None
        raise


def download_recording(name: str) -> bytes:
    """Sync wrapper matching device_client.download_recording()'s
    interface. Tries the L2CAP CoC fast path first (macOS only, requires
    the optional pyobjc-framework-CoreBluetooth dependency and firmware
    with L2CAP enabled) — falls back to the persistent bleak/notify()
    connection below on ANY failure: not macOS, pyobjc not installed, the
    firmware not offering L2CAP (older build), a connection hiccup,
    whatever. This is a new, unproven path, so failures here should never
    be fatal — they should just mean "use the path that's always worked."
    """
    if platform.system() == "Darwin" and os.environ.get("CLICKY_ENABLE_L2CAP") == "1":
        # Opt-in only (CLICKY_ENABLE_L2CAP=1), NOT default -- disabled after
        # extensive live testing because the L2CAP attempt never once
        # succeeded (NimBLE-Arduino's CoC channel dies the moment data
        # flows, on both 2.4.0 and 2.5.0) while actively making things
        # worse: each attempt disconnects the working persistent GATT
        # connection, forces a rescan/reconnect cycle, and repeated cycles
        # eventually crashed/hung the device itself twice in one session.
        # "No audio syncing at all" was the net effect of leaving this on.
        # The code stays for a future debugging session (likely needs an
        # upstream NimBLE-Arduino fix or a channel-teardown leak hunt);
        # syncs use the proven GATT path unconditionally until then.
        try:
            import ble_l2cap_client
            _run_coro(_disconnect_for_l2cap_async())
            # Hard ceiling independent of ble_l2cap_client's own internal
            # timeouts -- this is new, unproven code (pyobjc/CoreBluetooth's
            # L2CAP API, undocumented run-loop/dispatch-queue bridging) and
            # a bug there should never be able to stall the real sync loop
            # indefinitely. Confirmed live: an earlier bug in that module's
            # run-loop handling caused exactly this (a busy-spin that
            # starved the GIL badly enough to look like a multi-minute
            # hang). Runs in its own thread since there's no cheap way to
            # cancel pyobjc/CoreBluetooth calls mid-flight -- a timeout here
            # abandons that thread (it keeps running until GC) rather than
            # actually killing it, but the important thing is this call
            # returns and the sync loop moves on to the GATT fallback.
            #
            # Deliberately NOT a `with ThreadPoolExecutor() as pool:` block
            # -- confirmed live that this was a second, worse bug: the
            # context manager's __exit__ calls shutdown(wait=True), which
            # blocks until the abandoned thread actually finishes, silently
            # turning this "hard timeout" into a wait for whatever
            # ble_l2cap_client's OWN internal timeout eventually is (up to
            # TRANSFER_TIMEOUT_SECONDS=300s) -- worse than having no hard
            # timeout wrapper at all in that case. shutdown(wait=False) lets
            # this function actually return at the stated timeout while the
            # orphaned thread finishes (or doesn't) on its own.
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            try:
                future = pool.submit(ble_l2cap_client.download_recording, name)
                actual_name, data = future.result(timeout=L2CAP_HARD_TIMEOUT_SECONDS)
            finally:
                pool.shutdown(wait=False)
            # The firmware -- not this client -- picks which pending file to
            # auto-send the instant the L2CAP channel connects (see
            # ble_l2cap_client.download_recording's docstring), so this can
            # differ from what was requested. Should be rare/never in the
            # common single-pending-item case; if it ever happens, this
            # still returns the bytes under the originally-requested `name`
            # (matching download_recording()'s existing sync interface,
            # which callers -- poller.py -- expect to return exactly the
            # bytes for the name they asked for) rather than risk a caller
            # silently mislabeling content under the wrong name.
            if actual_name != name:
                log.warning("L2CAP auto-sent '%s' but '%s' was requested -- multiple pending files? "
                            "Returning bytes under the requested name.", actual_name, name)
            log.info("DOWNLOAD_STRATEGY=l2cap for %s (%d bytes)", name, len(data))
            return data
        except ImportError:
            log.debug("DOWNLOAD_STRATEGY=gatt for %s (pyobjc-framework-CoreBluetooth not installed)", name)
        except concurrent.futures.TimeoutError:
            log.warning("DOWNLOAD_STRATEGY=gatt for %s (L2CAP path exceeded hard timeout of %ds)", name, L2CAP_HARD_TIMEOUT_SECONDS)
        except Exception as e:
            log.warning("DOWNLOAD_STRATEGY=gatt for %s (L2CAP path failed: %s)", name, e)
    else:
        log.debug("DOWNLOAD_STRATEGY=gatt for %s (not macOS)", name)
    return _run_coro(_download_recording_async(name))


def send_notification(title: str, body: str):
    """Pushes an AI-pager notification to the device: shows on the e-paper
    until BOOT-dismissed, announced with a short click (silently skipped by
    the firmware while recording). Quick single write."""
    global _client
    try:
        return _run_coro(_send_notification_async(title, body), timeout=QUICK_CALL_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError:
        _client = None  # see list_recordings()'s comment on this pattern
        raise


def get_wifi_status() -> dict:
    """{"configured","connected","ssid","ip"} from the device -- works over
    BLE regardless of which sync_transport is currently configured, since
    the firmware's BLE service is always up (see wifi_sync.cpp/ble_sync.cpp)."""
    global _client
    try:
        return _run_coro(_get_wifi_status_async(), timeout=QUICK_CALL_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError:
        _client = None
        raise


def send_custom_statuses(statuses: list):
    """Pushes the user's custom status list (Settings -> Custom Statuses,
    each {"icon": "round"|"closed"|"x"|"narrow", "text": "..."}) to the
    device over BLE -- persisted to NVS there (see face.cpp's
    face_set_custom_status), so the BOOT button can cycle through them
    (after the six built-in statuses) even across reboots. Call right
    after a Settings save; safe to call again any time the list changes."""
    global _client
    try:
        return _run_coro(_send_custom_statuses_async(statuses), timeout=QUICK_CALL_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError:
        _client = None
        raise


def set_wifi_credentials(ssid: str, password: str):
    """Pushes new WiFi credentials to the device over BLE (SETWIFI) -- the
    reliable configuration channel since it works even before the device
    has ever joined a network. The device saves them to NVS and starts
    connecting immediately; call get_wifi_status() afterward to poll for
    the result."""
    global _client
    try:
        return _run_coro(_set_wifi_credentials_async(ssid, password), timeout=QUICK_CALL_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError:
        _client = None
        raise


def scan_wifi_networks() -> list:
    """Triggers an async scan on the device and polls until it completes,
    returning [{"ssid","rssi"}, ...] sorted strongest-first. Takes a few
    seconds (scan time) -- callable regardless of sync_transport, same as
    get_wifi_status()."""
    global _client
    try:
        return _run_coro(_scan_wifi_networks_async(),
                          timeout=WIFI_SCAN_POLL_TIMEOUT_SECONDS + QUICK_CALL_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError:
        _client = None
        raise


def delete_recording(name: str):
    """Confirms a successful sync — for the RAM fallback recording this
    actually clears the device's PSRAM buffer; for SD-card recordings the
    firmware ignores it (they're a permanent archive — see ble_sync.cpp's
    DELETE handling). poller.py only calls this for the RAM-named file, but
    it's harmless either way."""
    global _client
    try:
        return _run_coro(_delete_recording_async(name), timeout=QUICK_CALL_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError:
        _client = None  # see list_recordings()'s comment on this pattern
        raise


def delete_recording_from_sd(name: str):
    """Real, irreversible SD-card delete (DELETEFORCE) -- for the
    dashboard's explicit "delete from device" action, distinct from the
    routine sync-confirm delete_recording() above (which never touches SD
    files). Only ever called when the user explicitly asked for it."""
    global _client
    try:
        return _run_coro(_delete_recording_from_sd_async(name), timeout=QUICK_CALL_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError:
        _client = None
        raise
