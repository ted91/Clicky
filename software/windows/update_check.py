"""Two independent update paths, both anchored on config.GITHUB_REPO's
Releases:

1. App update -- check_app_update() compares config.APP_VERSION against
   GitHub's latest release tag and returns enough for Settings to show a
   banner + download link. Can't safely auto-download-and-replace itself:
   no paid Apple Developer ID yet (see this project's distribution
   planning), so an unsigned auto-install would just trade one Gatekeeper
   prompt for another with no real gain -- a manual re-run of
   "Setup Clicky.command" is the honest version of this for now.

2. Firmware update -- push_firmware_update_if_needed() compares this app's
   *bundled* firmware.bin (shipped alongside every app release, see
   config.FIRMWARE_DIR) against whatever version the paired device reports
   over its own WiFi HTTP server, and pushes the bundled .bin if newer.
   Deliberately NOT fetched from GitHub directly on the device's behalf --
   the device only ever talks to its already-paired app, never the
   internet directly, for this (see wifi_sync.cpp's /version and /ota
   routes on the firmware side, and this project's OTA design discussion
   for the full reasoning).
"""
import logging
import os
import time

import requests

import config

log = logging.getLogger("update_check")

GITHUB_API_BASE = "https://api.github.com"
_REQUEST_TIMEOUT_SECONDS = 8

# check_app_update() is called from every Settings page render (see
# app.py's _settings_context()), not just a background poll -- cached so a
# page reload doesn't mean a fresh GitHub API round trip every time.
_APP_UPDATE_CACHE_SECONDS = 3600
_app_update_cache = {"result": None, "checked_at": 0.0}


def _parse_version(v: str):
    """"0.1.0" -> (0, 1, 0); tolerates a leading "v" (git tag convention)
    and garbage suffixes by just taking the numeric prefix of each part."""
    v = (v or "").strip().lstrip("vV")
    parts = []
    for part in v.split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def check_app_update() -> dict:
    """Returns {"available": bool, "current": str, "latest": str, "url": str}
    -- "available" is always False (not an error) if the GitHub call fails
    for any reason (private repo without a token, network hiccup, rate
    limit), since this is a nice-to-have banner, not something that should
    ever block or alarm over. Cached for _APP_UPDATE_CACHE_SECONDS since
    this is called from every Settings page render."""
    now = time.monotonic()
    if _app_update_cache["result"] is not None and now - _app_update_cache["checked_at"] < _APP_UPDATE_CACHE_SECONDS:
        return _app_update_cache["result"]

    result = {"available": False, "current": config.APP_VERSION, "latest": None, "url": None}
    try:
        # /releases/latest deliberately excludes prereleases/drafts (GitHub's
        # own documented behavior) -- this project ships prerelease tags at
        # this early stage (see the v0.1.0 release), so that endpoint 404s
        # even though a real release exists. /releases (the list, newest
        # first) has no such filter.
        resp = requests.get(
            f"{GITHUB_API_BASE}/repos/{config.GITHUB_REPO}/releases",
            params={"per_page": 1},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        if not resp.ok:
            log.debug("app update check: GitHub returned %s (repo private without a token?)", resp.status_code)
        else:
            releases = resp.json()
            if releases:
                data = releases[0]
                latest_tag = data.get("tag_name") or ""
                result["latest"] = latest_tag
                result["url"] = data.get("html_url")
                if _parse_version(latest_tag) > _parse_version(config.APP_VERSION):
                    result["available"] = True
    except Exception as e:
        log.debug("app update check failed (non-fatal): %s", e)

    _app_update_cache.update(result=result, checked_at=now)
    return result


def get_bundled_firmware_version() -> str:
    path = os.path.join(config.FIRMWARE_DIR, "version.txt")
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except OSError:
        return "0.0.0"


def push_firmware_update_if_needed(base_url: str) -> bool:
    """Called opportunistically whenever the device is confirmed reachable
    over WiFi (see poller.py's _wifi_base_url_if_reachable) -- checks the
    device's current firmware version against this app's bundled one, and
    pushes the bundled firmware.bin if it's newer. Returns True if a push
    was sent (the device will reboot into it a moment later -- this
    doesn't wait for that). False for "nothing to do" or "failed", both
    non-fatal to the caller's own poll cycle."""
    bundled_version = get_bundled_firmware_version()
    try:
        resp = requests.get(f"{base_url}/version", timeout=5)
        if not resp.ok:
            return False
        device_version = resp.json().get("version") or "0.0.0"
    except Exception as e:
        log.debug("firmware version check failed (non-fatal): %s", e)
        return False

    if _parse_version(bundled_version) <= _parse_version(device_version):
        return False

    firmware_path = os.path.join(config.FIRMWARE_DIR, "firmware.bin")
    try:
        with open(firmware_path, "rb") as f:
            firmware_bytes = f.read()
    except OSError as e:
        log.warning("bundled firmware.bin missing or unreadable: %s", e)
        return False

    log.info("pushing firmware %s -> device (currently %s)", bundled_version, device_version)
    try:
        # Real multipart/form-data (requests builds this automatically for
        # `files=`), not a raw octet-stream body -- confirmed live that the
        # device's WebServer library can't reliably buffer a >1MB raw POST
        # body as a single arg (see wifi_sync.cpp's handleOtaUpload for the
        # full story); its multipart-upload parser is the one that actually
        # works for a payload this size.
        resp = requests.post(
            f"{base_url}/ota",
            files={"firmware": ("firmware.bin", firmware_bytes, "application/octet-stream")},
            timeout=60,
        )
        if not resp.ok:
            log.warning("firmware push rejected by device: %s %s", resp.status_code, resp.text[:200])
            return False
        log.info("firmware push accepted, device is rebooting into it")
        return True
    except Exception as e:
        # A dropped connection here is actually the EXPECTED happy path in
        # one case: the device's own handler calls ESP.restart() right
        # after responding, and a slow/flaky WiFi session can drop the
        # response before this side reads it even though the flash+reboot
        # already succeeded. Logged as non-fatal either way -- worst case
        # this just retries next time WiFi is reachable and the version
        # check above no-ops once the device is actually updated.
        log.info("firmware push request ended without a clean response (%s) -- device may still be rebooting into it", e)
        return False


def force_push_firmware(base_url: str) -> dict:
    """Unconditional firmware push -- the admin fleet page's explicit
    "flash this device" action (see app.py's /admin/flash), unlike
    push_firmware_update_if_needed's opportunistic version-gated check.
    Always uploads the bundled firmware.bin regardless of the device's
    current version, since an admin flashing devices before shipping wants
    every unit on the exact same build, not just "newer than what's
    there"."""
    firmware_path = os.path.join(config.FIRMWARE_DIR, "firmware.bin")
    try:
        with open(firmware_path, "rb") as f:
            firmware_bytes = f.read()
    except OSError as e:
        return {"ok": False, "error": f"bundled firmware.bin missing or unreadable: {e}"}
    try:
        resp = requests.post(
            f"{base_url}/ota",
            files={"firmware": ("firmware.bin", firmware_bytes, "application/octet-stream")},
            timeout=60,
        )
        if not resp.ok:
            return {"ok": False, "error": f"device rejected push: {resp.status_code} {resp.text[:200]}"}
        return {"ok": True}
    except Exception as e:
        # Same expected-happy-path note as push_firmware_update_if_needed:
        # ESP.restart() right after responding can drop the connection even
        # on a successful flash.
        return {"ok": True, "note": f"connection ended without a clean response ({e}) -- device may still be rebooting into it"}
