"""Tiny in-memory live-status store poller.py updates as it works, so the
webpage can show small "device connection" / "sync" indicator dots without
polling the device itself. Not persisted — resets on restart, which is
fine since it's just "what's happening right now", not history.
"""
import threading

_lock = threading.Lock()
_state = {
    "device_connecting": False,   # currently scanning/connecting to the device
    "device_connected": False,    # last connection attempt succeeded
    "sync_in_progress": False,    # currently downloading/transcribing/summarizing a recording
    "sync_ok": True,              # last full poll cycle completed without error
    # Byte-level download progress, updated by ble_device_client.py /
    # ble_l2cap_client.py while a transfer is in flight. None fields mean
    # "no download currently happening" -- the WiFi transport
    # (device_client.py) doesn't report this since its download is a
    # single blocking requests.get(), not chunked, so there's nothing to
    # report incrementally there.
    "sync_progress_name": None,
    "sync_progress_bytes": None,
    "sync_progress_total": None,
    # Which transport the poller most recently resolved ("wifi"/"ble") --
    # shown in the dashboard's status pill so it's self-explanatory which
    # path a sync is using (WiFi now auto-wins whenever reachable).
    "sync_transport_active": None,
}


def update(**kwargs):
    with _lock:
        _state.update(kwargs)


def get_all() -> dict:
    with _lock:
        return dict(_state)
