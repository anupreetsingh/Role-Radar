#!/bin/sh
# Package Role Radar for Windows (10 version 1703 or later, or 11, 64-bit; Windows on ARM runs it too),
# from this Mac. Writes dist/Role-Radar-<version>-windows-setup.exe: an installer with everything
# inside, so there's nothing else to install, and no AWS.
#
#   sh scripts/package_windows.sh          # the app people download
#   sh scripts/package_windows.sh --dev    # "Role Radar Dev", for trying it out on a Windows PC (a VM):
#                                          # its own files and credentials, no job sites checked, no updates
#                                          # (DEV_CHECKS=1 for one that checks)
#
# The installer holds the app (windows/RoleRadar, C# and WPF, with .NET inside), WinSparkle for updates,
# app.json naming the app's files and its update feed, and in python\ the checker: Python's embeddable
# package with Role Radar and the professions' company lists, run by Role Radar Checker.exe
# (windows/launcher, built with zig). Needs the .NET 10 SDK (https://dot.net, or its dotnet-install
# script into ~/.dotnet), uv (Python, zig and pefile come through it) and NSIS (brew install makensis).
# Neither the installer nor the app is signed: on a first download, Windows SmartScreen says it
# "protected your PC" until More info > Run anyway. Updates (scripts/publish_update.sh) are signed
# with the update key, which WinSparkle checks.
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

dotnet="$(command -v dotnet || echo "$HOME/.dotnet/dotnet")"
[ -x "$dotnet" ] || { echo "Needs the .NET 10 SDK: https://dot.net" >&2; exit 1; }
command -v uv >/dev/null || { echo "Needs uv: https://docs.astral.sh/uv/" >&2; exit 1; }
command -v makensis >/dev/null || { echo "Needs NSIS: brew install makensis" >&2; exit 1; }
[ -n "$update_key" ] || { echo "No update_key in scripts/package_app.sh" >&2; exit 1; }
export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
cache="$project/build/windows"
build="$(mktemp -d)"
trap 'rm -rf "$build"' EXIT
stage="$build/$name"
python="$stage/python"
site="$python/Lib/site-packages"
mkdir -p "$cache" "$python"

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

echo "Building the app (.NET, for 64-bit Windows)..."
"$dotnet" publish "$project/windows/RoleRadar/RoleRadar.csproj" -c Release -r win-x64 --self-contained true \
    -p:Version="$version" -p:AssemblyTitle="$name" -p:PublishReadyToRun=true -p:DebugType=none -o "$stage" \
    -v quiet -nologo >/dev/null

echo "Adding Python $python_version (the embeddable package) and Role Radar..."
fetch "https://www.python.org/ftp/python/$python_version/python-$python_version-embed-amd64.zip" \
    "python-$python_version-embed-amd64.zip" "$python_sha256"
unzip -q "$cache/python-$python_version-embed-amd64.zip" -d "$python"
rm "$python/pythonw.exe"  # Role Radar Checker.exe is the windowless one; python.exe stays, for a look from a terminal
# Only Role Radar's folder and packages on Python's path, whatever this PC's Python settings are.
printf 'python313.zip\r\n.\r\nLib\\site-packages\r\nimport site\r\n' > "$python/python313._pth"
uv pip install --quiet --target "$site" --python-platform x86_64-pc-windows-msvc --python-version 3.13 "$project"
rm -rf "$site/bin"
uv run --quiet --no-project --with pyyaml python "$project/scripts/write_lists.py" "$site/role_radar/lists"
# Compiled once here, so Python never writes into the app (and starts faster): the same for any 3.13.
uv run --quiet --no-project --python 3.13 python -m compileall -q -j 0 --invalidation-mode unchecked-hash "$site" >/dev/null

echo "Building Role Radar Checker.exe..."
cp "$project/windows/launcher/launcher.c" "$project/windows/launcher/launcher.manifest" "$build/"
cp "$project/windows/RoleRadar/Assets/AppIcon.ico" "$build/"
sed -e "s/@NAME@/$name/g" -e "s/@VERSION@/$version/g" -e "s/@VERSION_COMMAS@/$(echo "$version" | tr . ,)/g" \
    "$project/windows/launcher/launcher.rc" > "$build/launcher.rc"
(cd "$build" && uv run --quiet --no-project --with "$zig" python -m ziglang cc -target x86_64-windows-gnu -municode \
    -Wl,--subsystem,windows -O2 -s -o "$python/Role Radar Checker.exe" launcher.c launcher.rc -lshell32)

echo "Adding WinSparkle $winsparkle_version (updates)..."
fetch "https://github.com/vslavik/winsparkle/releases/download/v$winsparkle_version/WinSparkle-$winsparkle_version.zip" \
    "WinSparkle-$winsparkle_version.zip" "$winsparkle_sha256"
unzip -q -j -o "$cache/WinSparkle-$winsparkle_version.zip" "WinSparkle-$winsparkle_version/x64/Release/WinSparkle.dll" -d "$stage"

cat > "$stage/app.json" <<EOF
{
  "name": "$name",
  "home": "$name",
  "keychain": "$name",
  "version": "$version",
  "checks": $checks,
  "dev": $([ "$dev" ] && echo true || echo false),
  "feed": $([ "$dev" ] && echo null || echo "\"$update_feed\""),
  "update_key": $([ "$dev" ] && echo null || echo "\"$update_key\"")
}
EOF

echo "Checking that everything it loads and names is there..."
uv run --quiet --no-project --with pefile python "$project/windows/build.py" check "$stage"
python3 "$project/windows/build.py" check-app

echo "Making the installer..."
mkdir -p "$project/dist"
rm -f "$out"
makensis -V2 -DNAME="$name" -DVERSION="$version" -DKEY="$key" -DSOURCE="$stage" \
    -DICON="$project/windows/RoleRadar/Assets/AppIcon.ico" -DOUTFILE="$out" "$project/windows/installer.nsi" >/dev/null
echo "Built $out ($(du -h "$out" | cut -f1), the app $(du -sh "$stage" | cut -f1))"
if [ "${KEEP_APP:-}" ]; then
    rm -rf "$KEEP_APP/$name" && cp -R "$stage" "$KEEP_APP/" && echo "Kept a copy in $KEEP_APP"
fi
