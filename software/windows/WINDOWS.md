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

## Installing / running

Want a real installer (Start Menu entry, shows up in "Installed apps",
clean uninstall) instead of a raw folder? See **`msix/MSIX.md`** — packages
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
