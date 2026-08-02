"""USB (serial) firmware flashing for the /admin panel -- distinct from
update_check.force_push_firmware's OTA-over-WiFi push, which only works on
a device that already has firmware running and a known IP. This module
covers the case OTA can't: a brand-new/blank chip that's never run any
firmware, which has no bootloader/partition-table/app yet and must be
flashed over USB.

Uses `esptool` directly against three pre-built binaries (bootloader.bin,
partitions.bin, firmware.bin) rather than shelling out to `pio run -t
upload` against the firmware source tree -- those three files are ordinary
build artifacts (this project's .pio/build/esp32-s3-epaper/ produces them
on every `pio run`), so bundling them into config.FIRMWARE_DIR alongside
the app-partition firmware.bin already shipped for OTA means this feature
works in the actual packaged app for any user, not just a dev checkout
with the whole PlatformIO toolchain installed. Offsets (0x0/0x8000/0x10000)
match firmware/partitions.csv and flash_app_only.sh's proven-working
esptool invocation -- verify against that file if partitions.csv ever
changes.

Falls back to a settings-configured override folder (BINARIES_OVERRIDE_KEY)
containing the same three filenames if the bundled ones are missing or
stale relative to a local rebuild -- e.g. pointing at
firmware/.pio/build/esp32-s3-epaper/ directly during firmware development,
or a folder copied from an SD card / another machine's build.
"""
import glob
import logging
import os
import shutil
import subprocess

import config
import settings

log = logging.getLogger("firmware_flash")

FLASH_TIMEOUT_SECONDS = 180  # a full flash (bootloader+partitions+app) is slower than the app-only OTA path
BINARIES_OVERRIDE_KEY = "firmware_binaries_path"
BINARY_NAMES = ("bootloader.bin", "partitions.bin", "firmware.bin")

# Must match firmware/partitions.csv (bootloader/partition-table offsets are
# fixed by the ESP-IDF/PlatformIO toolchain for this chip, app0 offset comes
# from partitions.csv itself) -- see flash_app_only.sh for the same offsets
# used in the proven-working app-only flash.
FLASH_OFFSETS = {"bootloader.bin": "0x0", "partitions.bin": "0x8000", "firmware.bin": "0x10000"}


def _has_all_binaries(path: str) -> bool:
    return bool(path) and all(os.path.isfile(os.path.join(path, name)) for name in BINARY_NAMES)


def firmware_binaries_dir() -> str | None:
    """config.FIRMWARE_DIR (bundled with the app, works for any user) wins
    if it has all three files; else a settings override (dev convenience --
    point at a fresh .pio/build/ output, or a folder copied from
    elsewhere) if that's valid instead."""
    if _has_all_binaries(config.FIRMWARE_DIR):
        return config.FIRMWARE_DIR
    override = (settings.get_all().get(BINARIES_OVERRIDE_KEY) or "").strip()
    if _has_all_binaries(override):
        return override
    return None


def esptool_binary_path() -> str | None:
    """Matches flash_app_only.sh's proven-working invocation -- PlatformIO's
    penv bundles esptool, which isn't reliably on PATH for a GUI-launched
    .app (unlike a terminal shell, which sources the user's profile)."""
    default_path = os.path.expanduser("~/.platformio/penv/bin/esptool")
    if os.path.isfile(default_path):
        return default_path
    return shutil.which("esptool") or shutil.which("esptool.py")


def list_serial_ports() -> list:
    """USB-serial device paths a connected ESP32 board would show up as on
    macOS. No pyserial dependency added just for this -- a glob over the
    handful of vendor driver naming conventions we've actually seen this
    hardware enumerate as is sufficient and avoids bundling another
    package into the packaged app for a dev-only feature."""
    patterns = ["/dev/cu.usbmodem*", "/dev/cu.usbserial*", "/dev/cu.wchusbserial*", "/dev/cu.SLAB_USBtoUART*"]
    ports = []
    for pattern in patterns:
        ports.extend(glob.glob(pattern))
    return sorted(set(ports))


def flash_full(port: str, binaries_dir_override: str = None) -> dict:
    """Full flash (bootloader + partition table + app) via esptool --
    the only way to get firmware onto a chip that's never run any before
    (see flash_app_only.sh's docstring for why an app-only flash is
    preferred for an already-provisioned device instead: it wipes WiFi/BLE
    NVS state that a full flash doesn't need to touch, but a blank chip has
    no such state to preserve anyway, and no bootloader to skip flashing).
    Synchronous and slow (a minute or two) -- acceptable for this
    occasional, manual, single-device action; FastAPI runs sync routes in
    a threadpool so this doesn't block the app's event loop.

    binaries_dir_override, if given and valid, is persisted to settings
    (see the /admin route) so it only needs to be entered once per
    machine."""
    if binaries_dir_override and binaries_dir_override.strip():
        candidate = binaries_dir_override.strip()
        if not _has_all_binaries(candidate):
            return {"ok": False, "error": f"'{candidate}' doesn't contain all three of "
                                           f"{', '.join(BINARY_NAMES)}"}
        settings.update(**{BINARIES_OVERRIDE_KEY: candidate})
        binaries_dir = candidate
    else:
        binaries_dir = firmware_binaries_dir()
    if not binaries_dir:
        return {"ok": False, "error": f"no firmware binaries found (looked in the app's bundled "
                                       f"firmware/ folder) -- enter a folder containing "
                                       f"{', '.join(BINARY_NAMES)} below"}
    esptool = esptool_binary_path()
    if not esptool:
        return {"ok": False, "error": "esptool not found -- install it (pip install esptool) "
                                       "or check ~/.platformio/penv/bin/esptool"}
    if not port:
        return {"ok": False, "error": "no serial port selected"}

    write_flash_args = []
    for name in BINARY_NAMES:
        write_flash_args += [FLASH_OFFSETS[name], os.path.join(binaries_dir, name)]

    try:
        proc = subprocess.run(
            [esptool, "--chip", "esp32s3", "--port", port, "--baud", "460800",
             "--before", "default-reset", "--after", "hard-reset", "write-flash", "-z",
             "--flash-mode", "dio", "--flash-freq", "80m", "--flash-size", "detect",
             *write_flash_args],
            capture_output=True, text=True, timeout=FLASH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        return {"ok": False, "error": f"flash timed out after {FLASH_TIMEOUT_SECONDS}s",
                "output": (e.stdout or "") + (e.stderr or "")}
    except OSError as e:
        return {"ok": False, "error": f"failed to launch esptool: {e}"}

    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        log.warning("USB flash failed (port=%s, returncode=%s)", port, proc.returncode)
        return {"ok": False, "error": f"esptool exited with code {proc.returncode}", "output": output}
    return {"ok": True, "output": output}
