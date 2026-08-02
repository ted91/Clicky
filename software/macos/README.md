# pipeline

Phase 4 of the transcription device project: pulls WAV recordings off the
ESP32 device, transcribes and summarizes them, and shows the result on a
local, password-protected webpage. Runs entirely on your own machine — the
only thing that leaves it is the audio/transcript sent to whichever cloud
STT/LLM provider you pick during setup (or nothing at all, on the
local/Ollama path).

## Install & run

Just want the app, not to run from source? Download the packaged build from
[GitHub Releases](https://github.com/ted91/Clicky/releases/latest) and see
[INSTALL.md](INSTALL.md) for the DMG install steps.

To run from source instead:

```
cd pipeline
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open `http://localhost:8000` — first run redirects you to a one-time setup
page (below). No `.env` editing required for normal use; everything's
configured from the browser and saved to `pipeline/settings.json`
(gitignored).

## First-run setup (`/setup`)

- **Password** — protects this page. Required on first run; leave blank on
  later visits to `/setup` to keep the existing one.
- **Transcription & summarization providers** — independent choices, so you
  can mix, e.g. local transcription with a cloud LLM:

  | Provider   | STT (transcription) | LLM (summarization) | Needs |
  |------------|----------------------|----------------------|-------|
  | `mistral`  | Voxtral (~$0.003/min)| Mistral chat | API key |
  | `openai`   | Whisper API | GPT | API key |
  | `anthropic`| — (not available) | Claude | API key |
  | `local`    | faster-whisper (your CPU) | Ollama (your machine) | Ollama installed + a model pulled |

  Whichever provider(s) you pick, install that SDK first (not bundled by
  default — see the commented-out lines in `requirements.txt`):
  ```
  pip install mistralai          # provider: mistral
  pip install openai             # provider: openai
  pip install anthropic          # provider: anthropic
  pip install faster-whisper ollama   # provider: local
  ```
  For the local path, also install [Ollama](https://ollama.com) and pull a
  model: `ollama pull llama3`.

- **Sync transport** — how the pipeline reaches the device:
  - **WiFi** (default) — HTTP to the device's own web server. Simple and
    fast, but requires the device to join your WiFi network as a station,
    which the ESP32-S3 can only do over **2.4GHz**. Many home routers
    broadcast a single combined dual-band SSID and won't let the device
    negotiate the 2.4GHz half cleanly, in which case this silently never
    connects (check Serial Monitor for `wifi_sync: still connecting...`
    messages — see `epaper_transcriber/wifi_sync.cpp`).
  - **BLE** — Bluetooth to a GATT service the device also advertises
    (`epaper_transcriber/ble_sync.cpp`), regardless of your router. Doesn't
    require your Mac to join any network, so background polling never
    disrupts your Mac's own internet connection. Lower throughput than
    WiFi — fine for short voice memos. Needs `pip install bleak` and macOS
    Bluetooth permission granted to your terminal/Python (System Settings →
    Privacy & Security → Bluetooth). Keeps **one persistent connection**
    open for the pipeline's whole lifetime (see `ble_device_client.py`) —
    it only reconnects if the link actually drops (device power-cycled, out
    of range), not on every poll. The on-device "BLE" indicator (bottom of
    the e-paper screen, or Serial Monitor's `ble_sync: central connected` /
    `disconnected` lines) should stay solid/connected continuously, not
    flicker every few seconds.

## Pairing a device (`/pair`, BLE only)

Each device advertises as `EpaperTranscriber-XXXX` (MAC-suffixed) so
multiple units can be told apart. If only one is in range, sync works
without ever visiting `/pair`. If you own (or are near) more than one,
visit `/pair` to scan and pick which one this pipeline instance should
always connect to — saved so future syncs skip the ambiguity.

## Sync vs. processing — two independent phases

A recording shows up on the dashboard **as soon as its audio is downloaded
from the device** — tagged "Processing…" — well before transcription or
summarization happens. This matters because BLE/WiFi fetch and LLM calls
are two very different failure domains: if your API key is wrong or a
provider call fails, the recording still shows up with a "Processing
failed" badge, the error message, and an audio player, and *only the LLM
step* retries on the next poll cycle — the audio is never re-fetched from
the device. See `poller.py`'s `sync_once()` (device → disk) and
`process_once()` (disk → transcript/summary) for the two phases.

Sync itself is deduped by `(name, size)` from the device's own `/list`
response, checked **before** downloading — not by content hash after the
fact — since the RAM fallback recording keeps reporting the same filename
forever until overwritten, and re-fetching an unchanged file over BLE every
poll cycle would be exactly the "keeps re-transferring the same file"
symptom. See `storage.is_known_by_size()`.

**Two different retry cadences, deliberately.** `POLL_INTERVAL_SECONDS`
(default 3s) controls how often the device is checked for new
recordings — kept short since BLE reuses one open connection, so a check
is just a cheap characteristic read, not a fresh scan+connect. A freshly
synced recording is always processed immediately regardless of this value.
But if processing *fails* (bad API key, rate limit), retrying that specific
recording is throttled separately via `PROCESS_RETRY_INTERVAL_SECONDS`
(default 30s) — otherwise a bad API key would get hammered every few
seconds. See `poller.py`'s `process_once()`.

## Speaker separation

Mistral's Voxtral transcription supports real diarization (`diarize=True`
in the API call) — each transcript segment comes back tagged with a
`speaker_id`, shown on the dashboard as "Speaker SPEAKER_00: ...", etc.,
and fed into the summarization prompt so action items/stakeholders/
follow-ups can be attributed to the right speaker where the transcript
makes that clear. This is only available with `STT_PROVIDER=mistral` —
OpenAI's Whisper API and the local faster-whisper path don't do speaker
separation, so those fall back to a single undifferentiated transcript.

## Notion & Obsidian (`/integrations`)

Every successfully processed recording is automatically pushed to whichever
of these you've configured — independent of transcription itself, so a
Notion outage doesn't block new recordings from being processed, and vice
versa. Both are optional; leave a destination blank to skip it. Mistral (or
whichever LLM you picked) does all the structuring — action items, owners,
follow-ups, stakeholders — before anything gets sent onward; neither Notion
nor Obsidian do any of that work themselves.

- **Notion** — needs an internal integration token (create one at
  [notion.so/my-integrations](https://www.notion.so/my-integrations)) and a
  database ID, both entered at `/integrations`. Share your target database
  with the integration first (••• menu → Connections), or the push will
  fail with a permissions error. Each recording becomes a new page: title +
  a `Summary`/`Action items`/`Follow-ups`/`Stakeholders`/`Transcript`
  section per page. See `notion_sync.py`.

  **Notion Tasks (optional, separate database)** — a plain content section
  on a recording's page isn't visible to Notion's own Calendar or "My
  Tasks" views, since those only read real database *properties* (`Date`,
  `Person`), not page content. If you want actual per-task Calendar/My
  Tasks integration, set up a second database with `Name` (title), `Due
  Date` (date), `Owner` (person), and `Done` (checkbox) columns, share it
  with the integration, and enter its ID at `/integrations` too. Each
  action item becomes its own page there — not bundled into the recording's
  page — since each item can have its own distinct due date and owner.
  Owner names extracted from the transcript are matched (case-insensitive,
  exact or partial) against your Notion workspace's real members, which
  requires enabling **"Read user information"** under the integration's
  Capabilities; an unmatched name is appended to the task title instead of
  guessed at. See `notion_sync.py`'s `push_tasks()`.
- **Obsidian** — no account or API needed. Just point `/integrations` at
  your vault's folder path on disk; each recording is written as a
  markdown file with YAML frontmatter (created date, providers used) and
  the same section structure as Notion. Obsidian picks it up automatically
  since it just watches the vault folder. See `obsidian_sync.py`.

Calendar invites and email are deliberately **not** built here — those
require sending something on your behalf, which always needs your explicit
confirmation per send, not silent automation. That's a separate, bigger
piece of work (OAuth, draft-then-confirm UI) if you want it later.

## Meeting auto-recording (macOS only)

The menu-bar agent (`meetingcap`, see `meeting_recorder.py`) can start recording
without a manual click in two ways:

- **Calendar-based**: a Google/Apple Calendar event with a Meet/Teams link
  auto-starts recording the moment it begins, and auto-stops at its end time.
  See `poller.check_meeting_auto_start_once()`.
- **Ad-hoc (no calendar entry)**: for an instantaneous call — someone just
  starts a Teams/Zoom/Slack-huddle call, or opens a real Google Meet link in
  a browser — `meetingcap` periodically checks which processes are actively
  using the microphone (a first-party CoreAudio API, macOS 14.4+ only;
  silently unavailable on older systems) and starts recording if it's a known
  conferencing app, or a browser with a genuine in-call Meet URL open in some
  tab. There's no known end time for this case, so you stop it manually (menu
  bar). See `meetingcap/main.swift`'s `checkForAdhocMeeting`.
  - Checking a browser's open tabs needs **Automation** permission (System
    Settings → Privacy & Security → Automation) — a one-time prompt per
    browser, only triggered the first time it actually tries (a native app
    like Teams/Zoom never needs this).
  - Known limitation: Google Meet's pre-join lobby uses the same URL as an
    actual in-call tab, so opening a Meet link without joining can still
    trigger a false-positive recording — not fully solvable from the URL
    alone.
  - Not covered: any conferencing app/browser not on the known list, or a
    call in an incognito/private browser window (Automation can't see into
    those by browser design).

## Notes

- Downloaded audio is saved to `pipeline/audio/<hash>.wav` (gitignored) and
  playable directly from the dashboard — useful for confirming a recording
  came through even before (or if) transcription succeeds.
- Recordings and settings persist in `pipeline/recordings.json` and
  `pipeline/settings.json` (both gitignored) — restarting the pipeline
  loses neither history nor configuration.
- The pipeline never deletes recordings from the device — SD-card files are
  cheap to keep, and the RAM fallback just gets overwritten by the next
  recording anyway.
- `device_client.py` (WiFi) and `ble_device_client.py` (BLE) expose the
  same `list_recordings()`/`download_recording(name)` shape — `poller.py`
  picks one per poll based on the current `sync_transport` setting, so
  changing it in `/setup` takes effect without a restart.
- For local development without the web setup flow, `.env.example` still
  works as a fallback default (`cp .env.example .env`) — anything saved via
  `/setup` takes priority over it at runtime.
