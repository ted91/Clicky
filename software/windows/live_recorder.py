"""Live conversation recording on Windows (no device needed).

Records the default microphone and, optionally, what the computer is playing (WASAPI loopback --
the other side of a Teams/Zoom/Meet call). The audio is saved as one 16 kHz mono WAV AND, when the
models are installed, transcribed live while recording (live_transcriber.py, Parakeet). The finished
recording -- audio plus the live transcript -- goes into the normal pipeline via storage.add_pending(),
so it appears on the dashboard like a device recording, without a second transcription pass.

Started/stopped by `controller` (one per process): the tray menu, a dashboard button, and holding
Caps Lock for about a second (CapsLockHoldWatcher). The watcher polls only the Caps Lock key's state
-- no system-wide keyboard hook, so Clicky never sees anything else that's typed. A hold flips Caps
Lock like any press, so the watcher flips it back afterwards; short taps behave as normal Caps Lock.

Capture approach (PyAudioWPatch + soxr) is the one Live Subtitles already uses on Windows.
"""
import ctypes
import hashlib
import io
import logging
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

# Each capture source is one transcript speaker.
MIC, COMPUTER = "you", "others"
SOURCE_LABELS = {MIC: "microphone", COMPUTER: "computer audio"}


class _Source:
    """One capture stream, resampled to 16 kHz mono and placed on a shared wall-clock timeline."""

    def __init__(self, p, pa, source_id: str, info: dict, t0: float, on_audio=None):
        import soxr
        self.source_id, self._pa, self._t0, self._on_audio = source_id, pa, t0, on_audio
        self.rate = int(info["defaultSampleRate"])
        self.channels = max(1, int(info["maxInputChannels"]))
        self._resampler = soxr.ResampleStream(self.rate, RATE, 1, dtype="float32")
        self._parts = []     # (position_in_samples, float32 array)
        self._cursor = 0
        self.recent_peak = 0.0   # loudest sample since the last take_recent_peak() -- exactly 0 while muted
        self._lock = threading.Lock()
        self.stream = p.open(format=pa.paFloat32, channels=self.channels, rate=self.rate, input=True,
                             input_device_index=info["index"], frames_per_buffer=int(self.rate * 0.03),
                             stream_callback=self._callback)

    def _callback(self, in_data, frame_count, time_info, status):
        arrived = time.monotonic()
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
                self._parts.append((self._cursor, y))
                self._cursor += len(y)
            if self._on_audio:
                self._on_audio(self.source_id, arrived, y)
        return (None, self._pa.paContinue)

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


def _pick_loopback(p, name: str):
    if name:
        for d in p.get_loopback_device_info_generator():
            if d["name"].replace(" [Loopback]", "") == name:
                return d
        log.warning("chosen computer-audio device %r not found, using the Windows default", name)
    return p.get_default_wasapi_loopback()


class LiveRecorder:
    def __init__(self):
        self._p = None
        self._sources = []
        self.transcriber = None
        self.started_at = None
        self.started_monotonic = None
        self.sources_description = ""

    @property
    def recording(self) -> bool:
        return self._p is not None

    def take_mic_peak(self):
        """Loudest microphone sample since the previous call (None without a mic source)."""
        mic = next((s for s in self._sources if s.source_id == MIC), None)
        if mic is None:
            return None
        peak, mic.recent_peak = mic.recent_peak, 0.0
        return peak

    def start(self, include_computer_audio: bool = True, live_transcription: bool = True,
              mic_device: str = "", output_device: str = ""):
        """Raises RuntimeError with a user-facing message if no microphone can be opened.
        mic_device / output_device: device names from list_devices() ("" = Windows default)."""
        if self.recording:
            return
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

        sources = []
        try:
            sources.append(_Source(p, pa, MIC, _pick_mic(p, pa, mic_device), t0, feed))
        except Exception as e:
            p.terminate()
            raise RuntimeError(f"Couldn't open the microphone: {e}")
        if include_computer_audio:
            try:
                sources.append(_Source(p, pa, COMPUTER, _pick_loopback(p, output_device), t0, feed))
            except Exception as e:
                log.warning("computer-audio capture unavailable, recording microphone only: %s", e)
        self._p, self._sources = p, sources
        self.started_at, self.started_monotonic = datetime.now(), t0
        self.sources_description = " + ".join(SOURCE_LABELS[s.source_id] for s in sources)
        log.info("live recording started (%s, live transcription %s)", self.sources_description,
                 "on" if self.transcriber else "off")

    def stop(self):
        """Stops capture. Returns (wav_bytes, segments): 16 kHz mono 16-bit WAV, and the live transcript
        segments (None when live transcription was off). wav_bytes is None for a sub-second recording."""
        if not self.recording:
            return None, None
        for s in self._sources:
            s.close()
        self._p.terminate()
        sources, self._p, self._sources = self._sources, None, []
        segments = self.transcriber.finish() if self.transcriber else None

        length = max((s.length for s in sources), default=0)
        log.info("live recording stopped (%.1fs, %s live segments)", length / RATE,
                 len(segments) if segments is not None else "no")
        if length < _MIN_SECONDS * RATE:
            return None, segments
        mix = sum(s.timeline(length) for s in sources)
        peak = float(np.max(np.abs(mix))) if length else 0.0
        if peak > 1.0:
            mix /= peak   # two loud sources summed -- scale down rather than clip
        pcm = (np.clip(mix, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(pcm)
        return buf.getvalue(), segments


def ingest(wav_bytes: bytes, started_at: datetime, segments=None) -> str:
    """Adds a finished live recording to the normal pipeline. With live segments, the transcript is
    stored as-is (poller.process_once skips batch transcription for it). Returns the recording's name."""
    import settings
    import storage
    name = f"live_{started_at:%Y-%m-%d_%H-%M-%S}.wav"
    extra = None
    if segments:
        owner = (settings.get_all().get("owner_name") or "").strip() or "You"
        extra = {
            "prefilled_transcription": {"text": " ".join(s["text"] for s in segments),
                                        "segments": segments, "stt_provider": "parakeet-live"},
            "speaker_names": {MIC: owner, COMPUTER: "Others"},
        }
    storage.add_pending(name, len(wav_bytes), hashlib.md5(wav_bytes).hexdigest(), wav_bytes, extra=extra)
    return name


class Controller:
    """The single start/stop entry point (tray, Caps Lock hold, dashboard). Listeners get
    on_change(event, info) with event in started / stopped / saved / discarded / failed."""

    def __init__(self):
        self.recorder = LiveRecorder()
        self._lock = threading.Lock()
        self._listeners = []
        self.last_saved = None   # name of the last ingested recording, for the dashboard
        self.auto_started = False  # this recording was started by CallWatcher (so it also ends it)
        self.mic_state = ""      # checking / live / muted / muted_in_call -- kept current by _monitor_mic()
        self.call = ""           # the running call, when one was detected while the mic was muted

    def add_listener(self, fn):
        self._listeners.append(fn)

    def _fire(self, event, info=""):
        for fn in list(self._listeners):
            try:
                fn(event, info)
            except Exception:
                log.exception("live recording listener failed")

    @property
    def recording(self) -> bool:
        return self.recorder.recording

    def toggle(self, *_):
        return self.stop() if self.recording else self.start()

    def start(self, reason: str = "", auto: bool = False):
        """reason: shown in the "started" notification (e.g. "Teams call detected"); auto: started by
        CallWatcher, which then also stops it when the call ends."""
        import settings
        with self._lock:
            if self.recorder.recording:
                return
            saved = settings.get_all()
            try:
                self.recorder.start(include_computer_audio=saved.get("live_record_computer_audio", True),
                                    live_transcription=saved.get("live_transcription", True),
                                    mic_device=saved.get("live_mic_device", ""),
                                    output_device=saved.get("live_output_device", ""))
            except Exception as e:
                self._fire("failed", str(e))
                return
            self.auto_started = auto
        self.mic_state, self.call = "checking", ""
        self._fire("started", (f"{reason} - " if reason else "") + self.recorder.sources_description +
                   (", transcribing live" if self.recorder.transcriber else ""))
        threading.Thread(target=self._monitor_mic, args=(self.recorder.started_monotonic,), daemon=True).start()

    def _monitor_mic(self, started, first_check_s: float = 3.0, every_s: float = 2.0):
        """A muted mic delivers exact digital zeros, so there's simply nothing of the user in the recording.
        During a call that's fine (the meeting audio is still recorded) -- no warning. With no call running
        (an in-person conversation) the user is asked to unmute; Clicky never unmutes anything itself."""
        import meeting_detector
        warned = False
        time.sleep(first_check_s)
        while self.recorder.recording and self.recorder.started_monotonic == started:
            peak = self.recorder.take_mic_peak()
            if peak is None:                        # no microphone source at all
                return
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
                        self._fire("warning", "Your microphone is muted, so your voice isn't being recorded. "
                                              "Unmute it (headset mute button or boom arm) to record this "
                                              "conversation - Clicky won't unmute it for you.")
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
            started_at = self.recorder.started_at
            self._fire("stopping")
            wav_bytes, segments = self.recorder.stop()
        if wav_bytes is None:
            self._fire("discarded", "That was under a second, so nothing was saved.")
            return
        threading.Thread(target=self._save, args=(wav_bytes, started_at, segments), daemon=True).start()

    def _save(self, wav_bytes, started_at, segments):
        try:
            self.last_saved = ingest(wav_bytes, started_at, segments)
            self._fire("saved", self.last_saved)
        except Exception as e:
            self._fire("failed", f"Couldn't save the recording: {e}")

    def status(self) -> dict:
        """For the dashboard's live panel (app.py /api/live)."""
        import settings
        rec = self.recorder
        out = {"recording": rec.recording, "last_saved": self.last_saved,
               "auto_record": bool(settings.get_all().get("auto_record_calls", False)),
               "auto_started": rec.recording and self.auto_started}
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


class CapsLockHoldWatcher(threading.Thread):
    """Calls on_hold() each time Caps Lock is held for hold_seconds, then restores the Caps Lock
    state the hold flipped. Polls GetAsyncKeyState for this one key only (see module docstring)."""

    def __init__(self, on_hold, hold_seconds: float = 1.0, poll_seconds: float = 0.05):
        super().__init__(daemon=True, name="capslock-hold")
        self._on_hold, self._hold, self._poll = on_hold, hold_seconds, poll_seconds
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        user32 = ctypes.windll.user32
        down_since, fired, toggle_before = None, False, None
        toggle_while_up = user32.GetKeyState(VK_CAPITAL) & 1
        ignore_until = 0.0
        enabled, enabled_checked = True, 0.0
        while not self._stop_event.wait(self._poll):
            now = time.monotonic()
            if now - enabled_checked > 1.0:            # the Settings switch applies without a restart
                import settings
                enabled, enabled_checked = settings.get_all().get("capslock_record_hotkey", True), now
            if not enabled:
                down_since = None
                continue
            is_down = bool(user32.GetAsyncKeyState(VK_CAPITAL) & 0x8000)
            if now < ignore_until:
                continue
            if is_down:
                if down_since is None:
                    down_since, fired, toggle_before = now, False, toggle_while_up
                elif not fired and now - down_since >= self._hold:
                    fired = True
                    # off the polling thread: stopping waits for the live transcriber to catch up
                    threading.Thread(target=self._safe_hold, daemon=True).start()
            else:
                if down_since is not None and fired and (user32.GetKeyState(VK_CAPITAL) & 1) != toggle_before:
                    _tap_caps_lock()
                    ignore_until = time.monotonic() + 0.3   # don't treat our own restoring tap as a press
                down_since = None
                toggle_while_up = user32.GetKeyState(VK_CAPITAL) & 1

    def _safe_hold(self):
        try:
            self._on_hold()
        except Exception:
            log.exception("Caps Lock hold action failed")


def _tap_caps_lock():
    KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x1, 0x2
    user32 = ctypes.windll.user32
    user32.keybd_event(VK_CAPITAL, 0x3A, KEYEVENTF_EXTENDEDKEY, 0)
    user32.keybd_event(VK_CAPITAL, 0x3A, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
