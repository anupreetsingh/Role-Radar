#!/bin/sh
# Build the downloadable app from the working tree, for trying changes out: build/Role Radar Dev.app
# (git-ignored). Unlike scripts/package_app.sh it carries no Python of its own and makes no zip:
# it runs the repo's .venv (an editable install), so Python edits take effect without a rebuild;
# Swift edits need one. The profession lists are written to role_radar/lists/ (git-ignored).
# It behaves as the packaged app does (Setup on first open), but as an app of its own beside the
# installed Role Radar: "Role Radar Dev" (com.roleradar.app.dev), with its own files in
# ~/Library/Application Support/Role Radar Dev, checker agent and Keychain items, so trying it out
# never touches the installed app. It doesn't check job sites, so the Mac's requests aren't doubled:
# build with DEV_CHECKS=1 for one that does. Pass --open to (re)open it once built.
set -eu

project="$(cd "$(dirname "$0")/.." && pwd)"
app="$project/build/Role Radar Dev.app"
# Built whole in a hidden folder, then moved into place: a Finder window on build/ would otherwise
# catch the app half-made, without its icon, and keep showing the plain one it saw.
stage="$project/build/.staging/Role Radar Dev.app"
python="$project/.venv/bin/python"
version="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$project/role_radar/__init__.py")"
[ -x "$python" ] || { echo "Needs the project's .venv (uv venv && uv pip install -e .)" >&2; exit 1; }
id=com.roleradar.app.dev
home="Role Radar Dev"  # its folder in ~/Library/Application Support
agent=com.roleradar.app.dev.checker
checks='<key>RRNoChecks</key><true/>'
[ "${DEV_CHECKS:-}" = 1 ] && checks=''

# Quit a copy that's running, so the new one replaces it, and wait for it: quitting stops its checker
# with a command run from the app, so the app mustn't be deleted before that has run.
osascript -e "tell application id \"$id\" to quit" >/dev/null 2>&1 || true
for _ in 1 2 3 4 5 6 7 8 9 10; do
    pgrep -f "$app/Contents/MacOS/RoleRadarMenu" >/dev/null || break
    sleep 0.5
done
ROLE_RADAR_HOME="$HOME/Library/Application Support/$home" ROLE_RADAR_AGENT="$agent" \
    "$python" -m role_radar stop >/dev/null 2>&1 || true  # the dev build's own checker only (its own home and agent)

rm -rf "$stage"
mkdir -p "$stage/Contents/MacOS" "$stage/Contents/Resources/python/bin"
echo "Building the app..."
# Optimized and with Sparkle, as the packaged app is (unoptimized SwiftUI feels sluggish), but with no
# update feed: the dev build never updates itself.
sparkle="$(sh "$project/scripts/get_sparkle.sh")"
swiftc -parse-as-library -swift-version 5 -O -target arm64-apple-macos14.0 \
    -F "$sparkle" -framework Sparkle -Xlinker -rpath -Xlinker @executable_path/../Frameworks \
    "$project/macos/RoleRadarMenu.swift" -o "$stage/Contents/MacOS/RoleRadarMenu"
mkdir -p "$stage/Contents/Frameworks"
ditto "$sparkle/Sparkle.framework" "$stage/Contents/Frameworks/Sparkle.framework"
if xcrun --find actool >/dev/null 2>&1; then
    xcrun actool "$project/macos/AppIcon.icon" --compile "$stage/Contents/Resources" --platform macosx \
        --minimum-deployment-target 14.0 --app-icon AppIcon --output-partial-info-plist "$project/build/icon.plist" >/dev/null
fi

# Where the packaged app has its own Python, this one runs the repo's.
cat > "$stage/Contents/Resources/python/bin/python3" <<EOF
#!/bin/sh
exec "$python" "\$@"
EOF
chmod +x "$stage/Contents/Resources/python/bin/python3"

# The professions' company lists, as package_app.sh writes them into the app.
"$python" "$project/scripts/write_lists.py" "$project/role_radar/lists"

cat > "$stage/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Role Radar Dev</string>
  <key>CFBundleDisplayName</key><string>Role Radar Dev</string>
  <key>CFBundleIdentifier</key><string>$id</string>
  <key>CFBundleExecutable</key><string>RoleRadarMenu</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundleIconName</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$version-dev</string>
  <key>CFBundleVersion</key><string>$version</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
  <key>RRPackaged</key><true/>
  <key>RRDevBuild</key><true/>
  <key>RRHome</key><string>$home</string>
  <key>RRAgent</key><string>$agent</string>
  <key>RRKeychain</key><string>$id</string>
  $checks
</dict>
</plist>
EOF

rm -rf "$app"
mv "$stage" "$app"
rm -rf "$project/build/.staging"  # Finder may have left a .DS_Store in it
# Tell Finder and the Dock it changed, so they show its icon rather than one remembered from before.
touch "$app"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$app" || true
echo "Built $app"
if [ "${1:-}" = "--open" ]; then
    open "$app"
fi
