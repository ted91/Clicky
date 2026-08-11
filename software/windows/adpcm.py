"""Decoder for the private continuous-stream IMA ADPCM format the firmware
now records in (see firmware/src/ima_adpcm.h/.cpp) -- NOT the standard
Microsoft block-based WAVE_FORMAT_DVI_ADPCM (registered tag 0x0011). The
device streams one predictor/index pair per channel across an entire
recording (no per-block reset), which is what this decoder mirrors.

Why the device compresses at all: cuts SD card usage and WiFi transfer
time/energy by ~4:1 versus 16-bit PCM, for free (integer-only encode, no
real CPU cost on the device). The tradeoff lands entirely here instead --
decode happens once, on the Mac, right after download, before the audio
is ever handed to a transcription provider (which all expect standard
PCM WAV) or saved to disk (so local playback/dashboard scrubbing, and
anything reading storage.json's wav_path, sees ordinary PCM same as
always -- decoding is NOT deferred to those call sites).
"""
import struct

ADPCM_FORMAT_TAG = 0x1001

# Same standard IMA ADPCM reference tables the firmware's encoder uses --
# see ima_adpcm.cpp's comment for provenance (public domain, shared by
# nearly every open-source ADPCM codec). Encoder and decoder MUST use
# identical tables/logic or the reconstructed predictor drifts.
_STEP_TABLE = [
    7,     8,     9,     10,    11,    12,    13,    14,    16,    17,
    19,    21,    23,    25,    28,    31,    34,    37,    41,    45,
    50,    55,    60,    66,    73,    80,    88,    97,    107,   118,
    130,   143,   157,   173,   190,   209,   230,   253,   279,   307,
    337,   371,   408,   449,   494,   544,   598,   658,   724,   796,
    876,   963,   1060,  1166,  1282,  1411,  1552,  1707,  1878,  2066,
    2272,  2499,  2749,  3024,  3327,  3660,  4026,  4428,  4871,  5358,
    5894,  6484,  7132,  7845,  8630,  9493,  10442, 11487, 12635, 13899,
    15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767,
]
_INDEX_TABLE = [-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8]


def _decode_nibble(predictor: int, index: int, nibble: int):
    step = _STEP_TABLE[index]
    sign = nibble & 8
    delta = nibble & 7
    diffq = step >> 3
    if delta & 4:
        diffq += step
    if delta & 2:
        diffq += step >> 1
    if delta & 1:
        diffq += step >> 2
    predictor = predictor - diffq if sign else predictor + diffq
    predictor = max(-32768, min(32767, predictor))
    index = max(0, min(88, index + _INDEX_TABLE[nibble]))
    return predictor, index


def is_adpcm_wav(wav_bytes: bytes) -> bool:
    if len(wav_bytes) < 20 or wav_bytes[0:4] != b"RIFF" or wav_bytes[8:12] != b"WAVE":
        return False
    audio_format = struct.unpack_from("<H", wav_bytes, 20)[0]
    return audio_format == ADPCM_FORMAT_TAG


def decode_adpcm_wav(wav_bytes: bytes) -> bytes:
    """Decodes a device-recorded ADPCM WAV into a standard 16-bit PCM WAV
    with an identical header shape, just audioFormat=1/bitsPerSample=16/
    real byteRate -- every downstream consumer (transcription providers,
    local playback, _wav_duration_seconds) sees ordinary PCM. Assumes the
    firmware's fixed 44-byte canonical header layout (RIFF/fmt /data, no
    extra chunks) -- true for every file this device writes."""
    num_channels = struct.unpack_from("<H", wav_bytes, 22)[0]
    sample_rate = struct.unpack_from("<I", wav_bytes, 24)[0]
    data_size = struct.unpack_from("<I", wav_bytes, 40)[0]
    data = wav_bytes[44:44 + data_size]

    predictor = [0] * num_channels
    index = [0] * num_channels
    out_samples = bytearray()
    ch = 0
    for byte in data:
        for nibble in (byte & 0x0F, (byte >> 4) & 0x0F):
            predictor[ch], index[ch] = _decode_nibble(predictor[ch], index[ch], nibble)
            out_samples += struct.pack("<h", predictor[ch])
            ch = (ch + 1) % num_channels

    byte_rate = sample_rate * num_channels * 2
    block_align = num_channels * 2
    pcm_data_size = len(out_samples)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + pcm_data_size, b"WAVE",
        b"fmt ", 16, 1, num_channels, sample_rate, byte_rate, block_align, 16,
        b"data", pcm_data_size,
    )
    return header + bytes(out_samples)


def maybe_decode(wav_bytes: bytes) -> bytes:
    """Convenience entry point for callers that just want "give me
    playable PCM regardless of what the device actually sent" -- returns
    wav_bytes unchanged if it isn't ADPCM-tagged (e.g. an older recording
    synced before this feature existed, or a non-WAV blob that some other
    check will reject anyway)."""
    try:
        if is_adpcm_wav(wav_bytes):
            return decode_adpcm_wav(wav_bytes)
    except (struct.error, IndexError):
        pass
    return wav_bytes
