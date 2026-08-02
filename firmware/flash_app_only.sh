#!/bin/bash
# Flashes ONLY the app partition (0x10000), never the bootloader (0x0) or
# partition table (0x8000) -- unlike `pio run -t upload`, which rewrites all
# three on every single flash.
#
# Why this exists: confirmed live, twice in a row, that a plain `pio run -t
# upload` wipes the device's WiFi credentials and BLE pairing state even
# though nothing in partitions.csv changed between flashes. NVS itself lives
# at 0x9000-0xdfff, never touched by esptool's write ranges either way -- but
# the firmware's own ensureNvsReady()/loadCredentials() (wifi_sync.cpp,
# face.cpp) call nvs_flash_init() at boot and, per standard ESP-IDF
# behavior, erase-and-reinit NVS whenever it looks incompatible with the
# currently-booted partition layout (ESP_ERR_NVS_NEW_VERSION_FOUND /
# NO_FREE_PAGES). Rewriting the partition table on every boot, even with
# byte-identical partitions.csv, is the one variable that changes between
# the two flashing methods -- OTA pushes (which use esp_ota_ops'
# Update.begin/write/end, touching ONLY the app0/app1 partition, never the
# bootloader or partition table) have never once wiped credentials across
# many pushes this project has done.
#
# So: use THIS script for routine firmware iteration (anything that doesn't
# touch partitions.csv, board_build.partitions, or the bootloader/framework
# version) -- it does exactly what OTA already proves is safe, just over
# USB instead of WiFi. Only fall back to a full `pio run -t upload` (or a
# full chip erase) when the partition table itself actually needs to
# change, and expect to have to re-pair/reconnect afterward when you do.
#
# Usage: ./flash_app_only.sh [/dev/cu.usbmodemXXX]
set -euo pipefail

PORT="${1:-}"
if [ -z "$PORT" ]; then
    PORT=$(ls /dev/cu.usbmodem* 2>/dev/null | head -1)
    if [ -z "$PORT" ]; then
        echo "No /dev/cu.usbmodem* device found -- pass the port explicitly." >&2
        exit 1
    fi
fi

cd "$(dirname "$0")"
~/.platformio/penv/bin/pio run
~/.platformio/penv/bin/esptool --chip esp32s3 --port "$PORT" --baud 460800 \
    --before default-reset --after hard-reset write-flash -z \
    --flash-mode dio --flash-freq 80m --flash-size detect \
    0x10000 .pio/build/esp32-s3-epaper/firmware.bin
