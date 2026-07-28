# epaper-transcriber

Firmware for the Waveshare **ESP32-S3-ePaper-1.54** (V2, `ESP32-S3-PICO-1-N8R8`,
8MB Flash / 8MB PSRAM) that turns the board into a push-to-record transcription
device:

- **Single-click PWR** → starts recording (mic → WAV on the SD card), e-paper
  shows blinking "listening" eyes.
- **Double-click PWR** → stops recording, saves the WAV file, shows "syncing"
  eyes while it's reachable over WiFi.
- A tiny HTTP server on the board serves recordings at `http://<device-ip>/`
  so a phone/laptop on the same WiFi network can pull the WAV and hand it off
  to a speech-to-text + LLM summarization pipeline (that pipeline is *not*
  part of this firmware — see "What's not here" below).

## Layout

This repo has three top-level pieces:

- **`firmware/`** — the ESP32-S3 device firmware (PlatformIO project).
  - `firmware/src/main.cpp` — state machine wiring buttons, e-paper eyes, recorder, WiFi.
  - `firmware/src/eyes.{h,cpp}` — cute-eyes e-paper animation (idle / listening / syncing).
  - `firmware/src/recorder.{h,cpp}` — streams mic PCM to a WAV file on the SD card.
  - `firmware/src/wifi_sync.{h,cpp}` — WiFi STA + HTTP server exposing recordings.
  - `firmware/lib/board_bsp/` — Waveshare's official board support code (button
    debouncing/multi-click, e-paper SPI driver, ES8311 audio codec stack,
    SD card mount, power-rail control, I2C), vendored from
    [waveshareteam/ESP32-S3-ePaper-1.54](https://github.com/waveshareteam/ESP32-S3-ePaper-1.54)
    `02_Example/Arduino/08_Audio_Test` and `04_SD_Card`, restructured for
    PlatformIO but otherwise unmodified.
  - `firmware/include/secrets.h` — your WiFi credentials (gitignored; edit before flashing).
- **`software/`** — the Clicky pipeline (dashboard, sync, transcription/
  summarization, Notion/Google/Obsidian integrations). Split into two fully
  independent copies, one per platform (no shared code between them going
  forward):
  - `software/macos/` — macOS build (`clicky.spec`, `Setup Clicky.command`,
    `meetingcap/` for meeting audio capture). See
    [`software/macos/README.md`](software/macos/README.md).
  - `software/windows/` — Windows build (`clicky_windows.spec`,
    `Setup Clicky.bat`, `msix/` for a sideloadable installer). See
    [`software/windows/README.md`](software/windows/README.md) and
    [`software/windows/WINDOWS.md`](software/windows/WINDOWS.md).
- **`legacy/`** — superseded code kept for reference only, not part of the
  active build: `legacy/epaper_transcriber/` (the original pre-PlatformIO
  Arduino-IDE version of this firmware) and `legacy/epaper_smile_test/` (an
  unrelated scratch/test sketch).

## Before you flash

1. Edit `firmware/include/secrets.h` with your WiFi SSID/password.
2. Format a TF/micro-SD card as **FAT32** and insert it — recordings are
   saved to `/sdcard/rec_NNN.wav`.
3. Install [PlatformIO](https://platformio.org/) (CLI or the VS Code
   extension).

## Build & flash

```
cd firmware
pio run -t upload
pio device monitor
```

The serial monitor will print the assigned IP address once WiFi connects
(`wifi_sync: connected, IP=...`). Visit `http://<that-ip>/` in a browser to
see/download recordings, or `GET /list` for JSON.

## What's not here (by design)

Speech-to-text and LLM summarization (meeting notes, action items, owners,
calendar entries) are **not** firmware — they belong in whatever pulls the
WAV from the device's HTTP server (a phone app, a small script, etc.). This
project's job stops at: press button → record → save WAV → make it fetchable
over WiFi.

That pipeline lives in [`software/`](software/) — a local Python app that
polls the device, transcribes + summarizes new recordings (pluggable
providers: Mistral/OpenAI/Anthropic/fully-local), and shows the results on
a local webpage. See [`software/README.md`](software/README.md).

## Known risks / things to verify on first flash

- `firmware/lib/board_bsp/src/src/codec_board/lcd_init.c` references MIPI-DSI LCD
  headers used by other Waveshare boards in the same family; it's vendored
  verbatim because Waveshare's own examples compile it as-is for this board,
  but if your PlatformIO toolchain complains about missing
  `esp_lcd_*_dsi`/`ili9881c`/`ek79007` headers, that file (and
  `codec_board/drv/tca9554.c` if unused) can likely be deleted — this board
  doesn't have a MIPI LCD.
- `board_build.arduino.memory_type = qio_opi` in `firmware/platformio.ini` targets the
  **V2** board revision (`ESP32-S3-PICO-1-N8R8`, 8MB/8MB). If you have a
  **V1** board (`ESP32-S3FH4R2`, 4MB Flash/2MB PSRAM, no "V2" silkscreen),
  the vendored BSP examples aren't interchangeable between versions —
  you'll need Waveshare's V1 example set instead (`02_Example/ESP-IDF/V1`)
  and adjusted flash/PSRAM build flags.
