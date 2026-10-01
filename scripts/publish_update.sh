#!/bin/sh
# Publish the version packaged by scripts/package_app.sh as an update: every copy of the app finds
# it within six hours (Sparkle), or at once with "Check for Updates…".
#
#   sh scripts/package_app.sh
#   sh scripts/publish_update.sh [NOTES.md]      # DRY_RUN=1 writes dist/appcast.xml and stops there
#
# Signs dist/Role-Radar-<version>-apple-silicon.dmg with the update key in this Mac's Keychain
# (made once with `generate_keys --account role-radar`; macOS asks to let sign_update use it),
# writes dist/appcast.xml naming it the newest version, and creates GitHub release v<version>
# with both (SPARKLE_KEY_FILE: sign with a key exported by `generate_keys -x` instead, on another Mac).
# Every copy reads the latest release's appcast.xml, so publish updates as normal
# releases, not pre-releases, and bump __version__ in role_radar/__init__.py for each one.
# NOTES.md (optional) becomes the release notes: "- " lines become a list, others paragraphs.
set -eu

repo="anupreetsingh/Role-Radar"  # as in package_app.sh's update_feed
project="$(cd "$(dirname "$0")/.." && pwd)"
version="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$project/role_radar/__init__.py")"
name="Role-Radar-$version-apple-silicon.dmg"
dmg="$project/dist/$name"
notes="${1:-}"
appcast="$project/dist/appcast.xml"

[ -f "$dmg" ] || { echo "No $dmg: run scripts/package_app.sh first" >&2; exit 1; }
[ -z "$notes" ] || [ -f "$notes" ] || { echo "No such notes file: $notes" >&2; exit 1; }
sparkle="$(sh "$project/scripts/get_sparkle.sh")"

echo "Signing $name..."
if [ "${SPARKLE_KEY_FILE:-}" ]; then
    signature="$("$sparkle/bin/sign_update" --ed-key-file "$SPARKLE_KEY_FILE" "$dmg")"
else
    signature="$("$sparkle/bin/sign_update" --account role-radar "$dmg")"
fi  # sparkle:edSignature="..." length="..."
case "$signature" in
    *edSignature=*length=*) ;;
    *) echo "sign_update didn't sign it: $signature" >&2; exit 1 ;;
esac

html=""
if [ -n "$notes" ]; then
    html="$(awk '
        /^[-*] / { if (!list) { print "<ul>"; list = 1 } sub(/^[-*] /, ""); print "<li>" $0 "</li>"; next }
        { if (list) { print "</ul>"; list = 0 } }
        NF { print "<p>" $0 "</p>" }
        END { if (list) print "</ul>" }' "$notes")"
fi

cat > "$appcast" <<EOF
<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle">
  <channel>
    <title>Role Radar</title>
    <item>
      <title>Version $version</title>
      <pubDate>$(LC_ALL=C date -u "+%a, %d %b %Y %H:%M:%S +0000")</pubDate>
      <sparkle:version>$version</sparkle:version>
      <sparkle:shortVersionString>$version</sparkle:shortVersionString>
      <sparkle:minimumSystemVersion>14.0</sparkle:minimumSystemVersion>
      <sparkle:hardwareRequirements>arm64</sparkle:hardwareRequirements>
      <description><![CDATA[$html]]></description>
      <enclosure url="https://github.com/$repo/releases/download/v$version/$name" type="application/octet-stream" $signature/>
    </item>
  </channel>
</rss>
EOF
echo "Wrote $appcast"

if [ "${DRY_RUN:-}" ]; then
    echo "DRY_RUN: not published"
    exit 0
fi
if [ -n "$notes" ]; then
    gh release create "v$version" "$dmg" "$appcast" --repo "$repo" --title "Role Radar $version" --notes-file "$notes"
else
    gh release create "v$version" "$dmg" "$appcast" --repo "$repo" --title "Role Radar $version" --notes ""
fi
echo "Published v$version: copies of the app pick it up within six hours."
