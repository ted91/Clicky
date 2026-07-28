#ifndef FACE_H
#define FACE_H

#include "src/display/epaper_driver_bsp.h"

// BOOT-button-selectable status faces, shown when idle (recording always
// takes visual priority regardless of status). NONE = default idle smiley.
enum class Status {
    NONE,
    DND,         // "DND"   -- do not disturb, focusing
    HELLO,       // "HI"    -- available, say hi
    OVERLOADED,  // "NOPE"  -- swamped / out of capacity (the fun one)
    MEETING,     // "BUSY"  -- in a meeting
    FOCUS,       // "FOCUS" -- deep work, minimal interruptions
};

void face_init(epaper_driver_display *driver);

// Call frequently; only actually redraws the panel when something visible
// changed, since e-paper refreshes are slow (~0.3s partial) and there's no
// point re-pushing the same image every tick.
void face_update(bool recording);

// BOOT button: single click advances to the next status in the list
// (wrapping back to NONE after the last one); returns the new status.
Status face_next_status();

// BOOT button long-press: jumps straight back to NONE (default smiley)
// regardless of current status.
void face_clear_status();

// Small always-on-screen "BLE"/"SYNC" checkbox indicators pinned to the
// bottom of the panel, independent of face_update()'s own redraw cycle —
// call this periodically (e.g. every 1-2s) so connection/sync state stays
// current even when nothing else visible has changed.
void face_update_indicators(bool bleConnected, bool syncActive);

#endif
