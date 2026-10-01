#!/bin/sh
# Reset the dev build (build_dev_app.sh) on this Mac to a first run, as someone new would see it:
# quits it, and removes its checker (com.roleradar.app.dev.checker), its files in
# ~/Library/Application Support/Role Radar Dev, its logs and its Keychain items.
# With --installed, the downloaded app (package_app.sh) instead: com.roleradar.app and its files in
# ~/Library/Application Support/Role Radar, so the settings and job history of a copy in use go too.
# A Role Radar run from the code (its own agent, files and Keychain items) is never touched.
# "Open at Login" stays as it was: untick it in the app's menu if you don't want it.
set -eu

if [ "${1:-}" = "--installed" ]; then
    id=com.roleradar.app name="Role Radar"
else
    id=com.roleradar.app.dev name="Role Radar Dev"
fi
support="$HOME/Library/Application Support/$name"

osascript -e "tell application id \"$id\" to quit" >/dev/null 2>&1 || true
for _ in 1 2 3 4 5 6 7 8 9 10; do
    pgrep -qf "$name.app/Contents/MacOS/RoleRadarMenu" || break
    sleep 1
done
launchctl bootout "gui/$(id -u)/$id.checker" >/dev/null 2>&1 || true
rm -f "$HOME/Library/LaunchAgents/$id.checker.plist"
rm -rf "$support"
rm -f "$HOME/Library/Logs/$id.checker.log"* "$HOME/Library/Logs/$id.checker.out.log"
for key in EMAIL_TO EMAIL_FROM SMTP_HOST SMTP_PORT SMTP_SECURITY SMTP_USERNAME SMTP_PASSWORD DISCORD_WEBHOOK_URL; do
    security delete-generic-password -s "$id" -a "$key" >/dev/null 2>&1 || true
done
echo "Reset $name: its next launch is a first run."
