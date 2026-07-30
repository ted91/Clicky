#include "recorder.h"
#include "audio_bsp.h"
#include "power_mgr.h"
#include "sdcard/sdcard_bsp.h"
#include <stdio.h>
#include <math.h>
#include <string.h>
#include <dirent.h>
#include "esp_vfs_fat.h"
#include "ff.h"
#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <Preferences.h>

static const char *SDCARD_DIR = "/sdcard";

// Safety margin kept free on the SD card -- comfortably more than a typical
// voice memo needs (a few minutes at 16kHz/stereo/16-bit ~= 1.9MB/min), so
// a new recording almost never runs out of room mid-write. Checked before
// starting a recording, not continuously during one -- see recordToSd().
static const uint64_t SD_MIN_FREE_BYTES = 5UL * 1024 * 1024;

// Must match audio_play_init()'s esp_codec_dev_sample_info_t in audio_bsp.c
// (16kHz / stereo / 16-bit) — the codec is opened once at boot with that
// config, so every WAV we write has to declare the same format even though
// we only really care about one channel's worth of speech.
static const uint32_t SAMPLE_RATE = 16000;
static const uint16_t CHANNELS = 2;
static const uint16_t BITS_PER_SAMPLE = 16;
static const size_t CHUNK_BYTES = 2048;

// Recordings shorter than this are almost always an accidental PWR
// double-tap (start immediately followed by stop) rather than real speech
// -- silently discarded the same way a cancelled recording is, so a stray
// press doesn't leave a near-empty file for sync/transcription to choke on.
static const uint32_t MIN_RECORDING_DATA_BYTES = SAMPLE_RATE * CHANNELS * (BITS_PER_SAMPLE / 8); // 1 second

// PSRAM fallback cap when there's no SD card — 60s of 16kHz/stereo/16-bit
// audio is ~3.75MB, which comfortably fits alongside everything else in
// 8MB of PSRAM. Insert an SD card for anything longer than a quick memo.
static const uint32_t RECORDER_RAM_MAX_SECONDS = 60;
static const size_t RECORDER_RAM_MAX_BYTES =
    (size_t)RECORDER_RAM_MAX_SECONDS * SAMPLE_RATE * CHANNELS * (BITS_PER_SAMPLE / 8);

static TaskHandle_t s_recTask = nullptr;
static volatile bool s_stopRequested = false;
static volatile bool s_cancelRequested = false; // discard instead of save -- see recorder_cancel()
static volatile bool s_recording = false;
static bool s_lastWasSd = false;
static bool s_lastWasCommand = false;
static bool s_currentIsCommand = false;
static String s_lastFile = "";
static int s_recIndex = 0;

// cmd_NNN.wav files are deleted from SD by the host right after processing
// (see wifi_sync.cpp's force-delete route), unlike rec_NNN.wav which is a
// permanent archive -- so ensureRecIndexInitialized()'s "scan the card for
// the highest existing index" approach (see its own comment) cannot work
// for commands: once every cmd file is gone, a scan would find nothing and
// reset the counter back to 0, and the host's name+size dedup would then
// silently skip a reused cmd_000.wav that happens to match an old one's
// size -- the exact bug ensureRecIndexInitialized() was written to fix for
// rec_ files, reintroduced for cmd_ files if this counter isn't persisted
// independently. Stored in NVS instead, incremented monotonically.
static Preferences s_cmdPrefs;
static int s_cmdIndex = -1; // -1 == not yet loaded from NVS

static void ensureCmdIndexInitialized() {
    if (s_cmdIndex >= 0) return;
    s_cmdPrefs.begin("jarvis", false);
    s_cmdIndex = s_cmdPrefs.getInt("cmdIndex", 0);
}

static uint8_t *s_ramBuf = nullptr;      // header + PCM, preallocated once
static size_t s_ramUsedBytes = 0;        // header + PCM written so far
static bool s_ramHasRecording = false;

struct __attribute__((packed)) WavHeader {
    char riff[4] = {'R', 'I', 'F', 'F'};
    uint32_t chunkSize = 0; // filled in on close
    char wave[4] = {'W', 'A', 'V', 'E'};
    char fmt[4] = {'f', 'm', 't', ' '};
    uint32_t fmtSize = 16;
    uint16_t audioFormat = 1; // PCM
    uint16_t numChannels = CHANNELS;
    uint32_t sampleRate = SAMPLE_RATE;
    uint32_t byteRate = SAMPLE_RATE * CHANNELS * (BITS_PER_SAMPLE / 8);
    uint16_t blockAlign = CHANNELS * (BITS_PER_SAMPLE / 8);
    uint16_t bitsPerSample = BITS_PER_SAMPLE;
    char data[4] = {'d', 'a', 't', 'a'};
    uint32_t dataSize = 0; // filled in on close
};

// Plays a short tone sequence through the speaker. Only ever called from
// the record task's own thread (never concurrently with an in-progress
// audio_playback_read()), since the codec's record/playback channels share
// the underlying I2S peripheral and aren't safe to drive from two tasks at
// once.
static void playTones(const int *toneMs, const float *toneHz, int count) {
    const int16_t amplitude = 9000;
    for (int t = 0; t < count; t++) {
        int samples = (SAMPLE_RATE * toneMs[t]) / 1000;
        int16_t *buf = (int16_t *)heap_caps_malloc(samples * CHANNELS * sizeof(int16_t), MALLOC_CAP_SPIRAM);
        if (!buf) return;
        for (int i = 0; i < samples; i++) {
            int16_t s = (int16_t)(amplitude * sinf(2.0f * (float)M_PI * toneHz[t] * i / SAMPLE_RATE));
            buf[i * 2] = s;
            buf[i * 2 + 1] = s;
        }
        audio_playback_write(buf, samples * CHANNELS * sizeof(int16_t));
        heap_caps_free(buf);
    }
}

// Same short click for both starting and stopping a recording -- audible
// confirmation of the button press even though the e-paper takes a beat to
// redraw. Blocks ~90ms.
static void playClick() {
    const int ms[] = {90};
    const float hz[] = {700.0f};
    playTones(ms, hz, 1);
}

// Descending two-tone for a cancelled recording -- audibly distinct from
// the save click so the user knows the audio was discarded, not kept.
static void playCancelTone() {
    const int ms[] = {80, 120};
    const float hz[] = {600.0f, 380.0f};
    playTones(ms, hz, 2);
}

static uint64_t sdFreeBytes() {
    // sys/statvfs.h isn't available in the Arduino-ESP32 toolchain.
    // esp_vfs_fat_info() would be the tidy wrapper, but it isn't declared in
    // this PlatformIO build's (older) arduino-esp32 core -- fall back to the
    // underlying FatFs f_getfree() call directly, which has been stable
    // across ESP-IDF versions since long before esp_vfs_fat_info existed.
    // "0:" is the drive number ESP-IDF assigns to the first (and only,
    // here) FAT volume mounted via esp_vfs_fat_sdmmc_mount in sdcard_bsp.cpp.
    FATFS *fs = nullptr;
    DWORD freeClusters = 0;
    if (f_getfree("0:", &freeClusters, &fs) != FR_OK || fs == nullptr) {
        return UINT64_MAX; // can't tell -- don't block recording on a stat failure
    }
    return (uint64_t)freeClusters * fs->csize * 512; // sector size is fixed at 512 bytes
}

// FIFO eviction: while free space is below the safety margin, delete the
// lowest-numbered rec_NNN.wav (oldest, since indices only increase) until
// there's room again or nothing's left to delete. Recordings are meant to
// be a permanent archive -- this only kicks in as a last resort when the
// card is genuinely running out of room, never as routine cleanup.
static void evictOldestSdFileIfNeeded() {
    while (sdFreeBytes() < SD_MIN_FREE_BYTES) {
        DIR *dir = opendir(SDCARD_DIR);
        if (!dir) return;

        int lowestIndex = INT32_MAX;
        char lowestName[64] = "";
        struct dirent *entry;
        while ((entry = readdir(dir)) != nullptr) {
            int idx;
            if (sscanf(entry->d_name, "rec_%d.wav", &idx) == 1 && idx < lowestIndex) {
                lowestIndex = idx;
                strncpy(lowestName, entry->d_name, sizeof(lowestName) - 1);
            }
        }
        closedir(dir);

        if (lowestIndex == INT32_MAX) {
            Serial.println("recorder: SD card low on space but no recordings left to evict");
            return;
        }

        char path[300];
        snprintf(path, sizeof(path), "%s/%s", SDCARD_DIR, lowestName);
        Serial.printf("recorder: SD card low on space, evicting oldest recording %s (FIFO)\n", lowestName);
        remove(path);
    }
}

// Confirmed live: s_recIndex started at 0 on every single boot (plain RAM
// variable, never persisted to NVS) with no scan of what's already on the
// card -- across a session with many firmware reflashes, this repeatedly
// reset the counter back to 0, so brand-new recordings landed on
// rec_000.wav/rec_001.wav/etc., colliding with names from recordings the
// pipeline had already synced-and-forgotten (or the user had deleted) at
// a lower index. The pipeline's own dedup (by name+size, or by an
// explicit delete tombstone -- see storage.py) then correctly refused to
// treat the "new" file as new, since as far as it could tell that name
// was already accounted for -- so the genuinely new audio silently never
// synced at all. Scanning the card once, lazily (can't run this in
// recorder_init() -- that runs before sdcard_init() in main.cpp's boot
// sequence, so the card isn't mounted yet there), fixes this permanently:
// starts one past whatever's actually the highest index present, on SD or
// not, boot or not.
static bool s_recIndexInitialized = false;

static void ensureRecIndexInitialized() {
    if (s_recIndexInitialized) return;
    s_recIndexInitialized = true;

    DIR *dir = opendir(SDCARD_DIR);
    if (!dir) return;
    int highestIndex = -1;
    struct dirent *entry;
    while ((entry = readdir(dir)) != nullptr) {
        int idx;
        if (sscanf(entry->d_name, "rec_%d.wav", &idx) == 1 && idx > highestIndex) {
            highestIndex = idx;
        }
    }
    closedir(dir);

    if (highestIndex >= 0) {
        s_recIndex = highestIndex + 1;
        Serial.printf("recorder: found existing recordings up to rec_%03d.wav -- next recording starts at rec_%03d.wav\n",
                      highestIndex, s_recIndex);
    }
}

static void recordToSd(uint8_t *chunkBuf) {
    char path[64];
    if (s_currentIsCommand) {
        ensureCmdIndexInitialized();
        snprintf(path, sizeof(path), "%s/cmd_%03d.wav", SDCARD_DIR, s_cmdIndex++);
        s_cmdPrefs.putInt("cmdIndex", s_cmdIndex);
        // Commands are deleted from SD by the host right after processing
        // (see wifi_sync.cpp) rather than kept as a permanent archive, so
        // they're deliberately excluded from evictOldestSdFileIfNeeded()'s
        // FIFO (its scan only ever matches rec_%d.wav) and from the
        // rec_-index scan/eviction below.
    } else {
        ensureRecIndexInitialized();
        evictOldestSdFileIfNeeded();
        snprintf(path, sizeof(path), "%s/rec_%03d.wav", SDCARD_DIR, s_recIndex++);
    }

    FILE *f = fopen(path, "wb");
    if (!f) {
        Serial.printf("recorder: failed to open %s, falling back to RAM\n", path);
        return; // caller (recordTask) will notice s_lastWasSd stays false
    }

    // Batch SD writes through a 16KB stdio buffer (battery): newlib's
    // default stdio buffer is tiny, so each 2048-byte fwrite otherwise hits
    // the SD card (its high-power write state) every ~64ms. With 16KB
    // buffering the card is touched ~8x less often for the same data. The
    // 2048-byte codec reads are untouched -- I2S pacing, not SD pacing.
    // PSRAM keeps internal RAM free; falls back to newlib's default
    // buffering if the alloc ever fails (still correct, just unbatched).
    static uint8_t *s_sdWriteBuf = nullptr;
    if (!s_sdWriteBuf) s_sdWriteBuf = (uint8_t *)heap_caps_malloc(16 * 1024, MALLOC_CAP_SPIRAM);
    if (s_sdWriteBuf) setvbuf(f, (char *)s_sdWriteBuf, _IOFBF, 16 * 1024);

    WavHeader header; // placeholder written now, patched on close
    fwrite(&header, sizeof(header), 1, f);

    uint32_t totalDataBytes = 0;
    Serial.printf("recorder: recording to %s\n", path);
    while (!s_stopRequested) {
        audio_playback_read(chunkBuf, CHUNK_BYTES);
        fwrite(chunkBuf, 1, CHUNK_BYTES, f);
        totalDataBytes += CHUNK_BYTES;
    }

    header.dataSize = totalDataBytes;
    header.chunkSize = 36 + totalDataBytes;
    fseek(f, 0, SEEK_SET);
    fwrite(&header, sizeof(header), 1, f);
    fclose(f);

    if (s_cancelRequested) {
        // Cancelled mid-recording (the other button, see main.cpp) -- delete
        // the file and free its index for reuse, as if it never happened.
        // s_lastWasSd stays as the caller set it (recordTask's optimistic
        // true) but s_lastFile stays empty, and nothing advertises a new
        // recording since the file is gone before any sync can list it.
        remove(path);
        if (s_currentIsCommand) {
            s_cmdIndex--;
            s_cmdPrefs.putInt("cmdIndex", s_cmdIndex);
        } else {
            s_recIndex--;
        }
        s_lastFile = "";
        Serial.printf("recorder: recording cancelled, deleted %s\n", path);
        return;
    }

    if (totalDataBytes < MIN_RECORDING_DATA_BYTES) {
        // Same treatment as an explicit cancel -- see MIN_RECORDING_DATA_BYTES.
        remove(path);
        if (s_currentIsCommand) {
            s_cmdIndex--;
            s_cmdPrefs.putInt("cmdIndex", s_cmdIndex);
        } else {
            s_recIndex--;
        }
        s_lastFile = "";
        Serial.printf("recorder: discarded %s (%lu bytes, under 1s -- likely an accidental press)\n",
                      path, (unsigned long)totalDataBytes);
        return;
    }

    Serial.printf("recorder: saved %s (%lu bytes audio)\n", path, (unsigned long)totalDataBytes);
    s_lastFile = String(path);
    s_lastWasSd = true;
    s_lastWasCommand = s_currentIsCommand;
}

static void recordToRam(uint8_t *chunkBuf) {
    if (!s_ramBuf) {
        Serial.println("recorder: no PSRAM buffer available, cannot record");
        return;
    }

    Serial.printf("recorder: no SD card, buffering to PSRAM (max %lus)\n", (unsigned long)RECORDER_RAM_MAX_SECONDS);
    size_t offset = sizeof(WavHeader);
    while (!s_stopRequested && offset + CHUNK_BYTES <= RECORDER_RAM_MAX_BYTES) {
        audio_playback_read(chunkBuf, CHUNK_BYTES);
        memcpy(s_ramBuf + offset, chunkBuf, CHUNK_BYTES);
        offset += CHUNK_BYTES;
    }
    if (offset + CHUNK_BYTES > RECORDER_RAM_MAX_BYTES) {
        Serial.println("recorder: hit PSRAM recording cap, stopping automatically");
    }

    if (s_cancelRequested) {
        // Cancelled -- leave s_ramHasRecording unset so the buffered audio
        // is never offered for sync; the bytes just die in the buffer and
        // get overwritten by the next recording.
        s_lastWasSd = false;
        s_lastFile = "";
        Serial.println("recorder: recording cancelled, PSRAM audio discarded");
        return;
    }

    uint32_t totalDataBytes = offset - sizeof(WavHeader);

    if (totalDataBytes < MIN_RECORDING_DATA_BYTES) {
        // Same treatment as an explicit cancel -- see MIN_RECORDING_DATA_BYTES.
        s_lastWasSd = false;
        s_lastFile = "";
        Serial.printf("recorder: discarded PSRAM recording (%lu bytes, under 1s -- likely an accidental press)\n",
                      (unsigned long)totalDataBytes);
        return;
    }

    WavHeader header;
    header.dataSize = totalDataBytes;
    header.chunkSize = 36 + totalDataBytes;
    memcpy(s_ramBuf, &header, sizeof(header));

    s_ramUsedBytes = offset;
    s_ramHasRecording = true;
    s_lastWasSd = false;
    s_lastFile = "";
    s_lastWasCommand = s_currentIsCommand;
    Serial.printf("recorder: saved %lu bytes audio to PSRAM\n", (unsigned long)totalDataBytes);
}

static void recordTask(void *arg) {
    // Codec channels are closed whenever nothing is using them (battery,
    // see audio_bsp_power_down) -- open them for this recording's whole
    // lifetime, including the start/stop clicks either side of it.
    audio_bsp_power_up();
    playClick();

    uint8_t *chunkBuf = (uint8_t *)heap_caps_malloc(CHUNK_BYTES, MALLOC_CAP_SPIRAM);

    if (sdcard_is_mounted()) {
        s_lastWasSd = true; // optimistic; recordToSd flips it back if fopen fails
        recordToSd(chunkBuf);
        if (!s_lastWasSd) {
            // fopen failed after all — still honor the recording by falling
            // back to RAM rather than losing the audio.
            recordToRam(chunkBuf);
        }
    } else {
        recordToRam(chunkBuf);
    }

    // Played here (same task, after the read loop has fully stopped) rather
    // than from the button handler, since the codec's read/write channels
    // aren't safe to drive from two tasks concurrently. A cancel gets its
    // own descending tone so discarding is audibly distinct from saving.
    if (s_cancelRequested) {
        playCancelTone();
    } else {
        playClick();
    }
    // On external power, leave the codec hot -- no battery reason to pay
    // the reopen latency on every recording.
    if (!power_mgr_on_external_power()) audio_bsp_power_down();

    heap_caps_free(chunkBuf);
    s_recording = false;
    s_recTask = nullptr;
    vTaskDelete(NULL);
}

void recorder_init() {
    s_recording = false;
    s_stopRequested = false;
    s_ramBuf = (uint8_t *)heap_caps_malloc(RECORDER_RAM_MAX_BYTES, MALLOC_CAP_SPIRAM);
    if (!s_ramBuf) {
        Serial.println("recorder: WARNING could not allocate PSRAM fallback buffer");
    }
}

void recorder_start(bool isCommand) {
    if (s_recording) return;
    s_stopRequested = false;
    s_cancelRequested = false;
    s_currentIsCommand = isCommand;
    s_recording = true;
    xTaskCreatePinnedToCore(recordTask, "recorder", 4 * 1024, NULL, 4, &s_recTask, 1);
}

bool recorder_last_was_command() {
    return s_lastWasCommand;
}

void recorder_stop() {
    s_stopRequested = true;
}

void recorder_cancel() {
    s_cancelRequested = true;
    s_stopRequested = true; // the record loops only watch s_stopRequested; cancel rides on top
}

bool recorder_was_cancelled() {
    return s_cancelRequested && !s_recording;
}

bool recorder_is_recording() {
    return s_recording;
}

bool recorder_last_was_sd() {
    return s_lastWasSd;
}

String recorder_last_file() {
    return s_lastFile;
}

const uint8_t *recorder_ram_wav_data(size_t *outLen) {
    if (!s_ramHasRecording) {
        if (outLen) *outLen = 0;
        return nullptr;
    }
    if (outLen) *outLen = s_ramUsedBytes;
    return s_ramBuf;
}

void recorder_clear_ram() {
    s_ramHasRecording = false;
    s_ramUsedBytes = 0;
    Serial.println("recorder: RAM recording cleared (pipeline confirmed sync)");
}

// Notification "click": a short damped tick, deliberately NOT the 90ms
// record-start/stop sine beep -- an exponentially decaying envelope reads
// as a mechanical click rather than a tone. Skipped entirely while
// recording: the codec's record/playback channels share one I2S
// peripheral and can't be driven from two tasks at once (same constraint
// playClick() documents -- but unlike playClick(), this is called from the
// BLE callback context, not the record task, so it must yield instead).
void recorder_notify_click() {
    if (s_recording) return;
    const int ms = 18;
    const float hz = 1600.0f;
    int samples = (SAMPLE_RATE * ms) / 1000;
    int16_t *buf = (int16_t *)heap_caps_malloc(samples * CHANNELS * sizeof(int16_t), MALLOC_CAP_SPIRAM);
    if (!buf) return;
    for (int i = 0; i < samples; i++) {
        float env = expf(-(float)i / (samples * 0.22f));
        int16_t s = (int16_t)(14000.0f * env * sinf(2.0f * (float)M_PI * hz * i / SAMPLE_RATE));
        buf[i * 2] = s;
        buf[i * 2 + 1] = s;
    }
    // Codec is normally powered down while idle (battery) -- wake it just
    // for this click. Adds ~a few ms of open() latency, inaudible for a
    // notification chirp. If a recording started between the s_recording
    // check above and here, power_up is idempotent and the recordTask owns
    // the power-down, so worst case is a harmless double-open request.
    bool wasPowered = audio_bsp_is_powered();
    if (!wasPowered) audio_bsp_power_up();
    audio_playback_write(buf, samples * CHANNELS * sizeof(int16_t));
    if (!wasPowered && !s_recording) audio_bsp_power_down();
    heap_caps_free(buf);
}

// WAV header layout must match the one this file writes (see WavHeader
// above) -- the Mac side (jarvis.py's send_audio_reply) builds the same
// 44-byte RIFF/WAVE/fmt/data layout at 16kHz/stereo/16-bit before upload,
// so this just skips the header and streams the PCM straight through.
void recorder_play_wav(const uint8_t *data, size_t len) {
    // Record and playback share one I2S peripheral -- never run both from
    // two tasks at once (same constraint as playTones()/recorder_notify_click()).
    if (s_recording) {
        Serial.println("recorder: dropping Jarvis audio reply, a recording is in progress");
        return;
    }
    if (len <= sizeof(WavHeader)) return;

    bool wasPowered = audio_bsp_is_powered();
    if (!wasPowered) audio_bsp_power_up();

    const uint8_t *pcm = data + sizeof(WavHeader);
    size_t pcmLen = len - sizeof(WavHeader);
    size_t offset = 0;
    while (offset < pcmLen) {
        if (s_recording) {
            // A button press started a recording mid-playback -- bail
            // immediately so the new recording gets the I2S peripheral.
            Serial.println("recorder: Jarvis audio reply interrupted by a new recording");
            break;
        }
        size_t chunk = (pcmLen - offset < CHUNK_BYTES) ? (pcmLen - offset) : CHUNK_BYTES;
        audio_playback_write((void *)(pcm + offset), chunk);
        offset += chunk;
    }

    if (!wasPowered && !s_recording) audio_bsp_power_down();
}
