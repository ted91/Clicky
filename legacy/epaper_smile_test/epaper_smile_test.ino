// Minimal bring-up test: power on the e-paper panel and draw a smiley face.
// If this works, your board, wiring, and Arduino IDE toolchain setup are
// all good — no audio/SD/WiFi involved, just the display.
#include <Arduino.h>
#include <math.h>
#include "user_config.h"
#include "src/power/board_power_bsp.h"
#include "src/display/epaper_driver_bsp.h"

static epaper_driver_display *epd = nullptr;
static board_power_bsp_t power(EPD_PWR_PIN, Audio_PWR_PIN, VBAT_PWR_PIN);

static void drawFilledCircle(int cx, int cy, int r) {
    for (int y = -r; y <= r; y++) {
        int rowW = (int)sqrt((double)(r * r - y * y));
        for (int x = -rowW; x <= rowW; x++) {
            epd->EPD_DrawColorPixel(cx + x, cy + y, DRIVER_COLOR_BLACK);
        }
    }
}

// Draws a circle outline using a fixed-thickness ring, restricted to the
// bottom arc (angles from ~20deg to ~160deg) so it reads as a smiling mouth.
static void drawSmileArc(int cx, int cy, int r, int thickness) {
    for (float deg = 20.0f; deg <= 160.0f; deg += 0.5f) {
        float rad = deg * (float)M_PI / 180.0f;
        for (int t = 0; t < thickness; t++) {
            int rr = r + t;
            int x = cx + (int)(rr * cosf(rad));
            int y = cy + (int)(rr * sinf(rad));
            epd->EPD_DrawColorPixel(x, y, DRIVER_COLOR_BLACK);
        }
    }
}

static void drawSmiley() {
    epd->EPD_Clear();

    const int cx = EPD_WIDTH / 2;   // 100
    const int cy = EPD_HEIGHT / 2;  // 100

    // Face outline
    for (float deg = 0; deg < 360.0f; deg += 0.3f) {
        float rad = deg * (float)M_PI / 180.0f;
        for (int t = 0; t < 3; t++) {
            int r = 85 + t;
            int x = cx + (int)(r * cosf(rad));
            int y = cy + (int)(r * sinf(rad));
            epd->EPD_DrawColorPixel(x, y, DRIVER_COLOR_BLACK);
        }
    }

    // Eyes
    drawFilledCircle(cx - 30, cy - 20, 10);
    drawFilledCircle(cx + 30, cy - 20, 10);

    // Smiling mouth
    drawSmileArc(cx, cy - 10, 45, 4);

    epd->EPD_Display();
}

void setup() {
    Serial.begin(115200);
    delay(200);
    Serial.println("epaper_smile_test: powering up display...");

    power.POWEER_EPD_ON();

    custom_lcd_spi_t cfg = {};
    cfg.cs = EPD_CS_PIN;
    cfg.dc = EPD_DC_PIN;
    cfg.rst = EPD_RST_PIN;
    cfg.busy = EPD_BUSY_PIN;
    cfg.mosi = EPD_MOSI_PIN;
    cfg.scl = EPD_SCK_PIN;
    cfg.spi_host = EPD_SPI_NUM;
    cfg.buffer_len = 5000;
    epd = new epaper_driver_display(EPD_WIDTH, EPD_HEIGHT, cfg);
    epd->EPD_Init();

    Serial.println("epaper_smile_test: drawing smiley...");
    drawSmiley();
    Serial.println("epaper_smile_test: done. If you see a smiley, wiring + toolchain are good.");
}

void loop() {
    // Nothing to do — e-paper holds its image with no power.
    delay(1000);
}
