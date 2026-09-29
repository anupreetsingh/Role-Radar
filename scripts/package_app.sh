#!/bin/sh
# Package Role Radar.app for someone else's Mac (Apple Silicon), with its own Python inside:
# nothing to install, no AWS. Writes dist/Role-Radar-<version>-apple-silicon.zip.
#
# The app keeps its files in ~/Library/Application Support/Role Radar, opens a Setup window on
# first launch (roles, companies via a ChatGPT/Claude prompt, a Gmail app password), and runs
# its checker as its own launchd agent (com.roleradar.app.checker), apart from one run from
# the code. Needs Xcode or the Command Line Tools (swiftc) and uv (for the Python build).
# It's signed ad hoc, not notarized: on first open, macOS asks to confirm in
# System Settings → Privacy & Security → Open Anyway.
set -eu

project="$(cd "$(dirname "$0")/.." && pwd)"
python_build="cpython-3.13.2-macos-aarch64-none"   # python-build-standalone, via uv
version="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$project/role_radar/__init__.py")"
out="$project/dist/Role-Radar-$version-apple-silicon.zip"

command -v uv >/dev/null || { echo "Needs uv: https://docs.astral.sh/uv/" >&2; exit 1; }
build="$(mktemp -d)"
trap 'rm -rf "$build"' EXIT
app="$build/Role Radar.app"
resources="$app/Contents/Resources"
mkdir -p "$app/Contents/MacOS" "$resources"

echo "Building the app for Apple Silicon..."
swiftc -parse-as-library -swift-version 5 -O -target arm64-apple-macos14.0 \
    "$project/macos/RoleRadarMenu.swift" -o "$app/Contents/MacOS/RoleRadarMenu"

echo "Adding Python ($python_build) and Role Radar..."
uv python install "$python_build" --install-dir "$build/pythons" >/dev/null
cp -R "$build/pythons/$python_build" "$resources/python"
py="$resources/python/bin/python3"
uv pip install --quiet --python "$py" --break-system-packages --no-cache "$project"
site="$("$py" -I -c 'import role_radar, os; print(os.path.dirname(role_radar.__file__))')"  # -I: not the project's copy

# Known employers and their verified job boards, so Setup trusts those over an AI's guess.
"$py" - "$project/config/companies.yaml" "$site/templates/directory.yaml" <<'EOF'
import sys, yaml
companies = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))["companies"]
keep = [{k: v for k, v in c.items() if k != "filters"} for c in companies if c.get("enabled", True)]
with open(sys.argv[2], "w", encoding="utf-8") as fh:
    fh.write("# Known employers' job boards, from Role Radar's companies list: used by Setup.\n")
    yaml.safe_dump({"companies": keep}, fh, sort_keys=False, allow_unicode=True, width=200)
print(f"  directory: {len(keep)} known employers")
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
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$version</string>
  <key>CFBundleVersion</key><string>$version</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSHumanReadableCopyright</key><string>MIT License</string>
  <key>RRPackaged</key><true/>
</dict>
</plist>
EOF

echo "Signing (ad hoc)..."
find "$resources/python" -type f \( -name "*.so" -o -name "*.dylib" -o -perm -u+x \) | while IFS= read -r f; do
    if file "$f" | grep -q "Mach-O"; then codesign --force --sign - "$f" >/dev/null 2>&1; fi
done
codesign --force --sign - "$app" >/dev/null
codesign --verify --deep --strict "$app"

mkdir -p "$project/dist"
rm -f "$out"
ditto -c -k --sequesterRsrc --keepParent "$app" "$out"
echo "Built $out ($(du -h "$out" | cut -f1), the app $(du -sh "$app" | cut -f1) unzipped)"
if [ "${KEEP_APP:-}" ]; then
    rm -rf "$KEEP_APP/Role Radar.app" && cp -R "$app" "$KEEP_APP/" && echo "Kept a copy in $KEEP_APP"
fi
