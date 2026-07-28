#!/bin/bash
# Clicky setup — combined install/uninstall. Double-click this instead of
# opening Terminal yourself: no command-line knowledge needed either way.

set -e

APP="Clicky.app"
DEST="/Applications/$APP"
SRC="$(cd "$(dirname "$0")" && pwd)/$APP"
BUNDLE_ID="com.clicky.pipeline"
DATA_DIR="$HOME/.clicky-pipeline"

do_install() {
    echo "Installing Clicky..."
    cp -r "$SRC" "$DEST"
    xattr -dr com.apple.quarantine "$DEST" 2>/dev/null || true
    echo "Done. Launching Clicky..."
    open "$DEST"
}

do_uninstall() {
    echo "Stopping Clicky if it's running..."
    pkill -f "Clicky.app/Contents/MacOS/Clicky" 2>/dev/null || true
    pkill -f "meetingcap$" 2>/dev/null || true
    sleep 1

    echo "Removing $DEST..."
    rm -rf "$DEST"

    echo ""
    echo "Resetting Screen Recording / Microphone / Calendar permissions for Clicky..."
    # Operates on the current user's own permission database, no sudo
    # needed. If this fails, it's safe to ignore -- just remove the stale
    # "Clicky" entry manually from System Settings > Privacy & Security.
    tccutil reset ScreenCapture "$BUNDLE_ID" 2>/dev/null || echo "  (skipped -- remove manually in System Settings if listed)"
    tccutil reset Microphone "$BUNDLE_ID" 2>/dev/null || echo "  (skipped -- remove manually in System Settings if listed)"
    tccutil reset Calendar "$BUNDLE_ID" 2>/dev/null || echo "  (skipped -- remove manually in System Settings if listed)"

    # Deliberately NEVER touches $DATA_DIR (recordings, settings.json --
    # including your API keys, Notion token, Google OAuth token). Wiping
    # that as part of "uninstall" is exactly the kind of surprise that
    # burns trust -- most software leaves your config behind so
    # reinstalling doesn't mean starting over from scratch. If you
    # genuinely want to delete it, do that yourself, deliberately:
    #   rm -rf "$DATA_DIR"
    if [ -d "$DATA_DIR" ]; then
        echo ""
        echo "Your recordings and settings (including saved API keys/tokens) are kept at:"
        echo "  $DATA_DIR"
        echo "Delete that folder yourself if you want a completely clean slate."
    fi
    echo ""
    echo "Clicky has been uninstalled."
}

if [ -d "$DEST" ]; then
    echo "⚠️  Clicky is already installed at $DEST"
    echo ""
    echo "What would you like to do?"
    echo "  1) Reinstall (overwrite with this version)"
    echo "  2) Uninstall"
    echo "  3) Cancel"
    read -p "Choose 1, 2, or 3: " choice
    case "$choice" in
        1) rm -rf "$DEST"; do_install ;;
        2) do_uninstall ;;
        *) echo "Cancelled." ;;
    esac
else
    do_install
fi

read -p "Press Return to close this window..."
