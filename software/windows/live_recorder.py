"""Live conversation recording on Windows (no device needed).

Records the default microphone and, optionally, what the computer is playing (WASAPI loopback --
the other side of a Teams/Zoom/Meet call). The audio is saved as one 16 kHz mono WAV AND, when the
models are installed, transcribed live while recording (live_transcriber.py, Parakeet). The finished
recording -- audio plus the live transcript -- goes into the normal pipeline via storage.add_pending(),
so it appears on the dashboard like a device recording, without a second transcription pass.

Started/stopped by `controller` (one per process): the tray menu, a dashboard button, and a quick
double tap of Caps Lock (CapsLockDoubleTapWatcher). The watcher polls only the Caps Lock key's state
-- no system-wide keyboard hook, so Clicky never sees anything else that's typed. Two taps toggle Caps
Lock twice, so it ends up unchanged; single taps and holds behave as normal Caps Lock.

Capture approach (PyAudioWPatch + soxr) is the one Live Subtitles already uses on Windows.
"""
import ctypes
import hashlib
import io
import logging
import os
import threading
import time
import wave
from datetime import datetime

import numpy as np

log = logging.getLogger("live_recorder")

RATE = 16000                 # what the device sends and what storage.add_pending expects
_GAP_TOLERANCE = RATE // 10  # loopback delivers nothing while the PC is silent; re-sync after >100 ms gaps
_MIN_SECONDS = 1.0           # shorter than this is almost certainly an accidental start/stop
VK_CAPITAL = 0x14
VK_SHIFT = 0x10


def fmt_offset(seconds: float) -> str:
    """Recording time offset as m:ss (or h:mm:ss) -- how transcript lines and screenshots are stamped."""
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"

# Each capture source is one transcript speaker.
MIC, COMPUTER = "you", "others"
SOURCE_LABELS = {MIC: "microphone", COMPUTER: "computer audio"}


class _Source:
    """One capture stream, resampled to 16 kHz mono and placed on a shared wall-clock timeline."""

    def __init__(self, p, pa, source_id: str, info: dict, t0: float, on_audio=None, keep_audio: bool = True):
        import soxr
        self.source_id, self._pa, self._t0, self._on_audio = source_id, pa, t0, on_audio
        self.keep_audio = keep_audio   # False = subtitles only: audio goes to the live transcriber and nowhere else
        self.device_name = info.get("name", "")
        self.rate = int(info["defaultSampleRate"])
        self.channels = max(1, int(info["maxInputChannels"]))
        self._resampler = soxr.ResampleStream(self.rate, RATE, 1, dtype="float32")
        self._parts = []     # (position_in_samples, float32 array)
        self._cursor = 0
        self.recent_peak = 0.0   # loudest sample since the last take_recent_peak() -- exactly 0 while muted
        self.last_audio = time.monotonic()   # a mic delivers (zeros) even while muted; nothing at all = gone
        self._lock = threading.Lock()
        self.stream = p.open(format=pa.paFloat32, channels=self.channels, rate=self.rate, input=True,
                             input_device_index=info["index"], frames_per_buffer=int(self.rate * 0.03),
                             stream_callback=self._callback)

    def _callback(self, in_data, frame_count, time_info, status):
        arrived = time.monotonic()
        self.last_audio = arrived
        x = np.frombuffer(in_data, dtype=np.float32)
        if self.channels > 1:
            x = x.reshape(-1, self.channels).mean(axis=1)
        if x.size:
            self.recent_peak = max(self.recent_peak, float(np.max(np.abs(x))))
        y = self._resampler.resample_chunk(np.ascontiguousarray(x))
        if len(y):
            with self._lock:
                expected_start = int((arrived - self._t0) * RATE) - len(y)
                if expected_start > self._cursor + _GAP_TOLERANCE:
                    self._cursor = expected_start   # skip ahead over a silent gap instead of drifting early
                if self.keep_audio:
                    self._parts.append((self._cursor, y))
                self._cursor += len(y)
            if self._on_audio:
                self._on_audio(self.source_id, arrived, y)
        return (None, self._pa.paContinue)

    @property
    def alive(self) -> bool:
        try:
            return self.stream.is_active()
        except Exception:
            return False

    def close(self):
        try:
            self.stream.stop_stream()
            self.stream.close()
        except Exception:
            pass

    def timeline(self, length: int) -> np.ndarray:
        out = np.zeros(length, dtype=np.float32)
        with self._lock:
            for pos, y in self._parts:
                end = min(pos + len(y), length)
                if end > pos:
                    out[pos:end] += y[: end - pos]
        return out

    @property
    def length(self) -> int:
        with self._lock:
            return self._cursor


def list_devices() -> dict:
    """Names for the settings page: {"mics": [...], "outputs": [...]} (outputs = what you hear)."""
    import pyaudiowpatch as pa
    p = pa.PyAudio()
    try:
        wasapi = p.get_host_api_info_by_type(pa.paWASAPI)["index"]
        mics, outputs = [], []
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if d["hostApi"] != wasapi:
                continue
            if d.get("isLoopbackDevice"):
                outputs.append(d["name"].replace(" [Loopback]", ""))
            elif d["maxInputChannels"] > 0:
                mics.append(d["name"])
        return {"mics": mics, "outputs": outputs}
    finally:
        p.terminate()


def _pick_mic(p, pa, name: str):
    if name:
        wasapi = p.get_host_api_info_by_type(pa.paWASAPI)["index"]
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if d["hostApi"] == wasapi and not d.get("isLoopbackDevice") and d["maxInputChannels"] > 0 and d["name"] == name:
                return d
        log.warning("chosen microphone %r not found, using the Windows default", name)
    return p.get_default_wasapi_device(d_in=True)


def _pick_loopbacks(p, name: str, also: str = "") -> list:
    """Loopback devices to record "computer audio" from: the chosen device, or else the Windows default
    output AND the default communications output (`also`) when they differ -- Teams often plays the call
    on the communications device (e.g. a monitor or headset) while other sound goes to the default one."""
    loops = {d["name"].replace(" [Loopback]", ""): d for d in p.get_loopback_device_info_generator()}
    if name:
        if name in loops:
            return [loops[name]]
        log.warning("chosen computer-audio device %r not found, using the Windows default", name)
    out = [p.get_default_wasapi_loopback()]
    if also and also in loops and loops[also]["index"] != out[0]["index"]:
        out.append(loops[also])
    return out


def _desired_devices(snap: dict, mic_pref: str, out_pref: str, with_computer: bool):
    """Which devices a recording should be using right now, from audio_devices.snapshot()."""
    mic = mic_pref if mic_pref and mic_pref in snap["mics"] else snap["mic"]
    if not with_computer:
        outs = frozenset()
    elif out_pref and out_pref in snap["outputs"]:
        outs = frozenset([out_pref])
    else:
        outs = frozenset(n for n in (snap["output"], snap["output_comm"]) if n)
    return mic, outs


def _snapshot():
    try:
        import audio_devices
        return audio_devices.snapshot()
    except Exception as e:
        log.debug("couldn't read Windows' audio devices: %s", e)
        return None


class LiveRecorder:
    def __init__(self):
        self._p = None
        self._sources = []
        self._retired = []       # mic sources replaced mid-recording (switch_mic); still mixed into the WAV
        self.transcriber = None
        self.started_at = None
        self.started_monotonic = None
        self.sources_description = ""
        self.keep_audio = True
        self.last_tracks = {}    # {source_id: WAV bytes} of the last stop(), one per speaker source
        self._lock = threading.RLock()   # capture changes (device follower, mic fallback) vs. stop()
        self._prefs = ("", "", True)     # (chosen mic, chosen output, record computer audio) of this session
        self.on_device_change = None     # fn(message) -- the Controller turns it into a notification

    @property
    def recording(self) -> bool:
        return self._p is not None

    def find_live_mic(self, probe_s: float = 1.5):
        """Another input device that's actually delivering audio (not exact zeros), loudest first --
        or None. For when the recorded mic is a dongle whose headset is off, a disabled mic, etc."""
        import pyaudiowpatch as pa
        with self._lock:
            return self._find_live_mic(pa, probe_s)

    def _find_live_mic(self, pa, probe_s):
        if not self.recording:
            return None
        current = next((s for s in self._sources if s.source_id == MIC), None)
        wasapi = self._p.get_host_api_info_by_type(pa.paWASAPI)["index"]
        peaks, streams = {}, []
        for i in range(self._p.get_device_count()):
            d = self._p.get_device_info_by_index(i)
            if (d["hostApi"] != wasapi or d.get("isLoopbackDevice") or d["maxInputChannels"] <= 0
                    or (current and d["name"] == current.device_name)):
                continue

            def cb(data, n, ti, st, name=d["name"]):
                x = np.frombuffer(data, dtype=np.float32)
                if x.size:
                    peaks[name] = max(peaks.get(name, 0.0), float(np.abs(x).max()))
                return (None, pa.paContinue)
            try:
                streams.append((d, self._p.open(format=pa.paFloat32, channels=max(1, int(d["maxInputChannels"])),
                                                rate=int(d["defaultSampleRate"]), input=True, input_device_index=d["index"],
                                                frames_per_buffer=1024, stream_callback=cb)))
            except Exception:
                continue
        time.sleep(probe_s)
        for _, s in streams:
            try:
                s.close()
            except Exception:
                pass
        alive = sorted((d for d, _ in streams if peaks.get(d["name"], 0.0) > 0.0),
                       key=lambda d: -peaks[d["name"]])
        return alive[0] if alive else None

    def switch_mic(self, info: dict):
        """Swap the microphone source for another device mid-recording; the shared wall-clock timeline
        keeps it in sync with the rest of the recording (already-recorded audio stays)."""
        import pyaudiowpatch as pa
        with self._lock:
            if not self.recording:
                return
            old = next((s for s in self._sources if s.source_id == MIC), None)
            feed = self.transcriber.feed if self.transcriber else None
            new = _Source(self._p, pa, MIC, info, self.started_monotonic, feed, self.keep_audio)
            if old:
                old.close()
                self._retired.append(old)            # its audio so far still goes into the WAV
                self._sources.remove(old)
            self._sources.insert(0, new)
            self._describe()
            log.info("switched microphone: %s -> %s", old.device_name if old else None, new.device_name)

    def take_mic_peak(self):
        """Loudest microphone sample since the previous call (None without a mic source)."""
        mic = next((s for s in self._sources if s.source_id == MIC), None)
        if mic is None:
            return None
        peak, mic.recent_peak = mic.recent_peak, 0.0
        return peak

    def start(self, include_computer_audio: bool = True, live_transcription: bool = True,
              mic_device: str = "", output_device: str = "", keep_audio: bool = True):
        """Raises RuntimeError with a user-facing message if no microphone can be opened.
        mic_device / output_device: device names from list_devices() ("" = Windows default).
        keep_audio=False: subtitles only -- nothing is buffered, stop() returns no audio."""
        if self.recording:
            return
        self.keep_audio = keep_audio
        import pyaudiowpatch as pa
        p = pa.PyAudio()
        t0 = time.monotonic()
        wanted = [MIC] + ([COMPUTER] if include_computer_audio else [])

        self.transcriber = None
        if live_transcription:
            try:
                import live_transcriber
                if live_transcriber.is_enabled():
                    self.transcriber = live_transcriber.LiveTranscriber(t0, wanted)
            except Exception as e:
                log.warning("live transcription unavailable, recording audio only: %s", e)
        feed = self.transcriber.feed if self.transcriber else None

        self._prefs = (mic_device, output_device, include_computer_audio)
        snap = _snapshot()
        try:
            sources = self._open_sources(p, pa, t0, feed, snap, require_mic=True)
        except Exception as e:
            p.terminate()
            raise RuntimeError(f"Couldn't open the microphone: {e}")
        self._p, self._sources, self._retired = p, sources, []
        self.started_at, self.started_monotonic = datetime.now(), t0
        self._describe()
        log.info("live recording started (%s, live transcription %s)", self.sources_description,
                 "on" if self.transcriber else "off")
        threading.Thread(target=self._follow_devices, args=(t0, snap), daemon=True, name="device-follower").start()

    def _open_sources(self, p, pa, t0, feed, snap, require_mic=False) -> list:
        mic_pref, out_pref, with_computer = self._prefs
        sources = []
        try:
            sources.append(_Source(p, pa, MIC, _pick_mic(p, pa, mic_pref), t0, feed, self.keep_audio))
        except Exception as e:
            if require_mic:
                raise
            log.warning("couldn't open the microphone after a device change: %s", e)
        if with_computer:
            try:
                for info in _pick_loopbacks(p, out_pref, (snap or {}).get("output_comm", "")):
                    sources.append(_Source(p, pa, COMPUTER, info, t0, feed, self.keep_audio))
            except Exception as e:
                log.warning("computer-audio capture unavailable, recording microphone only: %s", e)
        return sources

    def _describe(self):
        mic = next((s.device_name for s in self._sources if s.source_id == MIC), "")
        outs = [s.device_name.replace(" [Loopback]", "") for s in self._sources if s.source_id == COMPUTER]
        bits = []
        if mic:
            bits.append(f"microphone ({mic})")
        if outs:
            bits.append(f"computer audio ({', '.join(outs)})")
        self.sources_description = " + ".join(bits) or "nothing (no audio device)"

    def _follow_devices(self, started, snap, every_s: float = 2.0):
        """Keeps the recording on the right devices when they change mid-meeting: a headset connected or
        removed, a new Windows default, or the recorded mic disappearing. PortAudio can't see new devices
        while it runs, so the capture is reopened -- a fraction of a second -- on the same timeline;
        everything recorded so far is kept."""
        try:
            ctypes.windll.ole32.CoInitializeEx(None, 0)
        except Exception:
            pass
        last = _desired_devices(snap, *self._prefs) if snap else None
        while self.recording and self.started_monotonic == started:
            time.sleep(every_s)
            snap = _snapshot()
            if snap is None or not self.recording or self.started_monotonic != started:
                continue
            want = _desired_devices(snap, *self._prefs)
            mic = next((s for s in self._sources if s.source_id == MIC), None)
            mic_gone = mic is not None and (not mic.alive or time.monotonic() - mic.last_audio > 3 * every_s)
            if want == last and not mic_gone:
                continue
            last = want
            try:
                if self.reopen(started, snap):
                    log.info("audio devices changed (mic gone: %s), now recording %s", mic_gone, self.sources_description)
                    if self.on_device_change:
                        self.on_device_change(f"Audio devices changed - now recording {self.sources_description}.")
            except Exception as e:
                log.warning("couldn't follow the audio device change: %s", e)

    def reopen(self, started, snap=None) -> bool:
        """Closes and reopens all capture on the current devices (with a fresh PortAudio device list)."""
        import pyaudiowpatch as pa
        with self._lock:
            if not self.recording or self.started_monotonic != started:
                return False
            for s in self._sources:
                s.close()
            self._retired.extend(self._sources)       # their audio so far stays in the recording
            self._sources = []
            self._p.terminate()
            self._p = pa.PyAudio()
            feed = self.transcriber.feed if self.transcriber else None
            self._sources = self._open_sources(self._p, pa, started, feed, snap or _snapshot())
            self._describe()
            return True

    def stop(self):
        """Stops capture. Returns (wav_bytes, segments): 16 kHz mono 16-bit WAV, and the live transcript
        segments (None when live transcription was off). wav_bytes is None for a sub-second recording."""
        with self._lock:
            if not self.recording:
                return None, None
            for s in self._sources:
                s.close()
            self._p.terminate()
            sources, self._p, self._sources = self._sources + self._retired, None, []
            self._retired = []
        segments = self.transcriber.finish() if self.transcriber else None

        length = max((s.length for s in sources), default=0)
        log.info("live %s stopped (%.1fs, %s live segments)", "recording" if self.keep_audio else "subtitles",
                 length / RATE, len(segments) if segments is not None else "no")
        self.last_tracks = {}
        if not self.keep_audio or length < _MIN_SECONDS * RATE:
            return None, segments
        # One track per speaker source (a mic swapped mid-recording is merged back into "you") -- kept so
        # the after-recording accuracy pass (transcript_refiner.py) can still tell You from Others.
        tracks = {}
        for s in sources:
            tracks[s.source_id] = tracks.get(s.source_id, 0) + s.timeline(length)
        self.last_tracks = {sid: _to_wav(arr) for sid, arr in tracks.items()}
        mix = sum(tracks.values())
        peak = float(np.max(np.abs(mix))) if length else 0.0
        if peak > 1.0:
            mix /= peak   # two loud sources summed -- scale down rather than clip
        return _to_wav(mix), segments


def _to_wav(samples: np.ndarray) -> bytes:
    """float32 16 kHz mono -> 16-bit PCM WAV bytes."""
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def ingest(wav_bytes: bytes, started_at: datetime, segments=None, screenshots=None, tracks=None,
           in_call=None) -> str:
    """Adds a finished live recording to the normal pipeline. With live segments, the transcript is
    stored as-is (poller.process_once skips batch transcription for it). screenshots: [{t, time, file,
    window}] taken during the recording (t = seconds from its start, same clock as segment start/end).
    tracks: {source_id: WAV bytes} per speaker source; with the accuracy pass on (transcript_refiner),
    the recording waits in status "refining" until transcript_refiner has transcribed them by speaker.
    Returns the recording's name."""
    import paths
    import settings
    import storage
    import transcript_refiner
    name = f"live_{started_at:%Y-%m-%d_%H-%M-%S}.wav"
    content_hash = hashlib.md5(wav_bytes).hexdigest()
    owner = (settings.get_all().get("owner_name") or "").strip() or "You"
    # The mic is the owner only during an online call; in the room everyone talks into it.
    mic_name = owner if in_call or in_call is None else "Microphone (room)"
    extra = {"screenshots": list(screenshots or []), "speaker_names": {MIC: mic_name, COMPUTER: "Others"}}
    if in_call is not None:
        extra["in_call"] = bool(in_call)
    if segments:
        extra["prefilled_transcription"] = {"text": " ".join(s["text"] for s in segments),
                                            "segments": segments, "stt_provider": "parakeet-live"}
    refine = bool(tracks) and transcript_refiner.is_enabled()
    if tracks:
        folder = os.path.join(paths.APP_DATA_DIR, "tracks")
        os.makedirs(folder, exist_ok=True)
        extra["source_tracks"] = {}
        for sid, data in tracks.items():
            path = os.path.join(folder, f"{name[:-4]}__{sid}.wav")
            with open(path, "wb") as f:
                f.write(data)
            extra["source_tracks"][sid] = path
    if refine:
        extra["status"] = "refining"      # poller.process_once only picks up pending/failed
    storage.add_pending(name, len(wav_bytes), content_hash, wav_bytes, extra=extra)
    if refine:
        transcript_refiner.enqueue(content_hash)
    return name


class Controller:
    """The single start/stop entry point (tray, Caps Lock double tap, dashboard). Listeners get
    on_change(event, info) with event in started / stopped / saved / discarded / failed."""

    def __init__(self):
        self.recorder = LiveRecorder()
        self.recorder.on_device_change = lambda msg: self._fire("info", msg)
        self._lock = threading.Lock()
        self._listeners = []
        self.last_saved = None   # name of the last ingested recording, for the dashboard
        self.auto_started = False  # this recording was started by CallWatcher (so it also ends it)
        self.mode = None           # "record" | "subtitles" while a session runs
        self.mic_state = ""      # checking / live / muted / muted_in_call -- kept current by _monitor_mic()
        self.call = ""           # the running call, when one was detected while the mic was muted
        self.screenshots = []    # [{t, time, file, window}] for the current recording (take_screenshot)
        self.saw_call = False    # a Teams/Zoom/Meet call ran at some point during this recording

    def add_listener(self, fn):
        self._listeners.append(fn)

    def _fire(self, event, info=""):
        for fn in list(self._listeners):
            try:
                fn(event, info)
            except Exception:
                log.exception("live recording listener failed")

    @property
    def active(self) -> bool:
        """A recording OR a subtitles-only session is running (audio devices open)."""
        return self.recorder.recording

    @property
    def recording(self) -> bool:
        """A recording that will be saved is running (not subtitles-only)."""
        return self.recorder.recording and self.mode == "record"

    @property
    def subtitles_only(self) -> bool:
        return self.recorder.recording and self.mode == "subtitles"

    def toggle(self, *_):
        """Caps Lock double tap / tray Start-Stop: start or stop a recording. During subtitles-only it
        switches to a recording (the subtitles keep going; from then on it's saved)."""
        if self.recording:
            return self.stop()
        return self.start()

    def toggle_subtitles(self, *_):
        """Subtitles-only switch (tray / dashboard): live subtitles without recording anything."""
        if self.subtitles_only:
            return self.stop()
        if self.recording:
            self._fire("info", "Already recording - the subtitles are on as part of the recording.")
            return
        return self.start(subtitles_only=True)

    def start(self, reason: str = "", auto: bool = False, subtitles_only: bool = False):
        """reason: shown in the "started" notification (e.g. "Teams call detected"); auto: started by
        CallWatcher, which then also stops it when the call ends. subtitles_only: transcribe + show the
        subtitle bar, keep nothing (no audio, no transcript, nothing saved)."""
        import settings
        if not subtitles_only and self.subtitles_only:
            self.stop()                               # subtitles-only -> real recording
        with self._lock:
            if self.recorder.recording:
                return
            saved = settings.get_all()
            if subtitles_only:
                import live_transcriber
                if not live_transcriber.models_available():
                    self._fire("failed", "Live subtitles need the transcription model - run "
                                         "`python live_transcriber.py` once to download it.")
                    return
            try:
                self.recorder.start(include_computer_audio=saved.get("live_record_computer_audio", True),
                                    live_transcription=True if subtitles_only else saved.get("live_transcription", True),
                                    mic_device=saved.get("live_mic_device", ""),
                                    output_device=saved.get("live_output_device", ""),
                                    keep_audio=not subtitles_only)
            except Exception as e:
                self._fire("failed", str(e))
                return
            self.mode = "subtitles" if subtitles_only else "record"
            self.auto_started = auto
            self.screenshots = []
            self.saw_call = auto
        self.mic_state, self.call = "checking", ""
        if subtitles_only:
            self._fire("subtitles_on", self.recorder.sources_description)
        else:
            self._fire("started", (f"{reason} - " if reason else "") + self.recorder.sources_description +
                       (", transcribing live" if self.recorder.transcriber else ""))
        threading.Thread(target=self._monitor_mic, args=(self.recorder.started_monotonic,), daemon=True).start()

    def take_screenshot(self, *_):
        """Screenshot of the running meeting's window, stamped with the recording's time offset (the same
        clock as the transcript's segment times) and the wall-clock time. Only while recording a call on
        this computer."""
        import meeting_screenshot
        import paths
        if not self.recording:
            self._fire("screenshot_failed", "Start a recording first (double-tap Caps Lock).")
            return
        rec = self.recorder
        t = time.monotonic() - rec.started_monotonic
        wall = datetime.now()
        try:
            img, title = meeting_screenshot.capture_meeting()
        except Exception as e:
            self._fire("screenshot_failed", str(e))
            return
        stamp = f"{int(t) // 3600:02d}-{int(t) % 3600 // 60:02d}-{int(t) % 60:02d}"
        folder = os.path.join(paths.APP_DATA_DIR, "screenshots", f"live_{rec.started_at:%Y-%m-%d_%H-%M-%S}")
        path = meeting_screenshot.save(img, folder, f"live_{rec.started_at:%Y-%m-%d_%H-%M-%S}__t{stamp}__{wall:%H-%M-%S}.png")
        self.screenshots.append({"t": round(t, 1), "time": wall.isoformat(timespec="seconds"), "file": path,
                                 "window": title})
        self._fire("screenshot", fmt_offset(t))

    def _monitor_mic(self, started, first_check_s: float = 3.0, every_s: float = 2.0):
        """A muted mic delivers exact digital zeros, so there's simply nothing of the user in the recording.
        During a call that's fine (the meeting audio is still recorded) -- no warning. With no call running
        (an in-person conversation) the user is asked to unmute; Clicky never unmutes anything itself."""
        import meeting_detector
        # WASAPI needs COM in every thread that opens audio devices; without it the live-mic probe below
        # opens streams that never deliver audio, so a silent mic was never swapped for a working one.
        try:
            ctypes.windll.ole32.CoInitializeEx(None, 0)
        except Exception:
            pass
        warned, tried_switch = False, False
        time.sleep(first_check_s)
        while self.recorder.recording and self.recorder.started_monotonic == started:
            if not self.saw_call:
                try:
                    self.saw_call = bool(meeting_detector.current_call())
                except Exception:
                    pass
            peak = self.recorder.take_mic_peak()
            if peak is None:                        # no microphone source at all
                time.sleep(every_s)
                continue
            if peak == 0.0 and not tried_switch:
                # Exact zeros can also mean the wrong device (a headset dongle with the headset off, a
                # disabled mic) while the user talks into another one -- switch to a live mic first.
                tried_switch = True
                try:
                    current = next((s.device_name for s in self.recorder._sources if s.source_id == MIC), "")
                    live = self.recorder.find_live_mic()
                    if live and self.recorder.recording and self.recorder.started_monotonic == started:
                        self.recorder.switch_mic(live)
                        self.mic_state = "live"
                        self._fire("info", f'"{current}" isn\'t sending any sound, so Clicky switched to '
                                           f'"{live["name"]}". Pick your microphone in Settings -> Live Subtitles '
                                           "to make this permanent.")
                        time.sleep(every_s)
                        continue
                except Exception as e:
                    log.warning("microphone fallback failed: %s", e)
            if peak == 0.0:
                try:
                    self.call = meeting_detector.current_call()
                except Exception:
                    self.call = ""
                if self.call:
                    self.mic_state = "muted_in_call"
                else:
                    self.mic_state = "muted"
                    if not warned:
                        warned = True
                        self._fire("warning", "Your microphone is muted, so your voice isn't being picked up. "
                                              "Unmute it (headset mute button or boom arm) to include it - "
                                              "Clicky won't unmute it for you.")
            else:
                self.mic_state, self.call = "live", ""
                if warned:
                    warned = False
                    self._fire("info", "Your microphone is live - Clicky is recording your voice too.")
            time.sleep(every_s)

    def stop(self):
        with self._lock:
            if not self.recorder.recording:
                return
            mode, started_at = self.mode, self.recorder.started_at
            if mode == "subtitles":
                self.recorder.stop()                  # transcript and audio are simply dropped
                self.mode = None
            else:
                self._fire("stopping")
                wav_bytes, segments = self.recorder.stop()
                self.mode = None
        if mode == "subtitles":
            self._fire("subtitles_off", "")
            return
        if wav_bytes is None:
            self._fire("discarded", "That was under a second, so nothing was saved.")
            return
        threading.Thread(target=self._save, args=(wav_bytes, started_at, segments, list(self.screenshots),
                                                  dict(self.recorder.last_tracks), self.saw_call), daemon=True).start()

    def _save(self, wav_bytes, started_at, segments, screenshots, tracks, in_call):
        try:
            self.last_saved = ingest(wav_bytes, started_at, segments, screenshots, tracks, in_call)
            self._fire("saved", self.last_saved)
        except Exception as e:
            self._fire("failed", f"Couldn't save the recording: {e}")

    def status(self) -> dict:
        """For the dashboard's live panel (app.py /api/live)."""
        import settings
        rec = self.recorder
        out = {"recording": self.recording, "subtitles_only": self.subtitles_only, "active": self.active,
               "last_saved": self.last_saved, "screenshots": len(self.screenshots),
               "auto_record": bool(settings.get_all().get("auto_record_calls", False)),
               "auto_started": self.recording and self.auto_started}
        if rec.recording:
            out.update(sources=rec.sources_description,
                       elapsed=round(time.monotonic() - rec.started_monotonic, 1),
                       live_transcription=bool(rec.transcriber), mic_state=self.mic_state, call=self.call)
            if rec.transcriber:
                out.update(rec.transcriber.snapshot())
        return out


controller = Controller()


def _call_label(call: str) -> str:
    c = call.lower()
    for key, label in (("teams", "Teams"), ("zoom", "Zoom"), ("webex", "Webex"), ("slack", "Slack"), ("meet", "Google Meet")):
        if key in c:
            return label
    return "Online meeting"


class CallWatcher(threading.Thread):
    """Opt-in (settings "auto_record_calls"): records Teams / Zoom / Meet calls automatically.
    Starts once a call has been seen for `start_after_s` (a quick mic check in Teams doesn't count) and
    stops `end_grace_s` after it ends (a short dropout doesn't split the recording). Only recordings it
    started itself are auto-stopped, and if the user stops one mid-call it isn't restarted for that call."""

    def __init__(self, ctl: Controller, poll_s: float = 3.0, start_after_s: float = 6.0, end_grace_s: float = 20.0):
        super().__init__(daemon=True, name="call-watcher")
        self._ctl, self._poll, self._start_after, self._end_grace = ctl, poll_s, start_after_s, end_grace_s
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        import meeting_detector
        import settings
        first_seen = last_seen = None
        we_started = handled_this_call = False
        while not self._stop_event.wait(self._poll):
            if not settings.get_all().get("auto_record_calls", False):
                first_seen = last_seen = None
                we_started = handled_this_call = False
                continue
            try:
                call = meeting_detector.current_call()
            except Exception:
                call = ""
            now = time.monotonic()
            if call:
                last_seen = now
                first_seen = first_seen or now
                if we_started and not self._ctl.recording:
                    we_started = False             # the user stopped it: leave this call alone
                if self._ctl.recording and not we_started:
                    handled_this_call = True       # the user is already recording this call themselves
                if not handled_this_call and not self._ctl.recording and now - first_seen >= self._start_after:
                    handled_this_call = True
                    self._ctl.start(reason=f"{_call_label(call)} call detected", auto=True)
                    we_started = self._ctl.recording
            elif last_seen is not None and now - last_seen >= self._end_grace:
                if we_started and self._ctl.recording and self._ctl.auto_started:
                    self._ctl.stop()
                first_seen = last_seen = None
                we_started = handled_this_call = False


class KeyDoubleTapWatcher(threading.Thread):
    """Calls on_double_tap() when `vk` is tapped twice quickly (second press within `window_s` of the
    first, both short taps), if the `setting_key` switch is on and `condition()` (if given) is true.
    Polls GetAsyncKeyState for this one key only (no keyboard hook). A press held longer than
    `max_tap_s` doesn't count, so holding the key stays ordinary. For Caps Lock, two taps toggle it twice,
    so it ends up where it started -- nothing to restore."""

    def __init__(self, on_double_tap, vk: int = VK_CAPITAL, setting_key: str = "capslock_record_hotkey",
                 condition=None, window_s: float = 0.4, max_tap_s: float = 0.35,
                 cooldown_s: float = 0.8, poll_seconds: float = 0.015):
        super().__init__(daemon=True, name=f"doubletap-{vk:#x}")
        self._on_double_tap, self._vk, self._setting_key, self._condition = on_double_tap, vk, setting_key, condition
        self._window, self._max_tap, self._cooldown, self._poll = window_s, max_tap_s, cooldown_s, poll_seconds
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        user32 = ctypes.windll.user32
        user32.GetAsyncKeyState(self._vk)              # clear the "pressed since last call" bit
        was_down, down_at, last_tap, quiet_until = False, 0.0, None, 0.0
        enabled, enabled_checked = True, 0.0
        while not self._stop_event.wait(self._poll):
            now = time.monotonic()
            if now - enabled_checked > 1.0:            # the Settings switch applies without a restart
                import settings
                enabled, enabled_checked = settings.get_all().get(self._setting_key, True), now
            state = user32.GetAsyncKeyState(self._vk)
            if not enabled:
                was_down, last_tap = False, None
                continue
            is_down = bool(state & 0x8000)
            tapped_between_polls = bool(state & 1) and not is_down and not was_down   # a press shorter than one poll
            if is_down and not was_down:
                down_at = now
            released = (was_down and not is_down) or tapped_between_polls
            if released:
                short = tapped_between_polls or (now - down_at) <= self._max_tap
                if not short:
                    last_tap = None
                elif now < quiet_until:
                    pass
                elif last_tap is not None and now - last_tap <= self._window:
                    last_tap, quiet_until = None, now + self._cooldown
                    # off the polling thread: stopping waits for the live transcriber to catch up
                    threading.Thread(target=self._safe_fire, daemon=True).start()
                else:
                    last_tap = now
            was_down = is_down

    def _safe_fire(self):
        try:
            if self._condition is None or self._condition():
                self._on_double_tap()
        except Exception:
            log.exception("double-tap action failed")


def CapsLockDoubleTapWatcher(on_double_tap):
    """Double-tap Caps Lock: start / stop a live recording."""
    return KeyDoubleTapWatcher(on_double_tap, VK_CAPITAL, "capslock_record_hotkey")


def ShiftDoubleTapWatcher(on_double_tap, ctl):
    """Double-tap Shift: screenshot the meeting -- only while recording AND a call runs on this computer,
    so Shift keeps working normally the rest of the time (a single Shift press is every capital letter)."""
    def during_call_recording():
        import meeting_detector
        return ctl.recording and bool(meeting_detector.current_call())
    return KeyDoubleTapWatcher(on_double_tap, VK_SHIFT, "shift_screenshot_hotkey", condition=during_call_recording)
