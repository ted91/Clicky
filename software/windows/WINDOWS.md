# Building and running Clicky on Windows

The pipeline itself (dashboard, WiFi/BLE sync, transcription, Notion/
Obsidian/Google sync, AI-pager notifications) is plain cross-platform
Python and runs on Windows today. The one thing that doesn't come along:
**meeting audio capture** (`meetingcap`) is a compiled Swift binary using
macOS-only frameworks (ScreenCaptureKit, AVAudioEngine, EventKit) — there's
no Windows build of it, and no equivalent bundled here. Everything else
works the same.

## Important: this has to be built ON Windows

PyInstaller does not cross-compile. `clicky_windows.spec` (in this folder)
cannot be run from macOS/Linux to produce a `Clicky.exe` — it has to be
run on an actual Windows machine (or a Windows CI runner/VM). Running it
elsewhere just produces a binary for whatever OS you ran it on, regardless
of the spec file's name.

## Build steps (run on Windows)

```
cd pipeline
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install bleak pyinstaller
REM plus whichever provider SDK(s) you're using -- see requirements.txt's
REM commented-out lines (mistralai / openai / anthropic / faster-whisper / ollama)

.venv\Scripts\pyinstaller.exe clicky_windows.spec --noconfirm
```

Result: `dist\Clicky\Clicky.exe` plus its supporting files, all in that one
folder (a "onedir" build — Windows has no equivalent to macOS's single
`.app` bundle). Copy/zip the whole `dist\Clicky\` folder to distribute it;
`Clicky.exe` alone won't run without the rest of the folder next to it.

## Live recording (Caps Lock)

**Double-tap Caps Lock** (or use the tray menu, or the dashboard's
"Start recording" button) to start recording; double-tap again to stop. It
records the microphone ("you") and the computer's audio ("others" -- the
far side of a Teams/Zoom/Meet call) and transcribes both **live** with
Parakeet-TDT 0.6B v3 (same engine as Live Subtitles), shown on the
dashboard while you speak. The saved recording keeps the audio and that
transcript, so it isn't transcribed a second time. See `live_recorder.py`
/ `live_transcriber.py`. Recording other people needs their consent --
tell them before you start.

While recording a call on this computer, **double-tap Shift** (or use the
tray / dashboard) to screenshot the meeting window. Screenshots are stamped
with the recording time (same clock as the transcript) and the wall-clock
time, shown on the recording's card, embedded in the Obsidian note at that
moment, and placed in the timestamped transcript the summarizer reads.
If the recorded microphone sends only silence (e.g. a headset dongle with
the headset off), Clicky switches to a microphone that is delivering audio.

Live transcription needs the models once (~650 MB) in
`%USERPROFILE%\.clicky-pipeline\models`: run `python live_transcriber.py`
(or set `live_models_dir` in settings.json to an existing Live Subtitles
`models` folder). Without them, recording still works and the selected
transcription provider runs after you stop. Settings keys (settings.json,
all default on): `live_transcription`, `live_record_computer_audio`,
`capslock_record_hotkey`.

## Installing / running

**Recommended: per-user MSI.** After the PyInstaller build, run
`python build_msi.py --version <x.y.z>` (Python 3.12 -- it uses the stdlib
`msilib`, removed in 3.13). Output: `dist\Clicky-<version>.msi`. It installs
to `%LOCALAPPDATA%\Programs\Clicky` with a small Welcome / progress / Finish
wizard, a Start menu shortcut and an Apps entry -- no admin rights and no
certificate to trust (unlike MSIX). On machines where endpoint protection
locks freshly built unsigned exes after a few minutes, run `build_msi.py`
immediately after PyInstaller.

Note that an unsigned `Clicky.exe` can still be blocked from *running* by a
company application-control policy (AppLocker/WDAC) even though it installs
fine -- that needs the exe code-signed or the install folder allowlisted.

Want an MSIX instead? See **`msix/MSIX.md`** — packages
this same `dist\Clicky\` build into a signed, sideloadable `.msix`. No
Microsoft account or Store review needed, just a free self-signed
certificate you trust once per machine.

Otherwise, run **`Setup Clicky.bat`** (also in this folder) from inside the same
folder as the built `Clicky\` directory — it copies `Clicky\` to
`%LOCALAPPDATA%\Clicky` and launches it, or offers to reinstall/uninstall
if it's already there. Uninstalling never touches your data
(`%USERPROFILE%\.clicky-pipeline\` — recordings, settings, API keys) for
the same reason the macOS installer doesn't: reinstalling shouldn't mean
starting over.

Or just run `Clicky.exe` directly from wherever you put the folder — the
`.bat` is a convenience, not a requirement.

## First run

Same as macOS: this is an **unsigned build**, so Windows SmartScreen will
likely warn ("Windows protected your PC") the first time you run it —
click "More info" → "Run anyway". This is the same category of warning as
macOS Gatekeeper's quarantine flag on the unsigned `.dmg` build; getting
past it durably (no warning on every future launch, ever) would need a
paid code-signing certificate, not set up here.

## What's different from the macOS build

- No meeting audio capture / no menu-bar-equivalent tray icon (see above).
- No Apple Calendar source (`apple_calendar.py`) — it's a thin wrapper
  around the same missing `meetingcap` agent. Google Calendar still works
  fully (OAuth-based, not macOS-specific).
- WiFi network name (SSID) auto-fill on the Settings → Device page uses
  `netsh wlan show interfaces` instead of macOS's `networksetup` — same
  feature, different OS command under the hood (see `app.py`'s
  `_current_mac_wifi_ssid()`).
- BLE sync uses `bleak`'s WinRT backend automatically — no extra
  configuration needed, but note Windows' own Bluetooth stack has its own
  quirks/permission prompts independent of anything in this codebase.
