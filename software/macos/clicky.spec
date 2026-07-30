# PyInstaller spec for the Clicky pipeline .app.
# Build with: ./meetingcap/build.sh && .venv/bin/pyinstaller clicky.spec --noconfirm
#
# Unsigned build -- macOS Gatekeeper will block it for anyone but you.
# First launch needs right-click -> Open (or `xattr -dr com.apple.quarantine
# Clicky.app` on the built app) until this is code-signed and notarized
# with an Apple Developer account.

a = Analysis(
    ['main_packaged.py'],
    pathex=[],
    # meetingcap is a separate compiled Swift binary (menu-bar agent for
    # meeting audio capture, see meetingcap/main.swift) -- must be built
    # first via meetingcap/build.sh. Landing it at the bundle root (not
    # nested) matches meeting_recorder.helper_path()'s frozen-mode lookup
    # of sys._MEIPASS/meetingcap.
    # librnnoise.dylib: vendored RNNoise shared library for background
    # noise suppression on recorded audio (see noise_reduction.py and
    # THIRD_PARTY_NOTICES.md) -- landed at the bundle root to match that
    # module's frozen-mode lookup of sys._MEIPASS/librnnoise.dylib, same
    # convention as meetingcap below.
    binaries=[('meetingcap/meetingcap', '.'), ('librnnoise.dylib', '.')],
    datas=[
        ('templates', 'templates'),
        ('static', 'static'),
        ('firmware', 'firmware'),
        # Carries GOOGLE_CLIENT_ID/SECRET (the app's shared OAuth client --
        # see config.py's comment) into the packaged build. Landed at the
        # bundle root to match config.py's frozen-mode lookup of
        # sys._MEIPASS/.env. Provider API keys (Mistral/OpenAI/etc.) don't
        # need this -- those are entered per-user via /setup and persisted
        # to settings.json, never read from .env at packaged runtime.
        ('.env', '.'),
    ],
    hiddenimports=[
        # Dynamically imported inside functions (providers/*.py's _client(),
        # providers/__init__.py's _load()) rather than at module top-level --
        # PyInstaller's static analysis usually still catches these, but
        # listed explicitly since a missed one fails silently at runtime
        # (ImportError only surfaces when that provider is actually selected).
        'mistralai',
        'mistralai.client',
        'bleak',
        'yaml',  # obsidian_sync.py's frontmatter read/write
        # voice_id.py's speaker-embedding model -- torch/speechbrain's own
        # dynamic import patterns are extensive; this list is a starting
        # point, not guaranteed complete -- expect to add entries after a
        # real build surfaces a missing-module error the first time voice
        # ID actually runs (import failures here are silent until then).
        'speechbrain',
        'torch',
        'torchaudio',
        'uvicorn.loops.auto',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan.on',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Clicky',
    debug=False,
    strip=False,
    upx=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='Clicky',
)

app = BUNDLE(
    coll,
    name='Clicky.app',
    icon=None,
    bundle_identifier='com.clicky.pipeline',
    info_plist={
        'NSBluetoothAlwaysUsageDescription': 'Clicky uses Bluetooth to sync recordings from your Clicky device.',
        'NSBluetoothPeripheralUsageDescription': 'Clicky uses Bluetooth to sync recordings from your Clicky device.',
        # meetingcap (bundled Swift agent) captures system audio via
        # ScreenCaptureKit -- no Info.plist key exists for that permission,
        # macOS prompts for Screen Recording automatically. Mic access does
        # need a usage string.
        'NSMicrophoneUsageDescription': 'Clicky uses your microphone to record your side of meetings you choose to capture.',
        # Apple Calendar (EventKit) as an optional second calendar source
        # alongside Google Calendar -- see meetingcap/main.swift's
        # CalendarLookup / apple_calendar.py. Only prompted the first time
        # a calendar lookup actually runs.
        'NSCalendarsUsageDescription': 'Clicky reads your calendar to detect upcoming meetings for auto-recording and pre-meeting prep notes.',
        'NSCalendarsFullAccessUsageDescription': 'Clicky reads your calendar to detect upcoming meetings for auto-recording and pre-meeting prep notes.',
        # Ad-hoc (non-calendar) meeting detection -- see meetingcap/main.swift's
        # checkForAdhocMeeting/openTabURLs. Only asked the first time it
        # actually tries to read a browser's tabs (a known conferencing app
        # alone, e.g. Teams/Zoom/Slack, never needs this -- only checking a
        # browser's open tabs for a Meet call URL does). NOTE (untested):
        # since meetingcap ships as a bare binary inside Clicky.app rather
        # than its own .app bundle, it's not fully confirmed this prompt
        # attributes correctly to Clicky.app rather than behaving
        # differently for a bundled helper -- worth confirming on first
        # real run.
        # Also covers Jarvis's voice-command actions (calendar_event/
        # reminder/email_draft/qa-with-named-app): AppleScript automation of
        # Calendar.app/Reminders.app/Mail.app/System Events, gated by this
        # same Apple Events consent, prompted per target app the first time
        # each is actually driven -- see jarvis.py's _osascript/
        # _dispatch_gui_automation.
        'NSAppleEventsUsageDescription': "Clicky checks your browser's open tabs for an active Google Meet call, to auto-record ad-hoc meetings that have no calendar entry, and drives Calendar/Reminders/Mail/other apps to carry out your Jarvis voice commands.",
        'CFBundleShortVersionString': '0.5.0',
        'LSUIElement': False,
    },
)
