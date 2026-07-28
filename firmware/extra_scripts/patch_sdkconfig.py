"""
Patches the Arduino-ESP32 core's generated sdkconfig.h for NimBLE settings
that PlatformIO's normal build_flags mechanism can't durably override.

Why this exists: several NimBLE-Arduino config macros (nimconfig.h) only
set a default via #ifndef (which respects a -D override) -- but the
framework's own generated sdkconfig.h for this board variant (esp32s3/
qio_opi) unconditionally #defines several of them with no #ifndef guard,
and gets #include'd (via Arduino.h) before nimconfig.h's #ifndef checks
run. A plain build_flags -D silently gets stomped back to sdkconfig.h's
value in every translation unit that includes Arduino.h (verified via
preprocessor dump for the first case found: CONFIG_BT_NIMBLE_L2CAP_COC_MAX_NUM
resolved to 0 despite -D on the command line). There's no supported
PlatformIO/Arduino mechanism to override a value sdkconfig.h defines
unconditionally, so this patches the file directly. Runs on every build
(idempotent -- skips already-patched files) so it self-heals if the
framework package is ever reinstalled or updated to a new version with a
fresh sdkconfig.h.

Current patches:
- CONFIG_BT_NIMBLE_L2CAP_COC_MAX_NUM: 0 -> 1, enables NimBLE-Arduino's
  L2CAP Connection-Oriented Channel API at all (createL2CAPServer() etc
  don't exist without it).
- CONFIG_BT_NIMBLE_MSYS_1_BLOCK_SIZE: 256 -> 512, CONFIG_BT_NIMBLE_MSYS_1_BLOCK_COUNT:
  12 -> 24 -- NimBLE's internal mbuf pool used for L2CAP CoC sends.
  Matches a real, confirmed fix from a NimBLE-Arduino GitHub issue
  (h2zero/NimBLE-Arduino#1049, "Send Failed Could not prepare l2cap packet")
  reporting the exact symptom seen live on this project's hardware: an
  L2CAP CoC channel connecting successfully but failing/disconnecting
  almost immediately once data actually starts flowing through it. The
  default pool is sized for GATT-scale traffic, not L2CAP CoC's larger
  packets.
- CONFIG_LWIP_TCP_SND_BUF_DEFAULT / CONFIG_LWIP_TCP_WND_DEFAULT: 5744/5760
  -> 65535 each. TCP throughput is bounded by roughly window-size / RTT --
  confirmed live that neither a larger app-level write() buffer
  (wifi_sync.cpp's 32KB streaming buffer) nor disabling Nagle
  (setNoDelay(true)) moved a real recording download off ~0.08-0.09 MB/s,
  which is suspiciously close to 5744 bytes / a ~60-70ms RTT -- i.e. the
  default 4-MSS-wide window, not app buffering, was the actual ceiling the
  whole time. Both need raising together: SND_BUF alone doesn't help if
  the receive WND on the other end of lwIP's own accounting stays tiny.
  65535 is the max representable in a plain (non-window-scaled) 16-bit TCP
  window field -- comfortably affordable now that transfers use PSRAM, not
  the small on-chip SRAM these defaults were originally sized for.
"""

import os

Import("env")

# Each entry: (old exact line, new exact line). Order doesn't matter.
PATCHES = [
    (
        "#define CONFIG_BT_NIMBLE_L2CAP_COC_MAX_NUM 0",
        "#define CONFIG_BT_NIMBLE_L2CAP_COC_MAX_NUM 1",
    ),
    (
        "#define CONFIG_BT_NIMBLE_MSYS_1_BLOCK_SIZE 256",
        "#define CONFIG_BT_NIMBLE_MSYS_1_BLOCK_SIZE 512",
    ),
    (
        "#define CONFIG_BT_NIMBLE_MSYS_1_BLOCK_COUNT 12",
        "#define CONFIG_BT_NIMBLE_MSYS_1_BLOCK_COUNT 24",
    ),
    (
        "#define CONFIG_LWIP_TCP_SND_BUF_DEFAULT 5744",
        "#define CONFIG_LWIP_TCP_SND_BUF_DEFAULT 65535",
    ),
    (
        "#define CONFIG_LWIP_TCP_WND_DEFAULT 5760",
        "#define CONFIG_LWIP_TCP_WND_DEFAULT 65535",
    ),
]


def patch_sdkconfig():
    platform = env.PioPlatform()
    libs_dir = platform.get_package_dir("framework-arduinoespressif32-libs")
    if not libs_dir:
        print("patch_sdkconfig: framework-arduinoespressif32-libs package not found, skipping")
        return

    memory_type = env.GetProjectOption("board_build.arduino.memory_type", "qio_opi")
    board = env.BoardConfig()
    mcu = board.get("build.mcu", "esp32s3")

    sdkconfig_path = os.path.join(libs_dir, mcu, memory_type, "include", "sdkconfig.h")
    if not os.path.isfile(sdkconfig_path):
        print("patch_sdkconfig: %s not found, skipping" % sdkconfig_path)
        return

    with open(sdkconfig_path, "r") as f:
        content = f.read()

    changed = False
    for old_line, new_line in PATCHES:
        if new_line in content:
            continue  # already patched
        if old_line not in content:
            print("patch_sdkconfig: expected line %r not found in %s, framework version may "
                  "have changed -- this setting won't apply until the script is updated" % (old_line, sdkconfig_path))
            continue
        content = content.replace(old_line, new_line)
        changed = True
        print("patch_sdkconfig: applied %r -> %r" % (old_line, new_line))

    if changed:
        with open(sdkconfig_path, "w") as f:
            f.write(content)


patch_sdkconfig()
