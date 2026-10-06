#!/bin/sh
# Publish the version packaged by scripts/package_app.sh and scripts/package_windows.sh as an update:
# every copy of the app finds it within six hours (Sparkle on the Mac, WinSparkle on Windows), or at
# once with "Check for Updates…".
#
#   sh scripts/package_app.sh && sh scripts/package_windows.sh
#   sh scripts/publish_update.sh [NOTES.md]      # DRY_RUN=1 writes the feeds in dist/ and stops there
#
# Signs dist/Role-Radar-<version>-apple-silicon.dmg and dist/Role-Radar-<version>-windows-setup.exe
# with the update key in this Mac's Keychain (made once with `generate_keys --account role-radar`;
# macOS asks to let sign_update use it), writes dist/appcast.xml and dist/appcast-windows.xml naming
# them the newest version, and creates GitHub release v<version> with them all (SPARKLE_KEY_FILE:
# sign with a key exported by `generate_keys -x` instead, on another Mac).
# People download them as Role-Radar-apple-silicon.dmg and Role-Radar-windows-setup.exe, the same
# names in every release, so .../releases/latest/download/<name> (the README's Download buttons, the
# repo's website link) is always the newest version. Updates fetch the same files under other names,
# Role-Radar-<version>-update.dmg and Role-Radar-<version>-windows-update.exe, so the downloads
# badge counts people downloading the app, not updates.
# Every copy reads the latest release's feed, so each release carries both apps: WINDOWS=0 publishes
# the Mac app alone (Windows copies then see no update, and the Windows download link is broken,
# until a release has it again). Publish updates as normal releases, not pre-releases, and bump
# __version__ in role_radar/__init__.py for each one.
# NOTES.md (optional) becomes the release notes: "- " lines become a list, others paragraphs.
set -eu

repo="anupreetsingh/Role-Radar"  # as in package_app.sh's and package_windows.sh's update feeds
project="$(cd "$(dirname "$0")/.." && pwd)"
version="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$project/role_radar/__init__.py")"
name="Role-Radar-$version-apple-silicon.dmg"
dmg="$project/dist/$name"
download="$project/dist/Role-Radar-apple-silicon.dmg"  # as in the README's Download button
update_name="Role-Radar-$version-update.dmg"
update="$project/dist/$update_name"
appcast="$project/dist/appcast.xml"
windows_name="Role-Radar-$version-windows-setup.exe"
setup="$project/dist/$windows_name"
windows_download="$project/dist/Role-Radar-windows-setup.exe"  # as in the README's Download for Windows button
windows_update_name="Role-Radar-$version-windows-update.exe"
windows_update="$project/dist/$windows_update_name"
windows_appcast="$project/dist/appcast-windows.xml"
notes="${1:-}"

[ -f "$dmg" ] || { echo "No $dmg: run scripts/package_app.sh first" >&2; exit 1; }
if [ "${WINDOWS:-1}" = 0 ]; then
    echo "WINDOWS=0: publishing the Mac app alone"
elif [ ! -f "$setup" ]; then
    echo "No $setup: run scripts/package_windows.sh first (or WINDOWS=0 to publish the Mac app alone)" >&2
    exit 1
fi
[ -z "$notes" ] || [ -f "$notes" ] || { echo "No such notes file: $notes" >&2; exit 1; }
sparkle="$(sh "$project/scripts/get_sparkle.sh")"

# Prints sparkle:edSignature="..." length="..." for a file, signed with the update key.
sign() {
    echo "Signing $(basename "$1")..." >&2
    if [ "${SPARKLE_KEY_FILE:-}" ]; then
        signature="$("$sparkle/bin/sign_update" --ed-key-file "$SPARKLE_KEY_FILE" "$1")"
    else
        signature="$("$sparkle/bin/sign_update" --account role-radar "$1")"
    fi
    case "$signature" in
        *edSignature=*length=*) echo "$signature" ;;
        *) echo "sign_update didn't sign it: $signature" >&2; exit 1 ;;
    esac
}

html=""
if [ -n "$notes" ]; then
    html="$(awk '
        /^[-*] / { if (!list) { print "<ul>"; list = 1 } sub(/^[-*] /, ""); print "<li>" $0 "</li>"; next }
        { if (list) { print "</ul>"; list = 0 } }
        NF { print "<p>" $0 "</p>" }
        END { if (list) print "</ul>" }' "$notes")"
fi
published="$(LC_ALL=C date -u "+%a, %d %b %Y %H:%M:%S +0000")"

cp "$dmg" "$download"
cp "$dmg" "$update"  # the same bytes, so the same signature
signature="$(sign "$dmg")"
cat > "$appcast" <<EOF
<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle">
  <channel>
    <title>Role Radar</title>
    <item>
      <title>Version $version</title>
      <pubDate>$published</pubDate>
      <sparkle:version>$version</sparkle:version>
      <sparkle:shortVersionString>$version</sparkle:shortVersionString>
      <sparkle:minimumSystemVersion>14.0</sparkle:minimumSystemVersion>
      <sparkle:hardwareRequirements>arm64</sparkle:hardwareRequirements>
      <description><![CDATA[$html]]></description>
      <enclosure url="https://github.com/$repo/releases/download/v$version/$update_name" type="application/octet-stream" $signature/>
    </item>
  </channel>
</rss>
EOF
echo "Wrote $appcast"
set -- "$download" "$update" "$appcast"  # what the release carries (the paths have spaces: kept apart)

if [ "${WINDOWS:-1}" != 0 ]; then
    cp "$setup" "$windows_download"
    cp "$setup" "$windows_update"
    signature="$(sign "$setup")"
    # WinSparkle runs the installer silently (/S): it quits the app, replaces it, and opens the new one.
    # Windows 10 version 1703 or later, as the README says.
    cat > "$windows_appcast" <<EOF
<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle">
  <channel>
    <title>Role Radar for Windows</title>
    <item>
      <title>Version $version</title>
      <pubDate>$published</pubDate>
      <sparkle:version>$version</sparkle:version>
      <sparkle:shortVersionString>$version</sparkle:shortVersionString>
      <sparkle:minimumSystemVersion>10.0.15063</sparkle:minimumSystemVersion>
      <description><![CDATA[$html]]></description>
      <enclosure url="https://github.com/$repo/releases/download/v$version/$windows_update_name" sparkle:os="windows"
                 sparkle:installerArguments="/S" type="application/octet-stream" $signature/>
    </item>
  </channel>
</rss>
EOF
    echo "Wrote $windows_appcast"
    set -- "$@" "$windows_download" "$windows_update" "$windows_appcast"
fi

if [ "${DRY_RUN:-}" ]; then
    echo "DRY_RUN: not published"
    exit 0
fi
if [ -n "$notes" ]; then
    gh release create "v$version" "$@" --repo "$repo" --title "Role Radar $version" --notes-file "$notes"
else
    gh release create "v$version" "$@" --repo "$repo" --title "Role Radar $version" --notes ""
fi
echo "Published v$version: copies of the app pick it up within six hours."
