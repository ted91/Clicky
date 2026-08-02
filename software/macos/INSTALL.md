# Installing Clicky on macOS

1. **Download** the latest `Clicky.dmg` from the [Releases page](https://github.com/ted91/Clicky/releases/latest).

2. **Open the DMG** (double-click the downloaded file). A window appears with the Clicky icon and a shortcut to your Applications folder.

3. **Drag Clicky into Applications.**

4. **Eject the DMG** (right-click it on the desktop → Eject, or drag it to the Trash) and launch Clicky from Applications.

5. **First launch — macOS will block it.** Since Clicky isn't notarized by Apple yet, Gatekeeper shows a warning like *"Clicky" can't be opened because Apple cannot check it for malicious software* the first time you try to open it. To get past this, do **one** of the following:
   - **Right-click (or Control-click) the Clicky icon in Applications → Open** → a dialog appears with an "Open" button (instead of the plain double-click warning) → click it. You only need to do this once; every launch after that works normally.
   - Or open **System Settings → Privacy & Security**, scroll down to the Security section, and click **"Open Anyway"** next to the Clicky warning that appears there after your first blocked attempt.

6. Clicky now runs as a menu-bar app (look for its icon in the top menu bar). On first run it opens a dashboard tab in your browser to walk through setup (Notion/Obsidian connections, pairing your Clicky device, etc.).

## Updating to a newer version

Just repeat steps 1–4 with the new DMG — drag the new Clicky.app over the old one in Applications (Finder will ask to replace it). You do **not** need to quit the running app first; newer versions detect and replace an old running instance automatically on launch.

## Troubleshooting

- **"Clicky is already running" but nothing seems to be open**: quit any Clicky-related process from Activity Monitor (search "Clicky"), then relaunch.
- **Dashboard won't load in the browser**: the app takes ~15-20 seconds to fully start on first launch each session (it loads a local voice-ID model and scans for your paired device) — wait a bit and reload.
