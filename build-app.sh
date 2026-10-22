#!/bin/bash
# Builds SpeakerBalance.app into ./build using the Swift toolchain from Xcode
# or the Command Line Tools. Run it from Terminal:  ./build-app.sh
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v swift >/dev/null 2>&1; then
    echo "swift not found. Install Xcode 13.2.1 (last version for Big Sur) or run: xcode-select --install" >&2
    exit 1
fi

echo "Compiling (release)…"
swift build -c release 2>&1 | grep -v '^\[' || true

BIN=".build/release/SpeakerBalance"
if [ ! -x "$BIN" ]; then
    echo "Build failed – see errors above." >&2
    exit 1
fi

APP="build/SpeakerBalance.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/SpeakerBalance"
cp Resources/Info.plist "$APP/Contents/Info.plist"
printf 'APPL????' > "$APP/Contents/PkgInfo"

# Ad-hoc signature so Gatekeeper is happy with a locally built app.
codesign --force --sign - "$APP" >/dev/null 2>&1 || true

echo "Built $APP"
echo "Open it with:  open \"$APP\"   (or drag it into /Applications)"
