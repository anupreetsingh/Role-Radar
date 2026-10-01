#!/bin/sh
# Package Role Radar.app for someone else's Mac (Apple Silicon), with its own Python inside:
# nothing to install, no AWS. Writes dist/Role-Radar-<version>-apple-silicon.dmg: a disk image whose
# window shows the app beside Applications to drag it onto (the same file is the update).
#
# The app keeps its files in ~/Library/Application Support/Role Radar, opens a Setup window on
# first launch (a profession, its job titles and countries, a Gmail app password), and runs
# its checker as its own launchd agent (com.roleradar.app.checker), apart from one run from
# the code. Needs Xcode (swiftc, and actool for the icon) and uv (for the Python build).
# It's signed ad hoc, not notarized: on first open, macOS asks to confirm in
# System Settings → Privacy & Security → Open Anyway.
#
# It updates itself with Sparkle (scripts/get_sparkle.sh): it reads the latest GitHub release's
# appcast.xml every few hours, and installs an update only if it's signed with the key whose
# public half is below. scripts/publish_update.sh signs and publishes a version built here.
# Opened from anywhere but Applications, the app offers to move itself there.
set -eu

# Where every copy looks for updates, and the key updates must be signed with (its private half is
# in the Keychain of the Mac that publishes: `generate_keys --account role-radar`).
update_feed="https://github.com/anupreetsingh/Role-Radar/releases/latest/download/appcast.xml"
update_key="l2iPW44nrwGWO2yH4EjdIKwl67g54tiEpmmwZzV6GL0="

project="$(cd "$(dirname "$0")/.." && pwd)"
python_build="cpython-3.13.2-macos-aarch64-none"   # python-build-standalone, via uv
version="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$project/role_radar/__init__.py")"
out="$project/dist/Role-Radar-$version-apple-silicon.dmg"

command -v uv >/dev/null || { echo "Needs uv: https://docs.astral.sh/uv/" >&2; exit 1; }
build="$(mktemp -d)"
trap 'rm -rf "$build"' EXIT
app="$build/Role Radar.app"
resources="$app/Contents/Resources"
mkdir -p "$app/Contents/MacOS" "$resources"

echo "Building the app for Apple Silicon..."
sparkle="$(sh "$project/scripts/get_sparkle.sh")"
swiftc -parse-as-library -swift-version 5 -O -target arm64-apple-macos14.0 \
    -F "$sparkle" -framework Sparkle -Xlinker -rpath -Xlinker @executable_path/../Frameworks \
    "$project/macos/RoleRadarMenu.swift" -o "$app/Contents/MacOS/RoleRadarMenu"
mkdir -p "$app/Contents/Frameworks"
ditto "$sparkle/Sparkle.framework" "$app/Contents/Frameworks/Sparkle.framework"  # as signed by Sparkle
# The icon (macos/AppIcon.icon, from Icon Composer): what macOS 26 draws, and AppIcon.icns for older macOS.
if xcrun --find actool >/dev/null 2>&1; then
    xcrun actool "$project/macos/AppIcon.icon" --compile "$resources" --platform macosx --minimum-deployment-target 14.0 \
        --app-icon AppIcon --output-partial-info-plist "$build/icon.plist" >/dev/null
else
    echo "  no Xcode (actool): the app gets macOS's plain icon" >&2
fi

echo "Adding Python ($python_build) and Role Radar..."
uv python install "$python_build" --install-dir "$build/pythons" >/dev/null
cp -R "$build/pythons/$python_build" "$resources/python"
py="$resources/python/bin/python3"
uv pip install --quiet --python "$py" --break-system-packages --no-cache "$project"
site="$("$py" -I -c 'import role_radar, os; print(os.path.dirname(role_radar.__file__))')"  # -I: not the project's copy

# Each profession's company list (config.profession_list): Tech is the companies file, the others sit
# beside it. The app checks the picked profession's list, narrowed to the person's countries.
mkdir -p "$site/lists"
"$py" - "$project/config" "$site/lists" <<'EOF'
import sys, yaml
from pathlib import Path
source, target = Path(sys.argv[1]), Path(sys.argv[2])
for profession, name in (("tech", "companies.yaml"), ("accounting", "accounting.yaml"), ("healthcare", "healthcare.yaml")):
    if not (source / name).exists():
        print(f"  {profession}: no list yet")
        continue
    companies = yaml.load((source / name).read_text(encoding="utf-8"), Loader=yaml.CSafeLoader)["companies"]
    keep = [{k: v for k, v in c.items() if k != "filters"} for c in companies]
    with open(target / f"{profession}.yaml", "w", encoding="utf-8") as fh:
        fh.write(f"# Role Radar's {profession} companies, with the countries each posts jobs in; built from config/{name}.\n")
        yaml.safe_dump({"companies": keep}, fh, sort_keys=False, allow_unicode=True, width=200)
    print(f"  {profession}: {sum(c.get('enabled', True) for c in keep)} companies ({len(keep)} with those switched off)")
EOF

# Leave out what the app never uses, then compile everything once: Python never writes into the app.
lib="$resources/python/lib/python3.13"
rm -rf "$resources/python/include" "$resources/python/share" "$lib/test" "$lib/idlelib" "$lib/tkinter" \
    "$lib/turtledemo" "$lib/ensurepip" "$lib/lib2to3" "$lib/pydoc_data" "$lib/config-3.13-darwin" \
    "$resources/python/lib/libtcl"* "$resources/python/lib/libtk"* "$resources/python/lib/tcl"* "$resources/python/lib/tk"* \
    "$lib/lib-dynload/_tkinter"*
find "$resources/python" -name "__pycache__" -type d -prune -exec rm -rf {} +
"$py" -m compileall -q -j 0 "$lib" >/dev/null || true

cat > "$app/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Role Radar</string>
  <key>CFBundleDisplayName</key><string>Role Radar</string>
  <key>CFBundleIdentifier</key><string>com.roleradar.app</string>
  <key>CFBundleExecutable</key><string>RoleRadarMenu</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundleIconName</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$version</string>
  <key>CFBundleVersion</key><string>$version</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSHumanReadableCopyright</key><string>MIT License</string>
  <key>RRPackaged</key><true/>
  <key>SUFeedURL</key><string>$update_feed</string>
  <key>SUPublicEDKey</key><string>$update_key</string>
  <key>SUEnableAutomaticChecks</key><true/>
  <key>SUScheduledCheckInterval</key><integer>21600</integer>
</dict>
</plist>
EOF

echo "Signing (ad hoc)..."
find "$resources/python" -type f \( -name "*.so" -o -name "*.dylib" -o -perm -u+x \) | while IFS= read -r f; do
    if file "$f" | grep -q "Mach-O"; then codesign --force --sign - "$f" >/dev/null 2>&1; fi
done
codesign --force --sign - "$app" >/dev/null
codesign --verify --deep --strict "$app"

echo "Making the disk image..."
mkdir -p "$project/dist"
rm -f "$out"
# dmgbuild writes the window's layout itself (no Finder scripting): the app, an arrow, Applications.
cat > "$build/dmg.py" <<'EOF'
import os
app = defines["app"]
files = [app]
symlinks = {"Applications": "/Applications"}
icon = os.path.join(app, "Contents", "Resources", "AppIcon.icns")
if not os.path.exists(icon):
    del icon
background = "builtin-arrow"  # 640 by 240, its arrow between the two icons
window_rect = ((200, 200), (640, 280))
icon_size = 112
text_size = 13
icon_locations = {os.path.basename(app): (140, 120), "Applications": (500, 120)}
format = "ULFO"
EOF
uv run --quiet --no-project --with dmgbuild dmgbuild -s "$build/dmg.py" -D app="$app" "Role Radar" "$out" >/dev/null
echo "Built $out ($(du -h "$out" | cut -f1), the app $(du -sh "$app" | cut -f1))"
if [ "${KEEP_APP:-}" ]; then
    rm -rf "$KEEP_APP/Role Radar.app" && cp -R "$app" "$KEEP_APP/" && echo "Kept a copy in $KEEP_APP"
fi
