#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
bundle=${1:-"$root/build/Thread Atlas.app"}
contents="$bundle/Contents"
workspace="$contents/Resources/workspace"
mkdir -p "$contents/MacOS" "$workspace"
swiftc "$root/macos/main.swift" -framework AppKit -o "$contents/MacOS/ThreadAtlas"
for file in atlas public extension Start.command pyproject.toml uv.lock; do
  /usr/bin/rsync -a --exclude '__pycache__' "$root/$file" "$workspace/"
done
cat > "$contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>Thread Atlas</string>
<key>CFBundleIdentifier</key><string>io.github.ainta.thread-atlas</string>
<key>CFBundleVersion</key><string>1</string>
<key>CFBundleShortVersionString</key><string>0.1.0</string>
<key>CFBundleExecutable</key><string>ThreadAtlas</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>LSUIElement</key><true/>
</dict></plist>
PLIST
codesign --force --sign - "$bundle"
printf '%s\n' "$bundle"
