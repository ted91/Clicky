#include "ima_adpcm.h"

// Standard IMA ADPCM reference tables (public domain -- the same 89-entry
// step-size table and 16-entry index-adjustment table used by essentially
// every open-source ADPCM implementation, e.g. libsndfile/ffmpeg's
// adpcm_ima_* variants). See ima_adpcm.h for why the surrounding
// container is custom even though this core algorithm isn't.
static const int16_t STEP_TABLE[89] = {
    7,     8,     9,     10,    11,    12,    13,    14,    16,    17,
    19,    21,    23,    25,    28,    31,    34,    37,    41,    45,
    50,    55,    60,    66,    73,    80,    88,    97,    107,   118,
    130,   143,   157,   173,   190,   209,   230,   253,   279,   307,
    337,   371,   408,   449,   494,   544,   598,   658,   724,   796,
    876,   963,   1060,  1166,  1282,  1411,  1552,  1707,  1878,  2066,
    2272,  2499,  2749,  3024,  3327,  3660,  4026,  4428,  4871,  5358,
    5894,  6484,  7132,  7845,  8630,  9493,  10442, 11487, 12635, 13899,
    15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767,
};

static const int8_t INDEX_TABLE[16] = {
    -1, -1, -1, -1, 2, 4, 6, 8,
    -1, -1, -1, -1, 2, 4, 6, 8,
};

// Encodes one sample against one channel's running state, returning the
// 4-bit nibble -- and updates predictor/index exactly as the decoder's
// own reconstruction will, so encoder and decoder never drift apart.
static uint8_t encodeSample(AdpcmChannelState *st, int16_t sample) {
    int32_t step = STEP_TABLE[st->index];
    int32_t diff = (int32_t)sample - st->predictor;
    uint8_t sign = 0;
    if (diff < 0) {
        sign = 8;
        diff = -diff;
    }

    uint8_t delta = 0;
    int32_t tempStep = step;
    int32_t diffq = step >> 3;
    if (diff >= tempStep) {
        delta = 4;
        diff -= tempStep;
        diffq += step;
    }
    tempStep >>= 1;
    if (diff >= tempStep) {
        delta |= 2;
        diff -= tempStep;
        diffq += step >> 1;
    }
    tempStep >>= 1;
    if (diff >= tempStep) {
        delta |= 1;
        diffq += step >> 2;
    }

    int32_t newPredictor = sign ? (st->predictor - diffq) : (st->predictor + diffq);
    if (newPredictor > 32767) newPredictor = 32767;
    if (newPredictor < -32768) newPredictor = -32768;
    st->predictor = (int16_t)newPredictor;

    int32_t newIndex = st->index + INDEX_TABLE[sign | delta];
    if (newIndex < 0) newIndex = 0;
    if (newIndex > 88) newIndex = 88;
    st->index = (int8_t)newIndex;

    return sign | delta;
}

size_t adpcm_encode(AdpcmEncoderState *state, const int16_t *samples, size_t numSamples, uint16_t channels, uint8_t *out) {
    size_t outBytes = 0;
    uint8_t pendingNibble = 0;
    bool havePending = false;

    for (size_t i = 0; i < numSamples; i++) {
        uint16_t ch = (uint16_t)(i % channels);
        uint8_t nibble = encodeSample(&state->ch[ch], samples[i]);
        if (!havePending) {
            pendingNibble = nibble;
            havePending = true;
        } else {
            out[outBytes++] = pendingNibble | (nibble << 4);
            havePending = false;
        }
    }
    // numSamples is always a multiple of channels (2 for stereo), and
    // channels itself is even in every case this device uses (mono=1
    // would leave a dangling nibble every call, but this device always
    // records stereo -- see recorder.cpp's CHANNELS), so havePending
    // should never be true here; no partial-byte handling needed.
    return outBytes;
}
