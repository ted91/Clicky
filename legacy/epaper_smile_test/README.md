# epaper_smile_test

Minimal bring-up sketch: powers the e-paper panel and draws a smiley face.
No audio, SD card, buttons, or WiFi involved — this only exercises the
board's power rail control and e-paper SPI driver, so it's the fastest way
to confirm your wiring, board revision setting, and Arduino IDE toolchain
are all correct before trying the full `epaper_transcriber` sketch.

## Open & flash

Same board settings as the main project (see
`../epaper_transcriber/README.md`): **ESP32S3 Dev Module**, PSRAM = OPI
PSRAM, Flash Size = 8MB, Partition Scheme = 8M w/ spiffs, USB CDC On Boot =
Enabled.

Open `epaper_smile_test.ino` in Arduino IDE, select your board's Serial
port, hit Upload. Watch Serial Monitor (115200 baud) for progress, and the
panel should show a smiley face after a couple seconds — e-paper doesn't
need continuous power to hold the image, so it'll stay on screen even after
you unplug it.

If nothing shows up: check Serial Monitor for `Out of bounds pixel` spam
(driver bug, shouldn't happen) or silence after "powering up display..."
(likely a wiring/SPI issue — double check EPD_BUSY_PIN is actually wired,
since the driver polls it during init/refresh and will hang there).
