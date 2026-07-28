# epaper_transcriber (Arduino IDE sketch)

Open `epaper_transcriber.ino` directly in Arduino IDE — this whole folder
*is* the sketch. Layout follows the same convention Waveshare's own examples
use: `.cpp`/`.h` files at the sketch root are compiled automatically, and
the `src/` subfolder is recursively compiled too (that's an Arduino IDE
1.8.10+/2.x feature, not unique to this project).

- `epaper_transcriber.ino` — setup()/loop(), button state machine.
- `eyes.{h,cpp}`, `recorder.{h,cpp}`, `wifi_sync.{h,cpp}` — app logic.
- `audio_bsp.{c,h}`, `user_config.h` — Waveshare's audio glue + pin macros.
- `secrets.h` — your WiFi SSID/password (edit before flashing; not committed).
- `src/` — vendored Waveshare BSP (button debounce, e-paper SPI driver,
  ES8311 codec stack, SD card mount, power rail control, I2C).

## One-time Arduino IDE setup

1. **Install ESP32 board support**: File → Preferences → "Additional Boards
   Manager URLs" → add
   `https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json`,
   then Tools → Board → Boards Manager → search "esp32" → install the
   Espressif package (this project was vendored from IDF5-era examples, so
   use a recent version — 3.x).
2. **Select the board**: Tools → Board → esp32 → **"ESP32S3 Dev Module"**.
3. **Board settings** (Tools menu) — this is the part people usually get
   wrong on S3 boards with in-package PSRAM/flash:
   - USB CDC On Boot: **Enabled** (so `Serial` shows up over the Type-C port
     without needing a separate UART bridge)
   - Flash Size: **8MB**
   - Partition Scheme: **8M with spiffs (3MB APP/1.5MB SPIFFS)** or similar —
     anything that isn't the default 4MB-assuming scheme
   - PSRAM: **OPI PSRAM**
   - CPU Frequency: 240MHz (default is fine)
4. **No extra libraries to install** — `WiFi.h` and `WebServer.h` ship with
   the ESP32 board package itself.

## Flash

1. Edit `secrets.h` with your WiFi SSID/password.
2. FAT32-format a microSD card, insert it.
3. Plug in via the Type-C port, select the right Serial port under Tools →
   Port, hit Upload.
4. Tools → Serial Monitor at 115200 baud — watch for
   `wifi_sync: connected, IP=...` to get the address to browse to.

## Known risks (see repo-root README.md for detail)

- `src/codec_board/lcd_init.c` references MIPI-DSI headers from a sibling
  Waveshare board family; if the IDE complains about missing
  `esp_lcd_*dsi`/`ili9881c`/`ek79007` headers, delete that file (and
  `src/codec_board/drv/tca9554.c` if it turns out unused) — this board has
  no MIPI LCD.
