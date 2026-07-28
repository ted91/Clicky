#ifndef RECORDER_H
#define RECORDER_H

#include <Arduino.h>

// Streams mic audio (via audio_playback_read) to a WAV file on the SD card
// when one is present. Without an SD card, falls back to buffering into
// PSRAM instead — downsampled to mono (16kHz/mono/16-bit) to double capacity
// vs. the old stereo buffer: 120s in ~3.75MB instead of 60s. SD recordings
// are unlimited duration and always stay stereo.

void recorder_init();

// Starts a new recording task; filename is derived from an index so
// consecutive recordings don't collide. Safe to call again only after
// recorder_stop() has fully finished (recorder_is_recording() == false).
// Plays a short click through the speaker before recording begins.
void recorder_start();

// Signals the recording task to finalize the WAV header and close out.
// Returns immediately; poll recorder_is_recording() to know when it's done.
// The same click sound plays once the recording has actually finished.
void recorder_stop();

bool recorder_is_recording();

// True if the most recently completed recording was written to the SD
// card; false if it landed in the PSRAM fallback buffer instead.
bool recorder_last_was_sd();

// Path of the most recently completed SD recording, or "" if the last
// recording used the RAM fallback (or none has completed yet).
String recorder_last_file();

// Valid after a RAM-fallback recording completes (recorder_last_was_sd()
// == false): returns the complete WAV file (header + PCM) living in
// PSRAM, for wifi_sync to serve directly over HTTP. Returns nullptr/0 if
// there's no RAM recording available.
const uint8_t *recorder_ram_wav_data(size_t *outLen);

// Call once the pipeline has confirmed it successfully downloaded the RAM
// recording -- frees it up (recorder_ram_wav_data() returns nullptr again)
// so the device doesn't keep re-offering already-synced audio and PSRAM is
// ready for the next recording. Only affects the RAM fallback; SD-card
// recordings are a permanent archive and are never deleted this way (see
// ble_sync.cpp/wifi_sync.cpp's DELETE handling, and recorder.cpp's
// automatic FIFO eviction for when the SD card itself runs low on space).
void recorder_clear_ram();

#endif
