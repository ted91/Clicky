#include "face.h"
#include <math.h>
#include <string.h>
#include <ctype.h>
#include "user_config.h"

static epaper_driver_display *s_driver = nullptr;
static int s_lastRecording = -1; // -1 = not drawn yet, forces first draw

// --- tiny 5x7 blocky font, just the letters needed for "RECORDING" ---
struct Glyph5x7 {
    char ch;
    uint8_t rows[7]; // each row: bits 4..0 = columns left..right
};

static const Glyph5x7 FONT[] = {
    {'R', {0b11110, 0b10001, 0b10001, 0b11110, 0b10100, 0b10010, 0b10001}},
    {'E', {0b11111, 0b10000, 0b10000, 0b11110, 0b10000, 0b10000, 0b11111}},
    {'C', {0b01111, 0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b01111}},
    {'O', {0b01110, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110}},
    {'D', {0b11110, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b11110}},
    {'I', {0b11111, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b11111}},
    {'N', {0b10001, 0b11001, 0b11001, 0b10101, 0b10101, 0b10011, 0b10001}},
    {'G', {0b01111, 0b10000, 0b10000, 0b10111, 0b10001, 0b10001, 0b01111}},
    {'H', {0b10001, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001}},
    {'P', {0b11110, 0b10001, 0b10001, 0b11110, 0b10000, 0b10000, 0b10000}},
    {'M', {0b10001, 0b11011, 0b10101, 0b10101, 0b10001, 0b10001, 0b10001}},
    {'T', {0b11111, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100}},
    {'F', {0b11111, 0b10000, 0b10000, 0b11110, 0b10000, 0b10000, 0b10000}},
    {'U', {0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110}},
    {'S', {0b01111, 0b10000, 0b10000, 0b01110, 0b00001, 0b00001, 0b11110}},
    {'B', {0b11110, 0b10001, 0b10001, 0b11110, 0b10001, 0b10001, 0b11110}},
    {'Y', {0b10001, 0b10001, 0b01010, 0b00100, 0b00100, 0b00100, 0b00100}},
    {'L', {0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b11111}},
    {'A', {0b01110, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001}},
    {'K', {0b10001, 0b10010, 0b10100, 0b11000, 0b10100, 0b10010, 0b10001}},
    {'W', {0b10001, 0b10001, 0b10001, 0b10101, 0b10101, 0b11011, 0b10001}},
    {'2', {0b01110, 0b10001, 0b00001, 0b00010, 0b00100, 0b01000, 0b11111}},
    {'!', {0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00000, 0b00100}},
    {',', {0b00000, 0b00000, 0b00000, 0b00000, 0b00110, 0b00100, 0b01000}},
    {'.', {0b00000, 0b00000, 0b00000, 0b00000, 0b00000, 0b00110, 0b00110}},
};

static const Glyph5x7 *findGlyph(char c) {
    for (auto &g : FONT) {
        if (g.ch == c) return &g;
    }
    return nullptr;
}

static void drawChar(int x, int y, char c, int scale) {
    const Glyph5x7 *g = findGlyph(c);
    if (!g) return;
    for (int row = 0; row < 7; row++) {
        for (int col = 0; col < 5; col++) {
            if (!(g->rows[row] & (1 << (4 - col)))) continue;
            for (int sy = 0; sy < scale; sy++) {
                for (int sx = 0; sx < scale; sx++) {
                    s_driver->EPD_DrawColorPixel(x + col * scale + sx, y + row * scale + sy, DRIVER_COLOR_BLACK);
                }
            }
        }
    }
}

// Draws text centered horizontally at the given baseline y.
static void drawTextCentered(const char *text, int y, int scale) {
    int len = strlen(text);
    int advance = (5 + 1) * scale; // glyph width + 1-column gap, scaled
    int totalWidth = len * advance - scale; // no trailing gap
    int x = (EPD_WIDTH - totalWidth) / 2;
    for (int i = 0; i < len; i++) {
        drawChar(x + i * advance, y, text[i], scale);
    }
}

// Greedily word-wraps text into centered lines no wider than maxWidth,
// starting at startY with the given line spacing. The font is uppercase
// blocky glyphs only -- lowercase input is upshifted so callers can write
// natural-looking source text.
static void drawTextWrapped(const char *text, int startY, int scale, int lineSpacing, int maxWidth) {
    char buf[128];
    strncpy(buf, text, sizeof(buf) - 1);
    buf[sizeof(buf) - 1] = '\0';
    for (char *p = buf; *p; p++) *p = toupper((unsigned char)*p);

    int advance = (5 + 1) * scale;
    int y = startY;
    char line[40] = "";
    char *word = strtok(buf, " ");

    while (word) {
        char candidate[64];
        if (line[0] == '\0') {
            snprintf(candidate, sizeof(candidate), "%s", word);
        } else {
            snprintf(candidate, sizeof(candidate), "%s %s", line, word);
        }
        int width = (int)strlen(candidate) * advance - scale;
        if (width > maxWidth && line[0] != '\0') {
            drawTextCentered(line, y, scale);
            y += lineSpacing;
            snprintf(line, sizeof(line), "%s", word);
        } else {
            snprintf(line, sizeof(line), "%s", candidate);
        }
        word = strtok(nullptr, " ");
    }
    if (line[0] != '\0') {
        drawTextCentered(line, y, scale);
    }
}

static void drawFilledCircle(int cx, int cy, int r) {
    for (int y = -r; y <= r; y++) {
        int rowW = (int)sqrt((double)(r * r - y * y));
        for (int x = -rowW; x <= rowW; x++) {
            s_driver->EPD_DrawColorPixel(cx + x, cy + y, DRIVER_COLOR_BLACK);
        }
    }
}

static void drawRing(int cx, int cy, int r, int thickness, float fromDeg, float toDeg) {
    for (float deg = fromDeg; deg <= toDeg; deg += 0.4f) {
        float rad = deg * (float)M_PI / 180.0f;
        for (int t = 0; t < thickness; t++) {
            int rr = r + t;
            int x = cx + (int)(rr * cosf(rad));
            int y = cy + (int)(rr * sinf(rad));
            s_driver->EPD_DrawColorPixel(x, y, DRIVER_COLOR_BLACK);
        }
    }
}

// Draws a short horizontal line for a closed/sleepy eye.
static void drawClosedEye(int cx, int cy, int halfWidth) {
    for (int x = -halfWidth; x <= halfWidth; x++) {
        for (int t = -1; t <= 1; t++) {
            s_driver->EPD_DrawColorPixel(cx + x, cy + t, DRIVER_COLOR_BLACK);
        }
    }
}

// Draws an X (used for the "overloaded/out of capacity" status's eyes).
static void drawXEye(int cx, int cy, int r) {
    for (int d = -r; d <= r; d++) {
        for (int t = -1; t <= 1; t++) {
            s_driver->EPD_DrawColorPixel(cx + d, cy + d + t, DRIVER_COLOR_BLACK);
            s_driver->EPD_DrawColorPixel(cx + d, cy - d + t, DRIVER_COLOR_BLACK);
        }
    }
}

// Narrow ellipse eye, used for the "focus" status (squinting/determined).
static void drawNarrowEye(int cx, int cy, int rx) {
    for (int x = -rx; x <= rx; x++) {
        for (int t = -2; t <= 2; t++) {
            s_driver->EPD_DrawColorPixel(cx + x, cy + t, DRIVER_COLOR_BLACK);
        }
    }
}

// Default idle screen: a small smiley up top (leaving room for the
// greeting below it) plus the "Clicky" tagline, word-wrapped. Bottom strip
// stays reserved for the BLE/SYNC indicators (see face_update_indicators).
static const int IDLE_FACE_CY = 40;
static const int IDLE_FACE_RADIUS = 30;

static void drawSmiley() {
    const int cx = EPD_WIDTH / 2;
    const int cy = IDLE_FACE_CY;
    drawRing(cx, cy, IDLE_FACE_RADIUS, 3, 0, 360);
    drawFilledCircle(cx - 15, cy - 9, 5);      // left eye
    drawFilledCircle(cx + 15, cy - 9, 5);      // right eye
    drawRing(cx, cy - 4, 20, 3, 20, 160);      // smiling mouth

    // Scale 2 (2x the original size) -- 5 short lines fit the remaining
    // vertical space between the shrunk face above and the indicator strip
    // pinned at the bottom (see INDICATOR_CLEAR_Y).
    drawTextWrapped("HI! I AM CLICKY, YOUR 2ND BRAIN IN REAL WORLD. COME SAY HELLO!",
                     85, 2, 17, EPD_WIDTH - 12);
}

static void drawRecording() {
    // Small filled "record" dot above the word, classic REC indicator look.
    drawFilledCircle(EPD_WIDTH / 2, 70, 14);
    drawTextCentered("RECORDING", 110, 3);
}

// Each status gets a distinct simple face (reusing the same primitives as
// the idle smiley) plus a short text label underneath. Face is confined to
// the top 70% of the panel (y 0-140) and the label to the bottom 30%
// (y 140-200) so the two never overlap regardless of which status is
// showing.
static const int STATUS_FACE_CY = 65;      // face vertical center, within the top 70%
static const int STATUS_FACE_RADIUS = 55;  // outline radius -- spans y 10-120, safely inside 0-140
static const int STATUS_LABEL_Y = 150;     // label baseline, within the bottom 30%

static void drawStatus(Status status) {
    const int cx = EPD_WIDTH / 2;
    const int cy = STATUS_FACE_CY;
    drawRing(cx, cy, STATUS_FACE_RADIUS, 3, 0, 360);

    switch (status) {
        case Status::DND:
            drawClosedEye(cx - 28, cy - 10, 9);
            drawClosedEye(cx + 28, cy - 10, 9);
            drawRing(cx, cy + 15, 30, 3, 170, 190); // flat/neutral mouth
            drawTextCentered("DND", STATUS_LABEL_Y, 4);
            break;
        case Status::HELLO:
            drawFilledCircle(cx - 28, cy - 10, 8);
            drawFilledCircle(cx + 28, cy - 10, 8);
            drawRing(cx, cy + 8, 32, 4, 15, 165); // big grin
            drawTextCentered("HI", STATUS_LABEL_Y, 4);
            break;
        case Status::OVERLOADED:
            drawXEye(cx - 28, cy - 10, 7);
            drawXEye(cx + 28, cy - 10, 7);
            drawRing(cx, cy + 35, 28, 3, 200, 340); // wavy/frown-ish mouth
            drawTextCentered("NOPE", STATUS_LABEL_Y, 3);
            break;
        case Status::MEETING:
            drawFilledCircle(cx - 28, cy - 10, 7);
            drawFilledCircle(cx + 28, cy - 10, 7);
            drawRing(cx, cy + 15, 28, 3, 200, 340); // neutral/small mouth
            drawTextCentered("BUSY", STATUS_LABEL_Y, 4);
            break;
        case Status::FOCUS:
            drawNarrowEye(cx - 28, cy - 10, 9);
            drawNarrowEye(cx + 28, cy - 10, 9);
            drawRing(cx, cy + 15, 28, 3, 200, 340); // determined flat mouth
            drawTextCentered("FOCUS", STATUS_LABEL_Y, 3);
            break;
        case Status::NONE:
            break; // never called with NONE; see face_update()
    }
}

// Left-aligned text (drawTextCentered's sibling) — used by the bottom
// status-indicator strip, which isn't centered.
static void drawTextAt(int x, int y, const char *text, int scale) {
    int advance = (5 + 1) * scale;
    int len = strlen(text);
    for (int i = 0; i < len; i++) {
        drawChar(x + i * advance, y, text[i], scale);
    }
}

static void drawCheckbox(int x, int y, int size, bool filled) {
    for (int i = 0; i < size; i++) {
        s_driver->EPD_DrawColorPixel(x + i, y, DRIVER_COLOR_BLACK);
        s_driver->EPD_DrawColorPixel(x + i, y + size - 1, DRIVER_COLOR_BLACK);
        s_driver->EPD_DrawColorPixel(x, y + i, DRIVER_COLOR_BLACK);
        s_driver->EPD_DrawColorPixel(x + size - 1, y + i, DRIVER_COLOR_BLACK);
    }
    if (filled) {
        for (int yy = 1; yy < size - 1; yy++) {
            for (int xx = 1; xx < size - 1; xx++) {
                s_driver->EPD_DrawColorPixel(x + xx, y + yy, DRIVER_COLOR_BLACK);
            }
        }
    }
}

static Status s_currentStatus = Status::NONE;

// Small "BLE"/checkbox + "SYNC"/checkbox pair pinned to the very bottom of
// the panel — always visible regardless of what's showing above (idle
// smiley, RECORDING, or a status face). There's no addressable LED on this
// board (the only onboard LED is a fixed-function charge indicator, not
// GPIO-controllable) — this is the closest on-device equivalent.
static bool s_lastBleIndicator = false;
static bool s_lastSyncIndicator = false;
static const int INDICATOR_CLEAR_Y = 180;
static const int INDICATOR_LABEL_Y = 184;
static const int INDICATOR_BOX_Y = 192;
static const int INDICATOR_BOX_SIZE = 8;

// Draws the indicator strip unconditionally (no change-check, no
// EPD_DisplayPart() call) — used both by face_update_indicators() and by
// face_update() itself, so a full-screen redraw doesn't leave the strip
// blank until the next indicator tick.
static void drawIndicatorStrip(bool bleConnected, bool syncActive) {
    for (int y = INDICATOR_CLEAR_Y; y < EPD_HEIGHT; y++) {
        for (int x = 0; x < EPD_WIDTH; x++) {
            s_driver->EPD_DrawColorPixel(x, y, DRIVER_COLOR_WHITE);
        }
    }

    drawTextAt(4, INDICATOR_LABEL_Y, "BLE", 1);
    drawCheckbox(4, INDICATOR_BOX_Y, INDICATOR_BOX_SIZE, bleConnected);

    const char *syncLabel = "SYNC";
    int syncLabelWidth = (int)strlen(syncLabel) * 6 - 1; // (5+1)*scale1 - 1
    drawTextAt(EPD_WIDTH - 4 - syncLabelWidth, INDICATOR_LABEL_Y, syncLabel, 1);
    drawCheckbox(EPD_WIDTH - 4 - INDICATOR_BOX_SIZE, INDICATOR_BOX_Y, INDICATOR_BOX_SIZE, syncActive);
}

void face_init(epaper_driver_display *driver) {
    s_driver = driver;
    s_lastRecording = -1;
    s_currentStatus = Status::NONE;
    s_lastBleIndicator = false;
    s_lastSyncIndicator = false;
}

void face_update(bool recording) {
    if (!s_driver) return;

    // Encode (recording, status) as a single comparable value so any
    // change in either triggers exactly one redraw.
    int wanted = recording ? 1000 : (int)s_currentStatus;
    if (wanted == s_lastRecording) return; // nothing changed, skip redraw
    s_lastRecording = wanted;

    s_driver->EPD_Clear();
    if (recording) {
        drawRecording();
    } else if (s_currentStatus != Status::NONE) {
        drawStatus(s_currentStatus);
    } else {
        drawSmiley();
    }
    // Reapply the last-known indicator state immediately -- otherwise the
    // strip would sit blank for up to a second until indicatorTask's next
    // tick, since EPD_Clear() above just wiped it along with everything else.
    drawIndicatorStrip(s_lastBleIndicator, s_lastSyncIndicator);
    s_driver->EPD_DisplayPart();
}

Status face_next_status() {
    int next = ((int)s_currentStatus + 1) % ((int)Status::FOCUS + 1);
    s_currentStatus = (Status)next;
    return s_currentStatus;
}

void face_clear_status() {
    s_currentStatus = Status::NONE;
}

void face_update_indicators(bool bleConnected, bool syncActive) {
    if (!s_driver) return;
    if (bleConnected == s_lastBleIndicator && syncActive == s_lastSyncIndicator) return;
    s_lastBleIndicator = bleConnected;
    s_lastSyncIndicator = syncActive;

    drawIndicatorStrip(bleConnected, syncActive);
    s_driver->EPD_DisplayPart();
}
