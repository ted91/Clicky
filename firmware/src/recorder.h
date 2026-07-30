#ifndef RECORDER_H
#define RECORDER_H

#include <Arduino.h>

// Streams mic audio (via audio_playback_read) to a WAV file on the SD card
// when one is present. Without an SD card, falls back to buffering into
// PSRAM instead so recording still works — capped at RECORDER_RAM_MAX_SECONDS
// of audio (see recorder.cpp) since 8MB of PSRAM has to hold everything
// else too. Get an SD card in for anything longer than a quick voice memo.

void recorder_init();

// Starts a new recording task; filename is derived from an index so
// consecutive recordings don't collide. Safe to call again only after
// recorder_stop() has fully finished (recorder_is_recording() == false).
// Plays a short click through the speaker before recording begins.
// isCommand=true records a Jarvis voice command instead of a memo -- saved
// as cmd_NNN.wav (own NVS-persisted counter, see recorder.cpp) instead of
// rec_NNN.wav, excluded from FIFO eviction, and meant to be deleted from SD
// by the host right after processing rather than kept as a permanent
// archive.
void recorder_start(bool isCommand = false);

// True if the most recently completed recording was a Jarvis command
// (started with isCommand=true) rather than a memo.
bool recorder_last_was_command();

// Signals the recording task to finalize the WAV header and close out.
// Returns immediately; poll recorder_is_recording() to know when it's done.
// The same click sound plays once the recording has actually finished.
void recorder_stop();

// Like recorder_stop(), but DISCARDS the audio entirely -- the SD file is
// deleted (its rec_NNN index freed for reuse) or the PSRAM buffer is left
// unmarked, so nothing is saved, offered for sync, or transcribed. As if
// the recording never happened. Returns immediately; poll
// recorder_is_recording() to know when the task has fully wound down.
// A descending two-tone plays instead of the normal stop click, so a
// cancel is audibly distinct from a save.
void recorder_cancel();

// True if the most recent recording ended via recorder_cancel() (nothing
// was kept). Reset by the next recorder_start().
bool recorder_was_cancelled();

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

// Short damped notification tick (distinct from the recording beep).
// Silently skipped while a recording is in progress -- record and playback
// share the I2S peripheral. Blocks ~18ms.
void recorder_notify_click();

// Plays a WAV buffer (header + 16-bit PCM, any channel count matching the
// codec's open format -- see audio_bsp.c) through the speaker, chunked so a
// button press can preempt it. Skipped entirely if a recording is in
// progress (same I2S-sharing constraint as recorder_notify_click()).
// Blocks for the duration of playback; call from a dedicated low-priority
// task (see main.cpp's jarvisAudioTask), never from the button/HTTP tasks.
void recorder_play_wav(const uint8_t *data, size_t len);

#endif
