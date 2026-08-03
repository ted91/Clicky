#include "face.h"
#include <math.h>
#include <string.h>
#include <ctype.h>
#include <stdio.h>
#include <Preferences.h>
#include <nvs_flash.h>
#include "user_config.h"
#include "power_mgr.h"
#include "fw_version.h"

static epaper_driver_display *s_driver = nullptr;
static int s_lastRecording = -1; // -1 = not drawn yet, forces first draw

// --- custom status persistence (NVS via Preferences) ------------------
// Same "own namespace, load once at boot, write-through on change" pattern
// as wifi_sync.cpp's WiFi-credentials storage.
struct CustomStatusEntry {
    uint8_t icon = 0;
    char text[64] = "";
};
static CustomStatusEntry s_customStatuses[MAX_CUSTOM_STATUSES];
static int s_customStatusCount = 0;
static Preferences s_customStatusPrefs;

// Same defensive nvs_flash_init() dance as wifi_sync.cpp's ensureNvsReady()
// -- duplicated rather than shared since this module can't assume WiFi's
// init has already run first (task startup order isn't guaranteed).
static void ensureCustomStatusNvsReady() {
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        err = nvs_flash_init();
    }
    if (err != ESP_OK) {
        Serial.printf("face: nvs_flash_init failed (err=%d) -- custom statuses won't persist across reboots\n", (int)err);
    }
}

static void loadCustomStatuses() {
    ensureCustomStatusNvsReady();
    // Fresh device: namespace doesn't exist yet, begin(readOnly=true)
    // fails -- same bail-out-immediately posture as wifi_sync.cpp's
    // loadCredentials() (calling getX()/end() on a failed handle anyway
    // has been confirmed to corrupt Preferences' internal state).
    if (!s_customStatusPrefs.begin("customstat", /*readOnly=*/true)) {
        s_customStatusCount = 0;
        return;
    }
    int count = s_customStatusPrefs.getInt("count", 0);
    if (count < 0) count = 0;
    if (count > MAX_CUSTOM_STATUSES) count = MAX_CUSTOM_STATUSES;
    for (int i = 0; i < count; i++) {
        char iconKey[8], textKey[8];
        snprintf(iconKey, sizeof(iconKey), "icon%d", i);
        snprintf(textKey, sizeof(textKey), "text%d", i);
        s_customStatuses[i].icon = (uint8_t)s_customStatusPrefs.getUChar(iconKey, 0);
        String t = s_customStatusPrefs.getString(textKey, "");
        snprintf(s_customStatuses[i].text, sizeof(s_customStatuses[i].text), "%s", t.c_str());
    }
    s_customStatusPrefs.end();
    s_customStatusCount = count;
}

void face_set_custom_status(int index, uint8_t icon, const char *text) {
    if (index < 0 || index >= MAX_CUSTOM_STATUSES) {
        Serial.printf("face: SETSTATUS index %d out of range, ignored\n", index);
        return;
    }
    s_customStatusPrefs.begin("customstat", /*readOnly=*/false);
    char iconKey[8], textKey[8];
    snprintf(iconKey, sizeof(iconKey), "icon%d", index);
    snprintf(textKey, sizeof(textKey), "text%d", index);
    s_customStatusPrefs.putUChar(iconKey, icon);
    s_customStatusPrefs.putString(textKey, text ? text : "");
    int newCount = index + 1;
    if (newCount > s_customStatusPrefs.getInt("count", 0)) {
        s_customStatusPrefs.putInt("count", newCount);
    }
    s_customStatusPrefs.end();

    s_customStatuses[index].icon = icon;
    snprintf(s_customStatuses[index].text, sizeof(s_customStatuses[index].text), "%s", text ? text : "");
    if (newCount > s_customStatusCount) s_customStatusCount = newCount;
}

void face_clear_custom_statuses() {
    s_customStatusPrefs.begin("customstat", /*readOnly=*/false);
    s_customStatusPrefs.clear();
    s_customStatusPrefs.putInt("count", 0);
    s_customStatusPrefs.end();
    s_customStatusCount = 0;
    // The device may currently be sitting on whatever status (built-in or
    // custom) was last selected via BOOT -- "clear" should mean the screen
    // actually goes back to idle, not just that the list is now empty.
    face_clear_status();
}

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
    // Added for notification text -- notifications carry arbitrary
    // sender/subject strings, unlike the fixed labels above.
    {'V', {0b10001, 0b10001, 0b10001, 0b10001, 0b01010, 0b01010, 0b00100}},
    {'X', {0b10001, 0b10001, 0b01010, 0b00100, 0b01010, 0b10001, 0b10001}},
    {'Z', {0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b10000, 0b11111}},
    {'Q', {0b01110, 0b10001, 0b10001, 0b10001, 0b10101, 0b10010, 0b01101}},
    {'J', {0b00111, 0b00010, 0b00010, 0b00010, 0b00010, 0b10010, 0b01100}},
    {'0', {0b01110, 0b10001, 0b10011, 0b10101, 0b11001, 0b10001, 0b01110}},
    {'1', {0b00100, 0b01100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110}},
    {'3', {0b11110, 0b00001, 0b00001, 0b01110, 0b00001, 0b00001, 0b11110}},
    {'4', {0b00010, 0b00110, 0b01010, 0b10010, 0b11111, 0b00010, 0b00010}},
    {'5', {0b11111, 0b10000, 0b11110, 0b00001, 0b00001, 0b10001, 0b01110}},
    {'6', {0b01110, 0b10000, 0b11110, 0b10001, 0b10001, 0b10001, 0b01110}},
    {'7', {0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b01000, 0b01000}},
    {'8', {0b01110, 0b10001, 0b10001, 0b01110, 0b10001, 0b10001, 0b01110}},
    {'9', {0b01110, 0b10001, 0b10001, 0b01111, 0b00001, 0b00001, 0b01110}},
    {':', {0b00000, 0b00110, 0b00110, 0b00000, 0b00110, 0b00110, 0b00000}},
    {'-', {0b00000, 0b00000, 0b00000, 0b01110, 0b00000, 0b00000, 0b00000}},
    {'?', {0b01110, 0b10001, 0b00001, 0b00010, 0b00100, 0b00000, 0b00100}},
    {'@', {0b01110, 0b10001, 0b10111, 0b10101, 0b10111, 0b10000, 0b01110}},
    {'/', {0b00001, 0b00010, 0b00010, 0b00100, 0b01000, 0b01000, 0b10000}},
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
    drawTextWrapped("HI! I AM CLICKY, YOUR 2ND BRAIN IN REAL WORLD.",
                     85, 2, 17, EPD_WIDTH - 12);
}

// --- notification scene (the "AI pager" surface) ---
// Set via face_show_notification() (from ble_sync's NOTIFY command),
// cleared only by the BOOT button (see main.cpp) -- no auto-dismiss, a
// pager message waits for its human. s_notifSeq is bumped per message so
// face_update()'s change-gating redraws even when one notification
// replaces another while already showing.
static bool s_notifActive = false;
static int s_notifSeq = 0;
static char s_notifTitle[48] = "";
static char s_notifBody[128] = "";
static uint32_t s_notifShownAtMs = 0;
// See face_notification_blocks_sleep()'s doc comment (face.h) for why this
// exists -- bounds how long an undismissed notification can keep the
// device from sleeping, separate from how long it stays visually shown.
static const uint32_t NOTIF_SLEEP_BLOCK_CEILING_MS = 5 * 60 * 1000; // 5 min

static void drawNotification() {
    // Title band: scale 2, up to ~2 wrapped lines. Body below at scale 1
    // (fits ~11 lines down to the indicator strip). A short separator rule
    // between them so the two read as header vs. content.
    drawTextWrapped(s_notifTitle, 8, 2, 17, EPD_WIDTH - 8);
    for (int x = 20; x < EPD_WIDTH - 20; x++) {
        s_driver->EPD_DrawColorPixel(x, 48, DRIVER_COLOR_BLACK);
    }
    drawTextWrapped(s_notifBody, 56, 1, 10, EPD_WIDTH - 8);
}

static void drawRecording() {
    // Small filled "record" dot above the word, classic REC indicator look.
    drawFilledCircle(EPD_WIDTH / 2, 70, 14);
    drawTextCentered("RECORDING", 110, 3);
}

// Jarvis voice-command capture scene -- visually distinct from a plain memo
// recording (ring instead of a filled dot, "JARVIS" label) so it's obvious
// at a glance which button's capture is live.
static void drawJarvis() {
    drawRing(EPD_WIDTH / 2, 70, 16, 4, 0, 360);
    drawTextCentered("JARVIS", 110, 3);
    drawTextWrapped("LISTENING, PRESS AGAIN WHEN DONE.", 128, 1, 10, EPD_WIDTH - 12);
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
        case Status::PAIRING:
            break; // never called with PAIRING; drawPairingSetup() handles that scene instead
        case Status::NONE:
            break; // never called with NONE; see face_update()
        case Status::CUSTOM:
            break; // never called with CUSTOM; drawCustomStatus() handles that scene instead
    }
}

// First-time setup screen -- shown automatically at boot while unpaired
// (see face_set_paired/main.cpp's setup()), and reachable via BOOT cycling
// too as long as pairing hasn't completed yet (see face_next_status()).
// Deliberately skips drawStatus()'s decorative ring/face entirely and uses
// almost the whole panel for actual instructions -- a returning, already-
// paired user never sees this at all, so it doesn't need to match the
// idle smiley's playful aesthetic, just be readable at a glance.
// BOOT long-press (face_clear_status(), see main.cpp) is the "I don't want
// to follow these, just get me to the idle screen" escape hatch.
static void drawPairingSetup() {
    drawTextCentered("SETUP", 8, 2);
    drawTextWrapped(
        "1. OPEN THE CLICKY APP ON YOUR COMPUTER. "
        "2. GO TO SETTINGS - DEVICE - SCAN AND PAIR BLE. "
        "3. ENTER YOUR WIFI NETWORK NAME AND PASSWORD THERE TOO. "
        "HOLD THIS BUTTON TO SKIP.",
        36, 1, 10, EPD_WIDTH - 8);
}

// --- custom status scene (top-third icon, wrapped message below --------
// same aspect ratio as the idle smiley's face+tagline, see drawSmiley()) --
static const int CUSTOM_ICON_CY = 40;
static const int CUSTOM_ICON_RADIUS = 30;

static void drawCustomIcon(uint8_t icon) {
    const int cx = EPD_WIDTH / 2;
    const int cy = CUSTOM_ICON_CY;
    drawRing(cx, cy, CUSTOM_ICON_RADIUS, 3, 0, 360);
    switch ((CustomStatusIcon)icon) {
        case CustomStatusIcon::CLOSED:
            drawClosedEye(cx - 15, cy - 9, 5);
            drawClosedEye(cx + 15, cy - 9, 5);
            break;
        case CustomStatusIcon::X:
            drawXEye(cx - 15, cy - 9, 5);
            drawXEye(cx + 15, cy - 9, 5);
            break;
        case CustomStatusIcon::NARROW:
            drawNarrowEye(cx - 15, cy - 9, 5);
            drawNarrowEye(cx + 15, cy - 9, 5);
            break;
        case CustomStatusIcon::ROUND:
        default:
            drawFilledCircle(cx - 15, cy - 9, 5);
            drawFilledCircle(cx + 15, cy - 9, 5);
            break;
    }
    drawRing(cx, cy - 4, 20, 3, 20, 160); // same smiling mouth as drawSmiley()
}

static void drawCustomStatus(int index) {
    if (index < 0 || index >= s_customStatusCount) return;
    drawCustomIcon(s_customStatuses[index].icon);
    // Same startY/scale/lineSpacing/maxWidth as drawSmiley()'s tagline --
    // proven wrapping/centering behavior, not new.
    drawTextWrapped(s_customStatuses[index].text, 85, 2, 17, EPD_WIDTH - 12);
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

// BOOT-button-function legend — labels what BOOT currently does, since it's
// multi-function depending on state (see main.cpp's bootButtonTask):
// starts/finishes a Jarvis capture when idle/Jarvis-recording, cancels a
// live memo recording instead (started by PWR), or dismisses a pending
// notification. PWR's own "Record"/"Stop"/"Cancel" legend lives in the
// bottom indicator strip instead (see drawIndicatorStrip).
static const int STATUS_LEGEND_LABEL_Y = 148;

static void drawButtonLabels(bool recording, bool jarvisActive, bool notificationActive) {
    // Recording takes priority over a pending notification here too (same
    // precedence as face_update()'s own scene selection).
    const char *statusLabel = jarvisActive ? "STOP"
                             : recording ? "CANCEL"
                             : notificationActive ? "CLEAR"
                             : "JARVIS";
    int statusWidth = (int)strlen(statusLabel) * 6 - 1; // (5+1)*scale1 - 1
    drawTextAt(EPD_WIDTH - 4 - statusWidth, STATUS_LEGEND_LABEL_Y, statusLabel, 1);
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
static bool s_isPaired = false; // see face_set_paired()

// Bottom strip, always visible regardless of what's showing above (idle
// smiley, RECORDING, or a status face): "BLE"/checkbox on the left, "SYNC"/
// checkbox in the middle, and the "Record"/"Stop" PWR-button legend on the
// right (where "SYNC" used to sit -- moved center to make room for it).
// There's no addressable LED on this board (the only onboard LED is a
// fixed-function charge indicator, not GPIO-controllable) -- the BLE/SYNC
// checkboxes are the closest on-device equivalent.
static bool s_lastBleIndicator = false;
static bool s_lastWifiIndicator = false;
static bool s_lastSyncIndicator = false;
static bool s_currentRecording = false; // set by face_update(); read here since
                                         // face_update_indicators()'s own periodic
                                         // caller (main.cpp's indicatorTask) doesn't
                                         // otherwise know recording state
static bool s_currentJarvisActive = false; // set by face_update(); PWR's bottom-strip
                                            // legend shows "CANCEL" instead of "RECORD"/
                                            // "STOP" while a Jarvis capture is live
static const int INDICATOR_CLEAR_Y = 180;
static const int INDICATOR_BOX_SIZE = 8;
// Single row, vertically centering the 8px checkbox and the 7px-tall font
// within the 20px strip -- previously label and checkbox were two separate
// rows (label above, checkbox below) per item, which read as more visually
// disconnected than intended, especially now that there are three items
// instead of two.
static const int INDICATOR_ROW_Y = INDICATOR_CLEAR_Y + (EPD_HEIGHT - INDICATOR_CLEAR_Y - INDICATOR_BOX_SIZE) / 2;
static const int INDICATOR_TEXT_Y = INDICATOR_ROW_Y; // scale-1 glyphs are 7px, close enough to the 8px box to look aligned

// Draws one "[x] LABEL" pair starting at x, returns the x position right
// after it (caller chains items left-to-right with a small gap between).
static int drawIndicatorItem(int x, const char *label, bool on) {
    drawCheckbox(x, INDICATOR_ROW_Y, INDICATOR_BOX_SIZE, on);
    int textX = x + INDICATOR_BOX_SIZE + 3;
    drawTextAt(textX, INDICATOR_TEXT_Y, label, 1);
    int labelWidth = (int)strlen(label) * 6 - 1; // (5+1)*scale1 - 1
    return textX + labelWidth;
}

// Draws the indicator strip unconditionally (no change-check, no
// EPD_DisplayPart() call) — used both by face_update_indicators() and by
// face_update() itself, so a full-screen redraw doesn't leave the strip
// blank until the next indicator tick.
static void drawIndicatorStrip(bool bleConnected, bool wifiConnected, bool syncActive) {
    for (int y = INDICATOR_CLEAR_Y; y < EPD_HEIGHT; y++) {
        for (int x = 0; x < EPD_WIDTH; x++) {
            s_driver->EPD_DrawColorPixel(x, y, DRIVER_COLOR_WHITE);
        }
    }

    // BLE / WIFI / SYNC, one line, left to right -- low-battery text that
    // used to live in this strip is gone; the always-on top-right battery
    // badge (drawBatteryBadge) already covers that, and this strip needed
    // the room for a third checkbox anyway.
    int x = 2;
    x = drawIndicatorItem(x, "BLE", bleConnected) + 8;
    x = drawIndicatorItem(x, "WIFI", wifiConnected) + 8;
    x = drawIndicatorItem(x, "SYNC", syncActive);

    // PWR is the memo Record button, but while a Jarvis capture (BOOT) is
    // live, PWR's role flips to cancelling it -- symmetric with BOOT's own
    // legend flipping to "CANCEL" while a memo recording is live instead.
    const char *recordLabel = s_currentJarvisActive ? "CANCEL" : (s_currentRecording ? "STOP" : "RECORD");
    int recordLabelWidth = (int)strlen(recordLabel) * 6 - 1;
    drawTextAt(EPD_WIDTH - 2 - recordLabelWidth, INDICATOR_TEXT_Y, recordLabel, 1);
}

// --- top-right battery badge, phone-status-bar style -----------------------
// No fuel-gauge/BMS chip on this board (see power_mgr.h) -- pct comes from
// a voltage-only estimate against a LiPo discharge curve, same number
// however big the pack is (a 500mAh cell reports the same % at the same
// cell voltage as a 2000mAh one; capacity only changes how long a given %
// lasts, not the reading itself).
static const int BATTERY_ICON_W = 16;
static const int BATTERY_ICON_H = 9;
static const int BATTERY_NUB_W = 2;
static const int BATTERY_Y = 2;
// Cleared region width, right-aligned -- must cover the worst case ("100%"
// text + icon + nub + margins, ~49px) or a shrinking percentage (e.g.
// 100% -> 90%) would leave a stale pixel sliver at the left edge that the
// narrower redraw doesn't reach.
static const int BATTERY_STRIP_W = 56;

static void drawBatteryIcon(int x, int y, int pct) {
    for (int i = 0; i < BATTERY_ICON_W; i++) {
        s_driver->EPD_DrawColorPixel(x + i, y, DRIVER_COLOR_BLACK);
        s_driver->EPD_DrawColorPixel(x + i, y + BATTERY_ICON_H - 1, DRIVER_COLOR_BLACK);
    }
    for (int j = 0; j < BATTERY_ICON_H; j++) {
        s_driver->EPD_DrawColorPixel(x, y + j, DRIVER_COLOR_BLACK);
        s_driver->EPD_DrawColorPixel(x + BATTERY_ICON_W - 1, y + j, DRIVER_COLOR_BLACK);
    }
    // Positive-terminal nub, classic battery-glyph look.
    int nubY0 = y + BATTERY_ICON_H / 2 - 1;
    for (int j = 0; j < 3; j++) {
        s_driver->EPD_DrawColorPixel(x + BATTERY_ICON_W, nubY0 + j, DRIVER_COLOR_BLACK);
    }
    // Fill proportional to charge, inset 2px inside the outline.
    int innerW = BATTERY_ICON_W - 4;
    int fillW = (innerW * pct) / 100;
    if (pct > 0 && fillW < 1) fillW = 1; // a real sliver at very low % reads as "still has some", not "broken"
    for (int i = 0; i < fillW; i++) {
        for (int j = 2; j < BATTERY_ICON_H - 2; j++) {
            s_driver->EPD_DrawColorPixel(x + 2 + i, y + j, DRIVER_COLOR_BLACK);
        }
    }
}

// Draws the badge unconditionally (no change-check, no EPD_DisplayPart()
// call) -- same "always redraw, caller gates on change" split as
// drawIndicatorStrip(), so face_update() can reapply it after EPD_Clear()
// wipes the panel for a scene change.
static void drawBatteryBadge(int pct) {
    for (int y = 0; y < BATTERY_Y + BATTERY_ICON_H + 1; y++) {
        for (int x = EPD_WIDTH - BATTERY_STRIP_W; x < EPD_WIDTH; x++) {
            s_driver->EPD_DrawColorPixel(x, y, DRIVER_COLOR_WHITE);
        }
    }
    char buf[6];
    snprintf(buf, sizeof(buf), "%d%%", pct);
    int textWidth = (int)strlen(buf) * 6 - 1; // (5+1)*scale1 - 1
    int iconX = EPD_WIDTH - 4 - BATTERY_NUB_W - BATTERY_ICON_W;
    int textX = iconX - 4 - textWidth;
    drawTextAt(textX, BATTERY_Y + 1, buf, 1);
    drawBatteryIcon(iconX, BATTERY_Y, pct);
}

// --- top-left firmware version label ---------------------------------------
// Compile-time constant, unlike the battery badge -- no periodic re-sample
// needed, just reapply it after every EPD_Clear() the same way the battery
// badge/indicator strip already do. Small and unobtrusive (scale 1, dim
// against the corner) since this is a diagnostic/support aid (confirming
// an OTA update actually landed, or reading it off over a support call),
// not something a normal user needs to look at day to day.
static void drawVersionBadge() {
    drawTextAt(2, 2, "v" FW_VERSION, 1);
}

static int s_lastBatteryPct = -1; // -1 = not read yet, skip drawing until the first real sample

static int s_customStatusIndex = -1; // meaningful only when s_currentStatus == Status::CUSTOM

void face_init(epaper_driver_display *driver) {
    s_driver = driver;
    s_lastRecording = -1;
    s_currentStatus = Status::NONE;
    s_lastBleIndicator = false;
    s_lastWifiIndicator = false;
    s_lastSyncIndicator = false;
    s_currentRecording = false;
    s_currentJarvisActive = false;
    s_lastBatteryPct = -1;
    s_customStatusIndex = -1;
    loadCustomStatuses();
}

void face_update(bool recording, bool jarvisActive) {
    if (!s_driver) return;

    // Kept current unconditionally, even on the early-return below --
    // drawIndicatorStrip() reads these directly, and it can be invoked from
    // face_update_indicators()'s own independent periodic tick (see
    // indicatorTask in main.cpp), which has no other way to know recording
    // state.
    s_currentRecording = recording;
    s_currentJarvisActive = jarvisActive;

    // Encode (recording, jarvisActive, notification, status) as a single
    // comparable value so any change in any of them triggers exactly one
    // redraw. Recording outranks a notification (you pressed the button,
    // you know what you're doing); a notification outranks the status face
    // and idle smiley until BOOT-dismissed. 1000 vs 1001 keeps a Jarvis
    // capture's scene distinct from a plain memo recording's. 2000+seq
    // keeps replacing notifications distinct from each other.
    // 3000+index keeps distinct custom statuses distinct from each other
    // (and from the plain (int)Status::CUSTOM value, which alone wouldn't
    // change when cycling between two different custom slots).
    int wanted = jarvisActive ? 1001
               : recording ? 1000
               : s_notifActive ? 2000 + s_notifSeq
               : (s_currentStatus == Status::CUSTOM) ? 3000 + s_customStatusIndex
               : (int)s_currentStatus;
    if (wanted == s_lastRecording) return; // nothing changed, skip redraw
    s_lastRecording = wanted;

    s_driver->EPD_Clear();
    if (jarvisActive) {
        drawJarvis();
    } else if (recording) {
        drawRecording();
    } else if (s_notifActive) {
        drawNotification();
    } else if (s_currentStatus == Status::CUSTOM) {
        drawCustomStatus(s_customStatusIndex);
    } else if (s_currentStatus == Status::PAIRING) {
        drawPairingSetup();
    } else if (s_currentStatus != Status::NONE) {
        drawStatus(s_currentStatus);
    } else {
        drawSmiley();
    }
    drawButtonLabels(recording, jarvisActive, s_notifActive);
    // Reapply the last-known indicator state immediately -- otherwise the
    // strip would sit blank for up to a second until indicatorTask's next
    // tick, since EPD_Clear() above just wiped it along with everything else.
    drawIndicatorStrip(s_lastBleIndicator, s_lastWifiIndicator, s_lastSyncIndicator);
    // Same reasoning as the indicator strip above -- reapply the last-known
    // battery reading so the badge doesn't sit blank until the next
    // periodic battery sample (see face_update_battery()/indicatorTask).
    if (s_lastBatteryPct >= 0) drawBatteryBadge(s_lastBatteryPct);
    drawVersionBadge();
    s_driver->EPD_DisplayPart();
}

Status face_next_status() {
    // Built-in enum values NONE..PAIRING occupy slots [0, builtInCount);
    // custom statuses (loaded from NVS, see face_set_custom_status)
    // continue the same cycle in slots [builtInCount, builtInCount +
    // s_customStatusCount), wrapping back to NONE after the last one. With
    // zero custom statuses this reduces to exactly the old PAIRING-wrapping
    // modulo behavior.
    //
    // PAIRING only occupies a slot while the device hasn't been paired yet
    // (see face_set_paired) -- it's first-time setup instructions, not
    // something a returning user should ever land on by cycling BOOT.
    const int builtInCount = s_isPaired ? (int)Status::NONE + 1 : (int)Status::PAIRING + 1;
    const int totalCount = builtInCount + s_customStatusCount;
    int current = (s_currentStatus == Status::CUSTOM) ? (builtInCount + s_customStatusIndex) : (int)s_currentStatus;
    int next = (current + 1) % totalCount;
    if (next < builtInCount) {
        s_currentStatus = (Status)next;
        s_customStatusIndex = -1;
    } else {
        s_currentStatus = Status::CUSTOM;
        s_customStatusIndex = next - builtInCount;
    }
    return s_currentStatus;
}

Status face_current_status() {
    return s_currentStatus;
}

int face_current_custom_index() {
    return s_customStatusIndex;
}

void face_clear_status() {
    s_currentStatus = Status::NONE;
    s_customStatusIndex = -1;
}

void face_show_pairing_setup() {
    s_currentStatus = Status::PAIRING;
    s_customStatusIndex = -1;
}

void face_set_paired(bool paired) {
    s_isPaired = paired;
    // Pairing just succeeded while the setup screen was showing -- no
    // reason to keep displaying "here's how to pair" once it's done.
    if (paired && s_currentStatus == Status::PAIRING) {
        face_clear_status();
    }
}

void face_show_notification(const char *title, const char *body) {
    snprintf(s_notifTitle, sizeof(s_notifTitle), "%s", title ? title : "");
    snprintf(s_notifBody, sizeof(s_notifBody), "%s", body ? body : "");
    s_notifActive = true;
    s_notifShownAtMs = millis();
    s_notifSeq = (s_notifSeq + 1) % 500; // stays within the 2000..2499 encoding band
}

bool face_notification_active() {
    return s_notifActive;
}

bool face_notification_blocks_sleep() {
    if (!s_notifActive) return false;
    return millis() - s_notifShownAtMs < NOTIF_SLEEP_BLOCK_CEILING_MS;
}

void face_dismiss_notification() {
    s_notifActive = false;
}

void face_update_indicators(bool bleConnected, bool wifiConnected, bool syncActive) {
    if (!s_driver) return;
    if (bleConnected == s_lastBleIndicator && wifiConnected == s_lastWifiIndicator &&
        syncActive == s_lastSyncIndicator) return;
    s_lastBleIndicator = bleConnected;
    s_lastWifiIndicator = wifiConnected;
    s_lastSyncIndicator = syncActive;

    drawIndicatorStrip(bleConnected, wifiConnected, syncActive);
    s_driver->EPD_DisplayPart();
}

void face_update_battery(int pct) {
    if (!s_driver) return;
    if (pct == s_lastBatteryPct) return;
    s_lastBatteryPct = pct;

    drawBatteryBadge(pct);
    s_driver->EPD_DisplayPart();
}
