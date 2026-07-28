#include "recorder.h"
#include "audio_bsp.h"
#include "src/sdcard/sdcard_bsp.h"
#include <stdio.h>
#include <math.h>
#include <string.h>
#include <dirent.h>
#include "esp_vfs_fat.h"
#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

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

// PSRAM fallback — when no SD card is present, record in mono to double
// capacity: 120s of 16kHz/mono/16-bit = ~3.75MB (same as 60s stereo was),
// leaving comfortable headroom alongside BLE/WiFi stacks and display buffers.
// SD-card recordings always stay stereo (no conversion needed there).
static const uint32_t RECORDER_RAM_MAX_SECONDS = 120;
static const uint16_t RAM_CHANNELS = 1;  // mono for RAM path only
static const size_t RECORDER_RAM_MAX_BYTES =
    (size_t)RECORDER_RAM_MAX_SECONDS * SAMPLE_RATE * RAM_CHANNELS * (BITS_PER_SAMPLE / 8);

static TaskHandle_t s_recTask = nullptr;
static volatile bool s_stopRequested = false;
static volatile bool s_recording = false;
static bool s_lastWasSd = false;
static String s_lastFile = "";
static int s_recIndex = 0;

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

static uint64_t sdFreeBytes() {
    // sys/statvfs.h isn't available in the Arduino-ESP32 toolchain -- use
    // ESP-IDF's own vfs_fat API instead (already proven to compile here;
    // sdcard_bsp.cpp includes the same header to mount the card in the
    // first place).
    uint64_t total = 0, free_ = 0;
    if (esp_vfs_fat_info(SDCARD_DIR, &total, &free_) != ESP_OK) {
        return UINT64_MAX; // can't tell -- don't block recording on a stat failure
    }
    return free_;
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

static void recordToSd(uint8_t *chunkBuf) {
    evictOldestSdFileIfNeeded();

    char path[64];
    snprintf(path, sizeof(path), "%s/rec_%03d.wav", SDCARD_DIR, s_recIndex++);

    FILE *f = fopen(path, "wb");
    if (!f) {
        Serial.printf("recorder: failed to open %s, falling back to RAM\n", path);
        return; // caller (recordTask) will notice s_lastWasSd stays false
    }

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

    Serial.printf("recorder: saved %s (%lu bytes audio)\n", path, (unsigned long)totalDataBytes);
    s_lastFile = String(path);
    s_lastWasSd = true;
}

static void recordToRam(uint8_t *chunkBuf) {
    if (!s_ramBuf) {
        Serial.println("recorder: no PSRAM buffer available, cannot record");
        return;
    }

    // Each codec read gives CHUNK_BYTES of stereo PCM.  Average L+R into a
    // single mono sample to halve the storage cost — doubles max duration
    // (120s) with the same PSRAM footprint as the old 60s stereo buffer.
    const size_t stereoSamples = CHUNK_BYTES / (CHANNELS * sizeof(int16_t));
    const size_t monoBytes = stereoSamples * sizeof(int16_t);  // half of CHUNK_BYTES

    Serial.printf("recorder: no SD card, buffering mono to PSRAM (max %lus)\n",
                  (unsigned long)RECORDER_RAM_MAX_SECONDS);
    size_t offset = sizeof(WavHeader);
    while (!s_stopRequested && offset + monoBytes <= sizeof(WavHeader) + RECORDER_RAM_MAX_BYTES) {
        audio_playback_read(chunkBuf, CHUNK_BYTES);
        // Stereo→mono: average left and right 16-bit samples
        const int16_t *src = (const int16_t *)chunkBuf;
        int16_t *dst = (int16_t *)(s_ramBuf + offset);
        for (size_t i = 0; i < stereoSamples; i++) {
            dst[i] = (int16_t)(((int32_t)src[i * 2] + src[i * 2 + 1]) >> 1);
        }
        offset += monoBytes;
    }
    if (!s_stopRequested) {
        Serial.println("recorder: hit PSRAM recording cap, stopping automatically");
    }

    uint32_t totalDataBytes = (uint32_t)(offset - sizeof(WavHeader));
    WavHeader header;
    // Override stereo defaults with mono values for this WAV
    header.numChannels  = RAM_CHANNELS;
    header.byteRate     = SAMPLE_RATE * RAM_CHANNELS * (BITS_PER_SAMPLE / 8);
    header.blockAlign   = RAM_CHANNELS * (BITS_PER_SAMPLE / 8);
    header.dataSize     = totalDataBytes;
    header.chunkSize    = 36 + totalDataBytes;
    memcpy(s_ramBuf, &header, sizeof(header));

    s_ramUsedBytes = offset;
    s_ramHasRecording = true;
    s_lastWasSd = false;
    s_lastFile = "";
    Serial.printf("recorder: saved %lu bytes mono audio to PSRAM (%lus)\n",
                  (unsigned long)totalDataBytes,
                  (unsigned long)(totalDataBytes / (SAMPLE_RATE * RAM_CHANNELS * (BITS_PER_SAMPLE / 8))));
}

static void recordTask(void *arg) {
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
    // aren't safe to drive from two tasks concurrently.
    playClick();

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

void recorder_start() {
    if (s_recording) return;
    s_stopRequested = false;
    s_recording = true;
    xTaskCreatePinnedToCore(recordTask, "recorder", 4 * 1024, NULL, 4, &s_recTask, 1);
}

void recorder_stop() {
    s_stopRequested = true;
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
