# PyInstaller spec for a Windows build of the Clicky pipeline.
#
# IMPORTANT: PyInstaller does not cross-compile -- this spec must be run
# ON an actual Windows machine (or a Windows CI runner) to produce a real
# Clicky.exe. Running it on macOS/Linux produces a macOS/Linux binary
# regardless of this file's name; there is no way around that.
#
# Build (from a Windows machine, inside pipeline/, with .venv active and
# `pip install pyinstaller` done):
#   .venv\Scripts\pyinstaller.exe clicky_windows.spec --noconfirm
# Result: dist\Clicky\Clicky.exe (onedir build -- see COLLECT below; a
# folder, not BUNDLE, since BUNDLE/.app is a macOS-only PyInstaller
# concept). Zip the whole dist\Clicky\ folder for distribution, or use
# an installer tool (Inno Setup, etc.) if you want a proper installer --
# neither is set up here, this just gets you a working folder to run or
# zip by hand.
#
# What's intentionally different from clicky.spec (the macOS build):
# - No meetingcap binary bundled: it's a compiled Swift binary using
#   ScreenCaptureKit/AVAudioEngine/EventKit, all macOS-only frameworks --
#   there's no Windows equivalent to build here. meeting_recorder.py's
#   helper_path() already returns None when the binary isn't found
#   (confirmed safe, no crash), so on Windows: no meeting-audio-capture
#   menu-bar/tray icon, no Apple Calendar source, but everything else
#   (dashboard, WiFi/BLE sync with the device, transcription, Notion/
#   Obsidian/Google sync, AI-pager notifications) works the same.
# - No BUNDLE/.app/Info.plist block -- Windows has no equivalent concept
#   (no usage-description permission prompts to declare; Windows' own
#   Bluetooth/microphone permission prompts, if any, are handled by the
#   OS at the point of first use, not declared up front like macOS TCC).
# - No code signing step here either (same unsigned-build situation as
#   the macOS build -- Windows SmartScreen will warn on first run, same
#   spirit as macOS Gatekeeper's quarantine flag).
#
# Want a real installer instead of a raw folder + Setup Clicky.bat? See
# msix/MSIX.md -- packages this same dist\Clicky\ output into a signed,
# sideloadable .msix (Start Menu entry, clean uninstall, no Store account
# needed).

a = Analysis(
    ['main_packaged.py'],
    pathex=[],
    # rnnoise.dll: vendored RNNoise shared library for background noise
    # suppression on recorded audio (see noise_reduction.py and
    # THIRD_PARTY_NOTICES.md) -- landed at the bundle root to match that
    # module's frozen-mode lookup of sys._MEIPASS/rnnoise.dll.
    binaries=[('rnnoise.dll', '.')],
    datas=[
        ('templates', 'templates'),
        ('static', 'static'),
        ('firmware', 'firmware'),
        # Carries GOOGLE_CLIENT_ID/SECRET (the app's shared OAuth client --
        # see config.py's comment) into the packaged build, same as the
        # macOS spec. Provider API keys (Mistral/OpenAI/etc.) don't need
        # this -- those are entered per-user via /setup.
        ('.env', '.'),
    ],
    hiddenimports=[
        # Same reasoning as clicky.spec's list -- these are imported
        # dynamically inside functions, not at module top-level, so
        # PyInstaller's static analysis can miss them without this.
        'mistralai',
        'mistralai.client',
        'bleak',
        'bleak.backends.winrt',  # Windows' actual BLE backend -- bleak picks this automatically at runtime, but PyInstaller needs it listed to bundle it at all
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
    # No terminal window -- same "no console for a packaged desktop app"
    # intent as clicky.spec's console=False. main_packaged.py already
    # doesn't rely on a visible console for anything (see its own
    # docstring on why it exists separately from app.py's dev-mode
    # __main__ block).
    console=False,
    icon='clicky_icon.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='Clicky',
)
