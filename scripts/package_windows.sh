#!/bin/sh
# Package Role Radar for Windows (10 or 11, 64-bit; Windows on ARM runs it too), from this Mac.
# Writes dist/Role-Radar-<version>-windows-setup.exe: an installer with its own Python inside, so
# there's nothing else to install, and no AWS.
#
#   sh scripts/package_windows.sh          # the app people download
#   sh scripts/package_windows.sh --dev    # "Role Radar Dev", for trying it out on a Windows PC (a VM):
#                                          # its own files and credentials, no job sites checked, no updates
#                                          # (DEV_CHECKS=1 for one that checks)
#
# The installer holds: Role Radar.exe (windows/launcher, built with zig), Python's embeddable
# package with Role Radar and the Windows app (windows/role_radar_app) and the parts of Qt it uses,
# the professions' company lists, WinSparkle for updates, and app.json naming the app's files and its
# update feed. Built with uv (Python, Qt, zig and pefile come through it) and NSIS (brew install
# makensis). Neither the installer nor the app is signed: on a first download, Windows SmartScreen
# says it "protected your PC" until More info > Run anyway. Updates (scripts/publish_update.sh)
# are signed with the update key, which WinSparkle checks.
set -eu

python_version=3.13.16
python_sha256=97dae5274cc54867065e8d5a3226e48c35017ed332a0fdb0e27d5b5821961297
winsparkle_version=0.9.4
winsparkle_sha256=6037df37fc263bd1650a1c4949681a9d40ffe991d01f35892a406cb5d103c976
zig=ziglang==0.16.0

project="$(cd "$(dirname "$0")/.." && pwd)"
version="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$project/role_radar/__init__.py")"
# Updates: the latest GitHub release's Windows feed, signed with the Mac app's update key.
update_feed="https://github.com/anupreetsingh/Role-Radar/releases/latest/download/appcast-windows.xml"
update_key="$(sed -n 's/^update_key="\(.*\)"/\1/p' "$project/scripts/package_app.sh")"
dev=""
[ "${1:-}" = "--dev" ] && dev=1
if [ "$dev" ]; then
    name="Role Radar Dev" key=RoleRadarDev checks=false
    [ "${DEV_CHECKS:-}" = 1 ] && checks=true
    out="$project/dist/Role-Radar-Dev-$version-windows-setup.exe"
else
    name="Role Radar" key=RoleRadar checks=true
    out="$project/dist/Role-Radar-$version-windows-setup.exe"
fi
exe="$name.exe"

command -v uv >/dev/null || { echo "Needs uv: https://docs.astral.sh/uv/" >&2; exit 1; }
command -v makensis >/dev/null || { echo "Needs NSIS: brew install makensis" >&2; exit 1; }
[ -n "$update_key" ] || { echo "No update_key in scripts/package_app.sh" >&2; exit 1; }
cache="$project/build/windows"
build="$(mktemp -d)"
trap 'rm -rf "$build"' EXIT
stage="$build/$name"
site="$stage/Lib/site-packages"
mkdir -p "$cache" "$stage"

# Download once into build/windows (git-ignored), checked against the checksum recorded above.
fetch() {  # URL FILE SHA256
    if [ ! -f "$cache/$2" ]; then
        curl -fsSL -o "$cache/$2.part" "$1"
        mv "$cache/$2.part" "$cache/$2"
    fi
    if ! echo "$3  $cache/$2" | shasum -a 256 -c - >/dev/null 2>&1; then
        echo "$2: the download doesn't match its checksum" >&2
        rm -f "$cache/$2"
        exit 1
    fi
}

echo "Adding Python $python_version (the embeddable package)..."
fetch "https://www.python.org/ftp/python/$python_version/python-$python_version-embed-amd64.zip" \
    "python-$python_version-embed-amd64.zip" "$python_sha256"
unzip -q "$cache/python-$python_version-embed-amd64.zip" -d "$stage"
rm "$stage/pythonw.exe"  # Role Radar.exe is the windowless one; python.exe stays, for a look from a terminal
# Only the app's own folder and packages on Python's path, whatever this PC's Python settings are.
printf 'python313.zip\r\n.\r\nLib\\site-packages\r\nimport site\r\n' > "$stage/python313._pth"

echo "Adding Role Radar, the Windows app and Qt..."
uv pip install --quiet --target "$site" --python-platform x86_64-pc-windows-msvc --python-version 3.13 "$project"
uv pip install --quiet --target "$site" --python-platform x86_64-pc-windows-msvc --python-version 3.13 \
    --only-binary :all: -r "$project/windows/requirements.txt"
rm -rf "$site/bin"
cp -R "$project/windows/role_radar_app" "$site/"
find "$site/role_radar_app" -name "__pycache__" -type d -prune -exec rm -rf {} +
cp "$project/macos/AppIcon.icon/Assets/glyph.svg" "$site/role_radar_app/glyph.svg"  # the Mac icon's glyph
uv run --quiet --no-project --with pyyaml python "$project/scripts/write_lists.py" "$site/role_radar/lists"
uv run --quiet --no-project python "$project/windows/build.py" prune "$site"
# Compiled once here, so Python never writes into the app (and starts faster): the same for any 3.13.
uv run --quiet --no-project --python 3.13 python -m compileall -q -j 0 --invalidation-mode unchecked-hash "$site" >/dev/null

echo "Adding WinSparkle $winsparkle_version (updates)..."
fetch "https://github.com/vslavik/winsparkle/releases/download/v$winsparkle_version/WinSparkle-$winsparkle_version.zip" \
    "WinSparkle-$winsparkle_version.zip" "$winsparkle_sha256"
unzip -q -j -o "$cache/WinSparkle-$winsparkle_version.zip" "WinSparkle-$winsparkle_version/x64/Release/WinSparkle.dll" -d "$stage"

echo "Building $exe..."
uv run --quiet --no-project --with-requirements "$project/windows/requirements.txt" python "$project/windows/build.py" icon "$build/AppIcon.ico"
cp "$project/windows/launcher/launcher.c" "$project/windows/launcher/launcher.manifest" "$build/"
sed -e "s/@NAME@/$name/g" -e "s/@VERSION@/$version/g" -e "s/@VERSION_COMMAS@/$(echo "$version" | tr . ,)/g" \
    "$project/windows/launcher/launcher.rc" > "$build/launcher.rc"
(cd "$build" && uv run --quiet --no-project --with "$zig" python -m ziglang cc -target x86_64-windows-gnu -municode \
    -Wl,--subsystem,windows -O2 -s -o "$stage/$exe" launcher.c launcher.rc -lshell32)

cat > "$stage/app.json" <<EOF
{
  "name": "$name",
  "home": "$name",
  "keychain": "$name",
  "checks": $checks,
  "dev": $([ "$dev" ] && echo true || echo false),
  "feed": $([ "$dev" ] && echo null || echo "\"$update_feed\""),
  "update_key": $([ "$dev" ] && echo null || echo "\"$update_key\"")
}
EOF

echo "Checking that everything it loads is there..."
uv run --quiet --no-project --with pefile python "$project/windows/build.py" check "$stage"

echo "Making the installer..."
mkdir -p "$project/dist"
rm -f "$out"
makensis -V2 -DNAME="$name" -DEXE="$exe" -DVERSION="$version" -DHOME="$name" -DKEY="$key" -DSOURCE="$stage" \
    -DICON="$build/AppIcon.ico" -DOUTFILE="$out" "$project/windows/installer.nsi" >/dev/null
echo "Built $out ($(du -h "$out" | cut -f1), the app $(du -sh "$stage" | cut -f1))"
if [ "${KEEP_APP:-}" ]; then
    rm -rf "$KEEP_APP/$name" && cp -R "$stage" "$KEEP_APP/" && echo "Kept a copy in $KEEP_APP"
fi
