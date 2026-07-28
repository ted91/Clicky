#!/bin/bash
# Builds the meetingcap helper binary. Requires Xcode Command Line Tools
# (xcode-select --install). Run before packaging the app:
#   ./meetingcap/build.sh && .venv/bin/pyinstaller clicky.spec --noconfirm
set -e
cd "$(dirname "$0")"

if ! command -v swiftc >/dev/null; then
    echo "error: swiftc not found — install Xcode Command Line Tools: xcode-select --install" >&2
    exit 1
fi

echo "Compiling meetingcap..."
swiftc -O \
    -framework AppKit \
    -framework ScreenCaptureKit \
    -framework AVFoundation \
    -framework CoreAudio \
    -framework CoreMedia \
    -framework EventKit \
    -o meetingcap \
    main.swift
echo "Built: $(pwd)/meetingcap"
