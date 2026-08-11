#ifndef IMA_ADPCM_H
#define IMA_ADPCM_H

#include <stdint.h>
#include <stddef.h>

// Continuous-stream (non-block-reset) IMA/DVI4 ADPCM encoder -- roughly
// 4:1 smaller than 16-bit PCM (one nibble per sample instead of two
// bytes), which directly cuts SD card usage and WiFi transfer time/energy
// for every recording (see this session's transfer-speed investigation --
// the actual bottleneck there was pipelining, not payload size, but a
// 79MB recording is still 79MB to move regardless of how fast the pipe
// is). Chosen over a real codec (Opus) specifically for being cheap
// enough to run inline in the record loop: a handful of integer ops per
// sample, no floating point, negligible CPU next to what's already
// budgeted for I2S capture.
//
// Deliberately NOT the standard Microsoft block-based WAVE_FORMAT_DVI_ADPCM
// (registered tag 0x0011) -- that format resets its predictor/index at
// the start of every fixed-size block (needed for seekable playback,
// which this device never needs) and carries a per-block 4-byte header
// per channel. This is simpler: ONE predictor/index pair per channel for
// the entire recording, streamed incrementally as audio_playback_read()
// delivers 2048-byte PCM chunks -- exactly matches how recordToSd()/
// recordToRam() already work, no block-alignment bookkeeping. Written out
// under a private, non-standard format tag (see recorder.cpp's WavHeader
// comment) specifically so a generic WAV player never mistakes it for
// real IMA ADPCM and produces garbage audio -- only this file's encoder
// and the matching Python decoder (software/*/adpcm.py) understand it.
//
// Algorithm itself (step-size/index tables, quantization) is the
// standard IMA ADPCM reference algorithm (public domain, same one nearly
// every open-source ADPCM codec implements) -- only the container/
// framing choice above is custom.

struct AdpcmChannelState {
    int16_t predictor = 0;
    int8_t index = 0;
};

struct AdpcmEncoderState {
    AdpcmChannelState ch[2]; // one per audio channel -- this device only ever records mono or stereo
};

// Encodes `numSamples` interleaved int16 PCM samples (numSamples must be
// a multiple of `channels`) into 4-bit nibbles packed two per byte,
// written to `out` (caller-sized -- numSamples/2 bytes). Returns the
// number of bytes written. Call repeatedly across a whole recording with
// the SAME `state`, so the predictor stays continuous chunk-to-chunk --
// reset `state` to a fresh AdpcmEncoderState{} only at the start of each
// new recording, never mid-recording.
size_t adpcm_encode(AdpcmEncoderState *state, const int16_t *samples, size_t numSamples, uint16_t channels, uint8_t *out);

#endif
