#!/bin/sh
set -eu
src=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
bundle=$1
mkdir -p "$bundle/Contents/MacOS"
swiftc "$src/alerts.swift" -framework AppKit -framework UserNotifications -o "$bundle/Contents/MacOS/Alerts"
cat > "$bundle/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>io.github.ainta.thread-atlas.alerts</string>
<key>CFBundleName</key><string>Thread Atlas Alerts</string>
<key>CFBundleExecutable</key><string>Alerts</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleVersion</key><string>1</string>
<key>LSUIElement</key><true/>
<key>CFBundleDocumentTypes</key><array><dict>
<key>CFBundleTypeName</key><string>Thread Atlas reply event</string>
<key>CFBundleTypeExtensions</key><array><string>atlasreply</string></array>
<key>CFBundleTypeRole</key><string>Viewer</string>
<key>LSHandlerRank</key><string>Alternate</string>
</dict></array>
</dict></plist>
PLIST
codesign --force --sign - "$bundle"
