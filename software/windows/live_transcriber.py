"""Live (while-recording) transcription for live_recorder.py, adapted from Live Subtitles
(github.com/ted91/teams-subtitles: vad_window.py + stt_worker.py), minus translation.

  capture callback -> feed() -> VAD thread: Silero VAD cuts each source's audio into utterances and
  re-emits the growing utterance every 250 ms -> STT thread: Parakeet-TDT 0.6B v3 (sherpa-onnx, int8,
  CPU) re-decodes it; a word is committed once two consecutive decodes agree (LocalAgreement-2).

Each source is its own speaker: the microphone is "you", the computer's audio is "others". Finished
utterances become Clicky transcript segments ({speaker_id, text, start, end}, seconds from the start of
the recording), so the recording needs no batch transcription afterwards.

Runs in threads, not processes: sherpa-onnx releases the GIL while decoding.
Models (~650 MB, downloaded once, see download_models()): <models_dir>/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8/
and <models_dir>/silero_vad.onnx, where models_dir is settings "live_models_dir" or <APP_DATA_DIR>/models.
"""
import logging
import os
import queue
import re
import threading
import time

import numpy as np

log = logging.getLogger("live_transcriber")

RATE = 16000
PARAKEET_NAME = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
_SHERPA_RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"

# Live Subtitles' tuned defaults (its config.yaml)
STT_THREADS = 4
AGREEMENT_N = 2
TRIM_AFTER_S = 5.0
WINDOW_INTERVAL_S = 0.25
PAUSE_S = 0.4
MAX_UTTERANCE_S = 15.0
VAD_THRESHOLD = 0.5

_CHUNK = 512                       # Silero window at 16 kHz (32 ms)
_CHUNK_S = _CHUNK / RATE
_PREROLL_CHUNKS = 10               # ~320 ms kept from before speech starts
_START_CHUNKS = 2                  # 64 ms of speech opens an utterance
_KEEP_TAIL_S = 0.15                # trailing silence kept in a final window
_PUNCT = re.compile(r"[^\w]+", re.UNICODE)


def models_dir() -> str:
    import paths
    import settings
    return settings.get_all().get("live_models_dir") or os.path.join(paths.APP_DATA_DIR, "models")


def _parakeet_dir() -> str:
    return os.path.join(models_dir(), PARAKEET_NAME)


def _silero_path() -> str:
    return os.path.join(models_dir(), "silero_vad.onnx")


def models_available() -> bool:
    try:
        import sherpa_onnx  # noqa: F401
    except ImportError:
        return False
    return os.path.exists(os.path.join(_parakeet_dir(), "tokens.txt")) and os.path.exists(_silero_path())


def is_enabled() -> bool:
    import settings
    return settings.get_all().get("live_transcription", True) and models_available()


def download_models(progress=print):
    """One-time download (~650 MB) of Parakeet + Silero VAD into models_dir()."""
    import tarfile
    import urllib.request
    os.makedirs(models_dir(), exist_ok=True)
    if not os.path.exists(_silero_path()):
        progress("downloading silero_vad.onnx")
        urllib.request.urlretrieve(f"{_SHERPA_RELEASES}/silero_vad.onnx", _silero_path())
    if not os.path.exists(os.path.join(_parakeet_dir(), "tokens.txt")):
        archive = os.path.join(models_dir(), PARAKEET_NAME + ".tar.bz2")
        progress(f"downloading {PARAKEET_NAME} (~640 MB)")
        urllib.request.urlretrieve(f"{_SHERPA_RELEASES}/{PARAKEET_NAME}.tar.bz2", archive)
        progress("extracting")
        with tarfile.open(archive) as tar:
            tar.extractall(models_dir())
        os.remove(archive)


# --- shared model (loaded once, ~14 s cold) ------------------------------------------------------

_recognizer = None
_recognizer_lock = threading.Lock()


def _get_recognizer():
    global _recognizer
    with _recognizer_lock:
        if _recognizer is None:
            import sherpa_onnx
            d = _parakeet_dir()
            t = time.time()
            _recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
                encoder=os.path.join(d, "encoder.int8.onnx"), decoder=os.path.join(d, "decoder.int8.onnx"),
                joiner=os.path.join(d, "joiner.int8.onnx"), tokens=os.path.join(d, "tokens.txt"),
                num_threads=STT_THREADS, model_type="nemo_transducer")
            log.info("Parakeet loaded in %.1fs", time.time() - t)
        return _recognizer


def preload_in_background():
    """Load Parakeet ahead of the first recording so live text starts immediately."""
    if is_enabled():
        threading.Thread(target=_get_recognizer, daemon=True, name="parakeet-preload").start()


def _transcribe(audio: np.ndarray):
    """-> [(word, start_seconds)]. Parakeet v3 detects the language itself."""
    rec = _get_recognizer()
    s = rec.create_stream()
    s.accept_waveform(RATE, audio)
    rec.decode_stream(s)
    r = s.result
    words = []  # tokens are sub-word pieces; a leading space starts a new word
    for tok, t in zip(r.tokens, r.timestamps):
        if tok.startswith(" ") or not words:
            words.append([tok.strip(), t])
        else:
            words[-1][0] += tok
    return [(w, t) for w, t in words if _norm(w)]


def _norm(w):
    return _PUNCT.sub("", w.lower())


def _common_prefix_len(a, b):
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _create_vad():
    import sherpa_onnx
    c = sherpa_onnx.SileroVadModelConfig()
    c.model = _silero_path()
    c.threshold = VAD_THRESHOLD
    c.window_size = _CHUNK
    vc = sherpa_onnx.VadModelConfig()
    vc.silero_vad = c
    vc.sample_rate = RATE
    vc.num_threads = 1
    return sherpa_onnx.VadModel.create(vc)


# --- stage 1: VAD + utterance windows (from Live Subtitles' vad_window.py) -----------------------

class _WindowMaker:
    def __init__(self, source, emit):
        self.source, self.emit = source, emit
        self.vad = _create_vad()
        self.pending = np.zeros(0, dtype=np.float32)
        self.preroll, self.utt = [], []
        self.utt_len = self.speech_run = self.utt_id = 0
        self.in_utt = False
        self.silence_s = self.t_last_emit = self.t_last_chunk = 0.0

    def feed(self, samples, t_end):
        buf = np.concatenate([self.pending, samples]) if len(self.pending) else samples
        n = len(buf) // _CHUNK
        for i in range(n):
            chunk = buf[i * _CHUNK:(i + 1) * _CHUNK]
            self._chunk(chunk, self.vad.is_speech(chunk), t_end - (len(buf) - (i + 1) * _CHUNK) / RATE)
        self.pending = buf[n * _CHUNK:].copy()

    def idle(self, t_now):
        """Close an utterance if audio stopped arriving (loopback delivers nothing while the PC is silent)."""
        if self.in_utt and t_now - self.t_last_chunk >= PAUSE_S:
            self._finish(self.t_last_chunk)

    def flush(self):
        if self.in_utt:
            self._finish(self.t_last_chunk)

    def _chunk(self, chunk, is_speech, t):
        self.t_last_chunk = t
        if not self.in_utt:
            self.preroll = (self.preroll + [chunk])[-_PREROLL_CHUNKS:]
            self.speech_run = self.speech_run + 1 if is_speech else 0
            if self.speech_run >= _START_CHUNKS:
                self._start(t)
            return
        self.utt.append(chunk)
        self.utt_len += _CHUNK
        self.silence_s = 0.0 if is_speech else self.silence_s + _CHUNK_S
        if self.silence_s >= PAUSE_S:
            self._finish(t)
        elif self.utt_len / RATE >= MAX_UTTERANCE_S:
            self._finish(t)
            if is_speech:                       # forced split: speech continues in a new utterance
                self.preroll, self.speech_run = [chunk], _START_CHUNKS
                self._start(t)
        elif t - self.t_last_emit >= WINDOW_INTERVAL_S:
            self._emit(False, t)

    def _start(self, t):
        self.in_utt = True
        self.utt_id += 1
        self.utt, self.preroll = list(self.preroll), []
        self.utt_len = len(self.utt) * _CHUNK
        self.silence_s = 0.0
        self.t_last_emit = t
        self._emit(False, t)

    def _finish(self, t):
        trim = int(max(0.0, self.silence_s - _KEEP_TAIL_S) * RATE)
        self._emit(True, t, trim)
        self.in_utt = False
        self.utt, self.preroll = [], []
        self.utt_len = self.speech_run = 0
        self.silence_s = 0.0
        self.vad.reset()

    def _emit(self, final, t, trim_tail=0):
        audio = np.concatenate(self.utt)
        if trim_tail:
            audio, t = audio[:-trim_tail], t - trim_tail / RATE
        self.t_last_emit = t
        self.emit({"source": self.source, "utt_id": self.utt_id, "audio": audio, "final": final, "t_end": t})


# --- stage 2: LocalAgreement + trimming (from Live Subtitles' stt_worker.py) ---------------------

class _UtteranceState:
    def __init__(self):
        self.reset(None)

    def reset(self, utt_id):
        self.utt_id = utt_id
        self.offset_s = 0.0
        self.committed, self.history = [], []
        self.region_n = 0

    def _align(self, norm):
        cn = [_norm(w) for w in self.committed[-len(norm):]] if norm else []
        for o in range(min(len(cn), len(norm)), 0, -1):
            if cn[-o:] == norm[:o]:
                return o, True
        return min(self.region_n, len(norm)), False

    def update(self, audio, final):
        sub = audio[int(self.offset_s * RATE):]
        sub_s = len(sub) / RATE
        words = _transcribe(sub) if sub_s >= 0.2 else []
        disp = [w for w, _ in words]
        norm = [_norm(w) for w in disp]
        k, matched = self._align(norm)
        if k and matched:
            self.committed[-k:] = disp[:k]
        if final:
            n_commit = len(words)
        else:
            self.history = (self.history + [norm])[-AGREEMENT_N:]
            if len(self.history) < AGREEMENT_N:
                n_commit = k
            else:
                agree = len(norm)
                for h in self.history[:-1]:
                    agree = min(agree, _common_prefix_len(h, norm))
                n_commit = max(k, agree)
            n_commit = min(n_commit, len(words))
        if n_commit > k:
            new = disp[k:n_commit]
            self.committed.extend(new)
            self.region_n = max(self.region_n, k) + len(new)
        draft = disp[n_commit:] if not final else []
        if not final and sub_s > TRIM_AFTER_S and 0 < n_commit < len(words):
            cut = words[n_commit][1] - 0.05
            if cut > 1.0:
                self.offset_s += cut
                self.region_n = 0
                self.history = []
        return list(self.committed), draft


# --- one live session ---------------------------------------------------------------------------

class LiveTranscriber:
    """One per recording. feed() is called from the capture callbacks; everything else from Clicky."""

    def __init__(self, t0: float, sources):
        self.t0 = t0
        self._audio_q = queue.Queue(maxsize=2000)
        self._window_q = queue.Queue(maxsize=16)
        self._stop = threading.Event()
        self._vad_done = threading.Event()
        self._lock = threading.Lock()
        self.segments = []                      # finished utterances, in time order (+ "translation")
        self.drafts = {s: "" for s in sources}  # current utterance per source (committed + draft words)
        self.draft_translations = {s: "" for s in sources}
        self.dropped_audio = 0
        self._post_q = queue.Queue()
        self._makers = {s: _WindowMaker(s, self._emit) for s in sources}
        self._vad_thread = threading.Thread(target=self._vad_loop, daemon=True, name="live-vad")
        self._stt_thread = threading.Thread(target=self._stt_loop, daemon=True, name="live-stt")
        self._post_thread = threading.Thread(target=self._post_loop, daemon=True, name="live-translate")
        self._vad_thread.start()
        self._stt_thread.start()
        self._post_thread.start()

    def feed(self, source, t_end, samples):
        try:
            self._audio_q.put_nowait((source, t_end, samples))
        except queue.Full:
            self.dropped_audio += 1

    def snapshot(self) -> dict:
        with self._lock:
            return {"segments": [dict(s) for s in self.segments], "drafts": dict(self.drafts),
                    "draft_translations": dict(self.draft_translations)}

    def finish(self, timeout: float = 30.0) -> list:
        """Ends the session: flushes open utterances, waits for the decoder (and translation) to catch
        up, returns the segments. Anything still undecoded after `timeout` is left out (the WAV has it)."""
        self._stop.set()
        self._vad_thread.join(timeout=5)
        self._stt_thread.join(timeout=timeout)
        self._post_thread.join(timeout=15)
        with self._lock:
            return sorted((dict(s) for s in self.segments), key=lambda s: s["start"])

    def _emit(self, msg):
        try:
            self._window_q.put_nowait(msg)
        except queue.Full:
            if msg["final"]:                    # finals matter: make room by dropping the oldest window
                try:
                    self._window_q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._window_q.put_nowait(msg)
                except queue.Full:
                    pass

    def _vad_loop(self):
        last_idle = 0.0
        while True:
            try:
                source, t_end, samples = self._audio_q.get(timeout=0.05)
            except queue.Empty:
                if self._stop.is_set():
                    break
                source = None
            if source is not None:
                self._makers[source].feed(samples, t_end)
            # On a timer, not only when the queue is empty: the computer-audio stream sends nothing while
            # the PC is silent, and a steady stream from another source (e.g. a muted mic's zeros) would
            # otherwise keep that utterance open forever -- run-on lines and repeated words.
            t = time.monotonic()
            if t - last_idle >= 0.05:
                last_idle = t
                for m in self._makers.values():
                    m.idle(t)
        for m in self._makers.values():
            m.flush()
        self._vad_done.set()

    def _stt_loop(self):
        states = {}
        while True:
            try:
                batch = [self._window_q.get(timeout=0.1)]
            except queue.Empty:
                if self._vad_done.is_set():
                    break
                continue
            while True:
                try:
                    batch.append(self._window_q.get_nowait())
                except queue.Empty:
                    break
            last = {(m["source"], m["utt_id"]): i for i, m in enumerate(batch)}
            for i, m in enumerate(batch):
                if last[(m["source"], m["utt_id"])] != i and not m["final"]:
                    continue                    # a newer window of this utterance is already queued
                st = states.setdefault(m["source"], _UtteranceState())
                if st.utt_id != m["utt_id"]:
                    st.reset(m["utt_id"])
                try:
                    committed, draft = st.update(m["audio"], m["final"])
                except Exception as e:
                    log.warning("live transcription error (continuing): %s", e)
                    continue
                with self._lock:
                    seg = None
                    if m["final"]:
                        self.drafts[m["source"]] = ""
                        self.draft_translations[m["source"]] = ""
                        text = " ".join(committed).strip()
                        if text:
                            end = m["t_end"] - self.t0
                            seg = {"speaker_id": m["source"], "text": text,
                                   "start": round(max(0.0, end - len(m["audio"]) / RATE), 2), "end": round(end, 2)}
                            self.segments.append(seg)
                    else:
                        self.drafts[m["source"]] = " ".join(committed + draft)
                self._post_q.put((m["source"], m["utt_id"], m["final"], committed, draft, seg))
        self._post_q.put(None)

    def _post_loop(self):
        """Translation + display stage, after STT so a slow translation never delays transcription.
        Superseded partial updates are skipped; finals always go through (their translation is saved)."""
        import live_translator
        langs = {}                                   # (source, utt_id) -> language, fixed per utterance
        done = False
        while not done:
            batch = [self._post_q.get()]
            while True:
                try:
                    batch.append(self._post_q.get_nowait())
                except queue.Empty:
                    break
            if None in batch:
                done = True
                batch = [b for b in batch if b is not None]
            last = {(b[0], b[1]): i for i, b in enumerate(batch)}
            for i, (source, utt_id, final, committed, draft, seg) in enumerate(batch):
                if last[(source, utt_id)] != i and not final:
                    continue
                lang = langs.setdefault((source, utt_id), self._lang_for(source, live_translator))
                tr = None
                if lang and (committed or draft):
                    try:
                        tr = live_translator.get_translator().update(committed, draft, final, lang)
                    except Exception as e:
                        log.warning("live translation error (continuing): %s", e)
                with self._lock:
                    if tr is not None:
                        english = " ".join(tr["sentences"] + [p for p in (tr["stable"], tr["dim"]) if p])
                        if final and seg is not None:
                            seg["translation"] = english
                            seg["translation_lang"] = lang
                        elif not final:
                            self.draft_translations[source] = english
                    if final:
                        langs.pop((source, utt_id), None)
                update = {"source": source, "utt_id": utt_id, "final": final, "stable": " ".join(committed),
                          "dim": " ".join(draft), "translation": tr, "lang": lang}
                for fn in list(_update_listeners):
                    try:
                        fn(update)
                    except Exception:
                        log.exception("live update listener failed")

    def _lang_for(self, source, live_translator):
        import settings
        lang = live_translator.current_language()
        if lang and source == "you" and not settings.get_all().get("subtitle_translate_own_mic", True):
            return ""
        return lang


_update_listeners = []


def add_update_listener(fn):
    """fn(update) for every live transcript update (subtitle bar). update: source, utt_id, final,
    stable (committed words), dim (draft words), translation ({sentences, stable, dim} or None), lang."""
    _update_listeners.append(fn)


if __name__ == "__main__":   # python live_transcriber.py  -> one-time model download into models_dir()
    download_models()
    print("Live transcription models installed in", models_dir())
