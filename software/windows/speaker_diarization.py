"""Who spoke when -- local speaker diarization for live recordings (sherpa-onnx: pyannote
segmentation-3.0 + a WeSpeaker ResNet34 voice embedding, CPU only, ~30 MB of models).

Used by transcript_refiner after a recording: each speaker track is diarized separately and every
turn is then transcribed on its own (see transcript_refiner for how turns become Speaker 1, 2, ...).

Models: <models_dir>/sherpa-onnx-pyannote-segmentation-3-0/model.onnx and
<models_dir>/wespeaker_en_voxceleb_resnet34_LM.onnx (download_models()).
"""
import logging
import os
import threading
import urllib.request

import numpy as np

log = logging.getLogger("speaker_diarization")

RATE = 16000
SEGMENTATION_DIR = "sherpa-onnx-pyannote-segmentation-3-0"
EMBEDDING_FILE = "wespeaker_en_voxceleb_resnet34_LM.onnx"
_RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
CLUSTER_THRESHOLD = 0.4        # higher = fewer speakers; 0.35-0.45 tested best on mixed-voice meetings
MIC_GUEST_MIN_S = 8.0          # another voice on the mic needs this much speech to be its own speaker

_sd = None
_sd_lock = threading.Lock()


def _models_dir() -> str:
    import live_transcriber
    return live_transcriber.models_dir()


def _paths():
    d = _models_dir()
    return os.path.join(d, SEGMENTATION_DIR, "model.int8.onnx"), os.path.join(d, EMBEDDING_FILE)


def models_available() -> bool:
    try:
        import sherpa_onnx  # noqa: F401
    except ImportError:
        return False
    return all(os.path.exists(p) for p in _paths())


def download_models(progress=print):
    import tarfile
    d = _models_dir()
    os.makedirs(d, exist_ok=True)
    seg, emb = _paths()
    if not os.path.exists(seg):
        tmp = os.path.join(d, "segmentation.tar.bz2")
        progress("downloading speaker segmentation model (7 MB)...")
        urllib.request.urlretrieve(f"{_RELEASES}/speaker-segmentation-models/{SEGMENTATION_DIR}.tar.bz2", tmp)
        with tarfile.open(tmp) as t:
            t.extractall(d, filter="data")
        os.remove(tmp)
    if not os.path.exists(emb):
        progress("downloading speaker embedding model (25 MB)...")
        urllib.request.urlretrieve(f"{_RELEASES}/speaker-recongition-models/{EMBEDDING_FILE}", emb + ".part")
        os.replace(emb + ".part", emb)


def _diarizer():
    global _sd
    import sherpa_onnx
    with _sd_lock:
        if _sd is None:
            seg, emb = _paths()
            threads = max(2, (os.cpu_count() or 4) // 2)
            config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
                segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                    pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=seg),
                    num_threads=threads),
                embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=emb, num_threads=threads),
                clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=CLUSTER_THRESHOLD),
                min_duration_on=0.3, min_duration_off=0.5)
            if not config.validate():
                raise RuntimeError("speaker diarization models are incomplete")
            _sd = sherpa_onnx.OfflineSpeakerDiarization(config)
        return _sd


def diarize(audio: np.ndarray) -> list:
    """16 kHz float32 mono -> [(start_s, end_s, cluster)], sorted by start."""
    if audio.size < RATE:
        return []
    sd = _diarizer()
    with _sd_lock:                      # one diarization at a time (shared model)
        result = sd.process(np.ascontiguousarray(audio, dtype=np.float32)).sort_by_start_time()
    return [(r.start, r.end, int(r.speaker)) for r in result]


def drop_echo(segments: list, owner_track: str = "you", call_track: str = "others") -> int:
    """Removes mic lines that are just the call coming out of the speakers into the microphone: same
    time as a call-track line and mostly the same words. Returns how many were dropped."""
    import difflib
    call = [s for s in segments if s["speaker_id"] == call_track]
    keep, dropped = [], 0
    for s in segments:
        if s["speaker_id"] == owner_track:
            for c in call:
                overlap = min(s["end"], c["end"]) - max(s["start"], c["start"])
                if overlap <= 0.5 * max(0.1, s["end"] - s["start"]):
                    continue
                mine = [w.strip(".,!?;:").lower() for w in s["text"].split()]
                theirs = {w.strip(".,!?;:").lower() for w in c["text"].split()}
                contained = mine and sum(w in theirs for w in mine) / len(mine) >= 0.8   # a piece of the line
                if contained or difflib.SequenceMatcher(None, s["text"].lower(), c["text"].lower()).ratio() > 0.6:
                    dropped += 1
                    break
            else:
                keep.append(s)
        else:
            keep.append(s)
    segments[:] = keep
    return dropped


if __name__ == "__main__":
    download_models()
    print("speaker diarization models ready in", _models_dir())
