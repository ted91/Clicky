"""Controls the meetingcap Swift menu-bar agent (see meetingcap/main.swift)
-- a persistent process, launched once at Clicky startup and left running
for the app's lifetime, showing a menu-bar icon the user can click to
start/stop recording manually. Clicky's own poller can also drive it (e.g.
Phase B's calendar auto-detect) by sending the same "start" command.

Either way, a finished recording is handed to storage.add_pending() so it
flows through the exact same transcribe/summarize/Notion pipeline as
device memos. The dashboard is deliberately NOT a recording control surface
-- per design, it's for configuration and viewing only; the menu-bar icon
is the only place a user starts/stops a meeting recording by hand.

IPC: newline-delimited JSON over the agent's stdin/stdout, established once
at start() and read continuously in a background thread for the lifetime
of the process (see _read_loop).
"""
import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

import paths
import storage

log = logging.getLogger("meeting_recorder")

_lock = threading.Lock()
_agent = {
    "proc": None,           # the persistent meetingcap subprocess, or None if not launched/dead
    "ready": False,
    "recording": False,
    "started_at": None,     # time.time() when the current recording began
    "error": None,          # last error message, shown on the dashboard
    "last_result": None,    # the most recent {"ok":..., "record":...} from a completed recording
}

_READY_TIMEOUT_SECONDS = 10
_START_TIMEOUT_SECONDS = 15  # SCShareableContent + first audio buffer can be slow on first TCC grant
_STOP_TIMEOUT_SECONDS = 10

# Bounded auto-relaunch if the agent crashes mid-session (confirmed to
# happen in the wild, not just theoretical -- see the "process exited"
# warning without a preceding "user_quit" event). Without this, a crash
# permanently loses the menu-bar icon and all recording capability for the
# rest of the session, with no visible explanation. Bounded (not infinite)
# so a genuinely broken agent (e.g. missing binary, corrupted install)
# doesn't retry-loop forever burning CPU/log spam -- after
# _MAX_RELAUNCH_ATTEMPTS within the backoff window, it gives up and leaves
# _agent["error"] set for the dashboard to show. The counter resets once
# the agent reports "ready" again, so a single transient crash doesn't
# permanently use up the retry budget for a later, unrelated one.
_MAX_RELAUNCH_ATTEMPTS = 3
_RELAUNCH_BACKOFF_SECONDS = 5
_relaunch_attempts = 0
_shutting_down = False  # set by shutdown() so a deliberate quit doesn't trigger a relaunch

_pending_meeting = None  # calendar metadata to attach to the *next* recording that starts (Phase B)

# Request/response bridge for the agent's EventKit calendar query (see
# apple_calendar.py) -- correlated by a request id since it's a genuine
# round-trip over the same fire-and-forget stdin/stdout channel everything
# else here uses one-way.
_calendar_lock = threading.Lock()
_calendar_pending = {}   # request_id -> threading.Event
_calendar_results = {}   # request_id -> event dict or None


def helper_path():
    """Resolves the meetingcap binary: bundled next to the executable in a
    PyInstaller build, else the dev checkout's meetingcap/ dir. None if
    missing (not built yet / not bundled)."""
    if getattr(sys, "frozen", False):
        candidate = os.path.join(sys._MEIPASS, "meetingcap")
    else:
        candidate = os.path.join(os.path.dirname(os.path.abspath(__file__)), "meetingcap", "meetingcap")
    return candidate if os.path.isfile(candidate) and os.access(candidate, os.X_OK) else None


def _is_valid_wav(data: bytes) -> bool:
    return len(data) >= 44 and data[0:4] == b"RIFF" and data[8:12] == b"WAVE"


def _repair_truncated_wav_header(data: bytes) -> bytes:
    """WavWriter (main.swift) only patches the real RIFF/data chunk sizes
    into the header on a clean close() -- if the agent dies mid-recording
    (crash, kill -9, or previously: SIGPIPE from the parent dying first, see
    the signal(SIGPIPE, SIG_IGN) comment in main.swift), the file left on
    disk still has the placeholder header written at open() time, which
    claims 0 bytes of audio data even though the real PCM bytes were
    appended throughout recording and are physically present after the
    44-byte header. Confirmed live on a real orphaned file: RIFF/data sizes
    both read 0 despite the file being 13MB. Rewrites those two fields from
    the actual file size -- a no-op if the header is already correct."""
    if len(data) <= 44:
        return data
    data = bytearray(data)
    real_data_bytes = len(data) - 44
    data[4:8] = (36 + real_data_bytes).to_bytes(4, "little")   # RIFF chunk size
    data[40:44] = real_data_bytes.to_bytes(4, "little")         # data chunk size
    return bytes(data)


def _recover_orphaned_recording():
    """Runs once at startup, before the agent is (re)launched -- if
    meeting_in_progress.wav already exists on disk, the previous agent
    process died before ever calling performStop() (which is the only
    place that renames/hands off the file), so nothing ever fed it into
    _ingest(). Repairs the header (see _repair_truncated_wav_header) and
    ingests it rather than leaving it silently orphaned forever -- there was
    no recovery path for this at all before, confirmed via a real leftover
    file from an actual crash."""
    path = os.path.join(paths.APP_DATA_DIR, "meeting_in_progress.wav")
    if not os.path.exists(path):
        return
    log.warning("found leftover %s from a previous session that didn't shut down cleanly -- attempting recovery", path)
    try:
        with open(path, "rb") as f:
            wav_bytes = f.read()
    except OSError as e:
        log.error("could not read orphaned recording %s: %s", path, e)
        return
    wav_bytes = _repair_truncated_wav_header(wav_bytes)
    with open(path, "wb") as f:
        f.write(wav_bytes)
    _ingest(path, meeting=None)


def launch():
    """Starts the persistent menu-bar agent. Called once from app.py's
    lifespan startup. A missing/failed launch is non-fatal -- the dashboard
    will just show meeting recording as unavailable (state()'s "error")."""
    _recover_orphaned_recording()
    helper = helper_path()
    if helper is None:
        with _lock:
            _agent["error"] = ("Meeting capture agent not found. In dev mode run ./meetingcap/build.sh; "
                                "in a packaged build this is a packaging bug.")
        log.warning(_agent["error"])
        return

    env = dict(os.environ)
    env["CLICKY_DATA_DIR"] = paths.APP_DATA_DIR
    try:
        proc = subprocess.Popen(
            [helper],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,  # line-buffered
            env=env,
        )
    except OSError as e:
        with _lock:
            _agent["error"] = f"failed to launch meeting capture agent: {e}"
        log.error(_agent["error"])
        return

    with _lock:
        _agent["proc"] = proc
        _agent["error"] = None

    threading.Thread(target=_read_loop, args=(proc,), daemon=True).start()

    # Wait briefly for "ready" so an immediate first click doesn't race a
    # cold-started process.
    deadline = time.time() + _READY_TIMEOUT_SECONDS
    while time.time() < deadline:
        with _lock:
            if _agent["ready"]:
                log.info("meeting capture agent ready")
                return
        time.sleep(0.1)
    log.warning("meeting capture agent didn't report ready within %ds", _READY_TIMEOUT_SECONDS)


def _read_loop(proc):
    """Continuously reads the agent's stdout JSON-line events and updates
    module state -- runs for the lifetime of the agent process, since it's
    persistent (unlike the old one-shot-CLI design where a single blocking
    read sufficed)."""
    global _pending_meeting
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            _handle_event(event)
    except (ValueError, OSError):
        pass
    finally:
        with _lock:
            if _agent["proc"] is proc:
                _agent["proc"] = None
                _agent["ready"] = False
                _agent["recording"] = False
                if _agent["error"] is None:
                    _agent["error"] = "Meeting capture agent exited unexpectedly."
        log.warning("meeting capture agent process exited")
        if not _shutting_down:
            threading.Thread(target=_attempt_relaunch, daemon=True).start()


def _attempt_relaunch():
    """Runs off a fresh thread (never the dying _read_loop thread itself)
    after an unexpected agent exit -- waits a beat, then retries launch(),
    up to _MAX_RELAUNCH_ATTEMPTS. See the module-level comment on those
    constants for why this is bounded rather than unconditional."""
    global _relaunch_attempts
    if _shutting_down:
        return
    _relaunch_attempts += 1
    if _relaunch_attempts > _MAX_RELAUNCH_ATTEMPTS:
        log.error("meeting capture agent crashed %d times -- giving up on auto-relaunch; "
                   "restart Clicky manually to try again", _relaunch_attempts - 1)
        return
    log.info("attempting to relaunch meeting capture agent (attempt %d/%d) in %ds",
              _relaunch_attempts, _MAX_RELAUNCH_ATTEMPTS, _RELAUNCH_BACKOFF_SECONDS)
    time.sleep(_RELAUNCH_BACKOFF_SECONDS)
    if _shutting_down:
        return
    launch()


def _handle_event(event: dict):
    global _pending_meeting
    kind = event.get("event")

    if kind == "ready":
        global _relaunch_attempts
        with _lock:
            _agent["ready"] = True
        _relaunch_attempts = 0  # a stable agent resets the retry budget for any future, unrelated crash

    elif kind == "recording_started":
        with _lock:
            _agent["recording"] = True
            _agent["started_at"] = time.time()
            _agent["error"] = None
        # Attach calendar context even for a manually-triggered recording
        # (menu-bar click) -- start(meeting=...) already set _pending_meeting
        # for the auto-detect path, but a manual click bypasses start()
        # entirely (the Swift agent handles its own menu clicks), so this is
        # the only place that path gets calendar metadata at all.
        #
        # Run on a separate thread, NOT inline here: _handle_event runs on
        # the same thread that reads the agent's stdout (_read_loop), and
        # the Apple Calendar fallback below (get_apple_calendar_event) is
        # itself a request/response round-trip over that exact channel --
        # calling it synchronously here would deadlock this thread waiting
        # on a response only this same thread could ever deliver.
        if _pending_meeting is None:
            threading.Thread(target=_lookup_meeting_for_manual_start, daemon=True).start()

    elif kind == "adhoc_meeting_detected":
        # An instantaneous/non-calendar meeting (see main.swift's
        # checkForAdhocMeeting) -- a known conferencing app or a browser
        # with a real Meet call URL is actively using the microphone.
        # Synthesize a minimal meeting dict (no real calendar event exists
        # for this by definition) and start recording the same way
        # poller.check_meeting_auto_start_once() does for a calendar
        # match. `end: None` deliberately means that function's own
        # auto-stop-at-event-end logic never applies here -- there's no
        # known end time, so the user stops it manually (menu bar), same
        # as any other manually-started recording.
        #
        # Must run on a separate thread, NOT inline here -- start() blocks
        # waiting for a "recording_started" event, which can only ever
        # arrive back over the same stdout stream this handler itself is
        # reading on; calling it synchronously would deadlock (same
        # reasoning as _lookup_meeting_for_manual_start above).
        with _lock:
            already_recording = _agent["recording"]
        if not already_recording:
            app_label = event.get("app") or ""
            meeting_url = event.get("meeting_url")
            synthetic_meeting = {
                "title": f"Ad-hoc meeting ({app_label})" if app_label else "Ad-hoc meeting",
                "start": datetime.now(timezone.utc).isoformat(),
                "end": None,
                "meeting_url": meeting_url,
                "attendees": [],
            }
            threading.Thread(target=start, args=(synthetic_meeting,), daemon=True).start()

    elif kind == "recording_stopped":
        wav_path = event.get("path")
        discarded = event.get("discarded", False)
        meeting = _pending_meeting
        _pending_meeting = None
        with _lock:
            _agent["recording"] = False
            _agent["started_at"] = None
        if not discarded and wav_path:
            _ingest(wav_path, meeting)

    elif kind == "error":
        with _lock:
            _agent["recording"] = False
            _agent["started_at"] = None
            _agent["error"] = event.get("message", "unknown capture error")
        log.error("meeting capture error [%s]: %s", event.get("code"), _agent["error"])

    elif kind == "calendar_event":
        req_id = event.get("id")
        with _calendar_lock:
            if req_id in _calendar_pending:
                _calendar_results[req_id] = event.get("data")
                _calendar_pending.pop(req_id).set()

    elif kind == "user_quit":
        # The user picked Quit from the menu bar -- without this, only the
        # Swift agent process exited; the actual Python/uvicorn server (and
        # everything it's doing: polling, recording, Notion/Google syncs)
        # kept running invisibly forever, since there's no Dock icon, no
        # window, and now no menu-bar icon either to reveal it's still
        # alive. os._exit (not sys.exit -- this runs on a background
        # thread, not the main one) terminates the whole process tree
        # immediately; storage/settings writes are already atomic
        # (tmp-file + os.replace) so an abrupt exit mid-write can't corrupt
        # them.
        log.info("user quit Clicky from the menu bar -- shutting down the whole app")
        os._exit(0)

    # "user_clicked_start" / "user_clicked_stop" / "status" carry no state
    # changes beyond what "recording_started"/"recording_stopped" already do.


def _lookup_meeting_for_manual_start():
    """Runs off the _read_loop thread (see its call site's comment) --
    tries Google Calendar first, then Apple Calendar, and only commits the
    result if the recording is still in progress and nothing else already
    set _pending_meeting in the meantime (a very short recording could
    already have stopped, and stopped, by the time this finishes; writing
    stale meeting metadata into a *later* recording would be worse than
    just not having it here)."""
    global _pending_meeting
    try:
        import google_client
        event = google_client.current_or_next_event()
        if event is None:
            import apple_calendar
            event = apple_calendar.current_or_next_event()
        with _lock:
            still_recording = _agent["recording"]
        if event and still_recording and _pending_meeting is None:
            _pending_meeting = event
    except Exception as e:
        log.warning("calendar lookup failed at recording start: %s", e)


def _ingest(wav_path: str, meeting: dict = None):
    """Reads the finished recording off disk and hands it to the existing
    ingestion path (storage.add_pending) -- same as a device sync."""
    try:
        with open(wav_path, "rb") as f:
            wav_bytes = f.read()
    except OSError as e:
        log.error("could not read meeting recording %s: %s", wav_path, e)
        with _lock:
            _agent["error"] = f"could not read recording file: {e}"
        return

    if not _is_valid_wav(wav_bytes) or len(wav_bytes) <= 44:
        log.warning("meeting recording %s is empty/corrupt, discarding", wav_path)
        with _lock:
            _agent["error"] = "The recording was empty or corrupt -- capture may have failed mid-recording."
        return

    content_hash = hashlib.md5(wav_bytes).hexdigest()
    # Timestamped name: never collides with the device's ram_recording.wav,
    # and uniqueness sidesteps storage.is_known_by_size's name+size dedupe.
    name = datetime.now().strftime("meeting-%Y-%m-%d-%H%M%S.wav")
    record = storage.add_pending(name, len(wav_bytes), content_hash, wav_bytes)

    if meeting:
        storage.set_meeting(content_hash, meeting)

    try:
        os.remove(wav_path)
    except OSError:
        pass

    with _lock:
        _agent["last_result"] = {"name": name, "content_hash": content_hash, "size": len(wav_bytes)}
    log.info("meeting recording saved: %s (%d bytes)", name, len(wav_bytes))


def _send(cmd: dict):
    with _lock:
        proc = _agent["proc"]
    if proc is None or proc.poll() is not None:
        return False
    try:
        proc.stdin.write(json.dumps(cmd) + "\n")
        proc.stdin.flush()
        return True
    except (BrokenPipeError, OSError):
        return False


def state() -> dict:
    with _lock:
        return {
            "available": _agent["proc"] is not None and _agent["ready"],
            "recording": _agent["recording"],
            "started_at": _agent["started_at"],
            "elapsed_sec": int(time.time() - _agent["started_at"]) if _agent["recording"] and _agent["started_at"] else 0,
            "error": _agent["error"],
        }


def start(meeting: dict = None) -> dict:
    """Requests a recording start (manual dashboard-adjacent call, or
    Phase B's calendar auto-detect). The menu-bar icon click path doesn't
    go through this -- the Swift agent handles that itself and just emits
    events. `meeting`, when given, is attached to the resulting recording
    once it finishes."""
    global _pending_meeting
    with _lock:
        if _agent["proc"] is None or not _agent["ready"]:
            return {"error": "agent_unavailable", "detail": _agent["error"] or "Meeting capture agent is not running."}
        if _agent["recording"]:
            return {"error": "already_recording", "detail": "A meeting recording is already in progress."}
        _agent["error"] = None

    _pending_meeting = meeting
    if not _send({"cmd": "start", "meeting": meeting}):
        return {"error": "send_failed", "detail": "Could not reach the meeting capture agent."}

    deadline = time.time() + _START_TIMEOUT_SECONDS
    while time.time() < deadline:
        with _lock:
            if _agent["recording"]:
                return {"ok": True}
            if _agent["error"]:
                return {"error": "capture_error", "detail": _agent["error"]}
        time.sleep(0.1)
    return {"error": "start_timeout", "detail": "Recording didn't start within 15s -- check Screen Recording and Microphone permissions in System Settings > Privacy & Security."}


def stop() -> dict:
    with _lock:
        if not _agent["recording"]:
            return {"error": "not_recording", "detail": "No meeting recording in progress."}

    if not _send({"cmd": "stop"}):
        return {"error": "send_failed", "detail": "Could not reach the meeting capture agent."}

    deadline = time.time() + _STOP_TIMEOUT_SECONDS
    while time.time() < deadline:
        with _lock:
            if not _agent["recording"]:
                return {"ok": True, "record": _agent["last_result"]}
        time.sleep(0.1)
    return {"error": "stop_timeout", "detail": "Agent didn't confirm stop in time."}


def get_apple_calendar_event(window_min: int = 15, timeout: float = 5.0):
    """Synchronous round-trip to the agent's EventKit query (see
    meetingcap/main.swift's handling of "get_event") -- the only
    request/response pair over this otherwise fire-and-forget channel,
    correlated by a request id since it's a genuine ask-and-wait rather
    than a one-way command. Returns the same shape as
    google_client.current_or_next_event() ({title, start, end,
    meeting_url, attendees} or None) so poller.py can treat both calendar
    sources interchangeably. Returns None (not an error) if the agent
    isn't running, the request times out, or EventKit access hasn't been
    granted -- Apple Calendar is an optional second source, never a hard
    requirement."""
    with _lock:
        if _agent["proc"] is None or not _agent["ready"]:
            return None

    req_id = str(uuid.uuid4())
    done = threading.Event()
    with _calendar_lock:
        _calendar_pending[req_id] = done
    if not _send({"cmd": "get_event", "id": req_id, "window_min": window_min}):
        with _calendar_lock:
            _calendar_pending.pop(req_id, None)
        return None

    if not done.wait(timeout):
        with _calendar_lock:
            _calendar_pending.pop(req_id, None)
        return None
    with _calendar_lock:
        return _calendar_results.pop(req_id, None)


def show_prep_note(title: str, body: str) -> bool:
    """Shows a pre-meeting prep note as a popover anchored under the
    menu-bar icon (see meetingcap/main.swift's PrepPopover) -- used instead
    of a Notification Center banner so longer synthesized context doesn't
    get truncated. Fire-and-forget: returns False only if the agent isn't
    reachable at all (dashboard/OS notification remain unaffected either
    way -- see poller.check_meeting_prep_once)."""
    with _lock:
        if _agent["proc"] is None or not _agent["ready"]:
            return False
    return _send({"cmd": "show_prep", "title": title, "body": body})


def shutdown():
    """Lifespan cleanup -- tells the agent to quit (it finalizes any
    in-progress recording first) rather than leaving it orphaned. Sets
    _shutting_down first so the agent's exit (in response to our own "quit"
    command) doesn't trigger the crash-recovery auto-relaunch -- this exit
    is expected, not a crash."""
    global _shutting_down
    _shutting_down = True
    _send({"cmd": "quit"})
    with _lock:
        proc = _agent["proc"]
    if proc is not None:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
