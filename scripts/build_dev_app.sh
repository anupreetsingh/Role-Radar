#!/bin/sh
# Build the downloadable app from the working tree, for trying changes out: build/Role Radar.app
# (git-ignored). Unlike scripts/package_app.sh it carries no Python of its own and makes no zip:
# it runs the repo's .venv (an editable install), so Python edits take effect without a rebuild;
# Swift edits need one. The profession lists are written to role_radar/lists/ (git-ignored).
# It behaves as the packaged app does: its files in ~/Library/Application Support/Role Radar,
# its own checker agent, Setup on first open. Pass --open to (re)open it once built.
set -eu

project="$(cd "$(dirname "$0")/.." && pwd)"
app="$project/build/Role Radar.app"
# Built whole in a hidden folder, then moved into place: a Finder window on build/ would otherwise
# catch the app half-made, without its icon, and keep showing the plain one it saw.
stage="$project/build/.staging/Role Radar.app"
python="$project/.venv/bin/python"
version="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$project/role_radar/__init__.py")"
[ -x "$python" ] || { echo "Needs the project's .venv (uv venv && uv pip install -e .)" >&2; exit 1; }

# Quit a copy that's running, so the new one replaces it, and wait for it: quitting stops its checker
# with a command run from the app, so the app mustn't be deleted before that has run.
osascript -e 'tell application id "com.roleradar.app" to quit' >/dev/null 2>&1 || true
for _ in 1 2 3 4 5 6 7 8 9 10; do
    pgrep -f "$app/Contents/MacOS/RoleRadarMenu" >/dev/null || break
    sleep 0.5
done
ROLE_RADAR_HOME="$HOME/Library/Application Support/Role Radar" ROLE_RADAR_AGENT=com.roleradar.app.checker \
    "$python" -m role_radar stop >/dev/null 2>&1 || true  # the app's own checker only (its own home and agent)

rm -rf "$stage"
mkdir -p "$stage/Contents/MacOS" "$stage/Contents/Resources/python/bin"
echo "Building the app..."
swiftc -parse-as-library -swift-version 5 -target arm64-apple-macos14.0 \
    "$project/macos/RoleRadarMenu.swift" -o "$stage/Contents/MacOS/RoleRadarMenu"
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
"$python" - "$project/config" "$project/role_radar/lists" <<'EOF'
import sys, yaml
from pathlib import Path
source, target = Path(sys.argv[1]), Path(sys.argv[2])
target.mkdir(exist_ok=True)
for profession, name in (("tech", "companies.yaml"), ("accounting", "accounting.yaml"), ("healthcare", "healthcare.yaml")):
    if not (source / name).exists():
        (target / f"{profession}.yaml").unlink(missing_ok=True)
        print(f"  {profession}: no list yet")
        continue
    companies = yaml.load((source / name).read_text(encoding="utf-8"), Loader=yaml.CSafeLoader)["companies"]
    keep = [{k: v for k, v in c.items() if k != "filters"} for c in companies]
    with open(target / f"{profession}.yaml", "w", encoding="utf-8") as fh:
        fh.write(f"# Role Radar's {profession} companies, from config/{name} (scripts/build_dev_app.sh).\n")
        yaml.safe_dump({"companies": keep}, fh, sort_keys=False, allow_unicode=True, width=200)
    print(f"  {profession}: {sum(c.get('enabled', True) for c in keep)} companies")
EOF

cat > "$stage/Contents/Info.plist" <<EOF
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
  <key>CFBundleShortVersionString</key><string>$version-dev</string>
  <key>CFBundleVersion</key><string>$version</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
  <key>RRPackaged</key><true/>
</dict>
</plist>
EOF

rm -rf "$app"
mv "$stage" "$app"
rmdir "$project/build/.staging" 2>/dev/null || true
# Tell Finder and the Dock it changed, so they show its icon rather than one remembered from before.
touch "$app"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$app" || true
echo "Built $app"
if [ "${1:-}" = "--open" ]; then
    open "$app"
fi
