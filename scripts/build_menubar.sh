#!/bin/sh
# Build the Role Radar menu bar app into ~/Applications/Role Radar.app and open it,
# restarting the Mac's checker on the current code.
# Needs Xcode or the Command Line Tools (swiftc) and this project's .venv.
set -eu

project="$(cd "$(dirname "$0")/.." && pwd)"
python="$project/.venv/bin/python"
app="${ROLE_RADAR_APP:-$HOME/Applications/Role Radar.app}"

[ -x "$python" ] || { echo "No $python: create the venv and install role-radar first." >&2; exit 1; }

build="$(mktemp -d)"
trap 'rm -rf "$build"' EXIT
swiftc -parse-as-library -swift-version 5 -O -target "$(uname -m)-apple-macos14.0" \
    "$project/macos/RoleRadarMenu.swift" -o "$build/RoleRadarMenu"

# Stop the Mac's checker (it finishes the companies in flight) so the new app starts it on the current code.
"$python" -m role_radar stop >/dev/null 2>&1 || true
pkill -x RoleRadarMenu 2>/dev/null || true
rm -rf "$app"
mkdir -p "$app/Contents/MacOS"
cp "$build/RoleRadarMenu" "$app/Contents/MacOS/RoleRadarMenu"
cat > "$app/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Role Radar</string>
  <key>CFBundleDisplayName</key><string>Role Radar</string>
  <key>CFBundleIdentifier</key><string>com.roleradar.menu</string>
  <key>CFBundleExecutable</key><string>RoleRadarMenu</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
  <key>RRProjectDir</key><string>$project</string>
  <key>RRPython</key><string>$python</string>
</dict>
</plist>
EOF
codesign --force --sign - "$app" >/dev/null
echo "Built $app"
open "$app"
