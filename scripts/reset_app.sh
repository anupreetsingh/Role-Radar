#!/bin/sh
# Reset the packaged app (package_app.sh) on this Mac to a first run, as someone new would see it:
# quits it, and removes its checker (com.roleradar.app.checker), its files in
# ~/Library/Application Support/Role Radar, its logs and its Keychain items. A Role Radar run
# from the code (its own agent, files and Keychain items) is never touched.
# "Open at Login" stays as it was: untick it in the app's menu if you don't want it.
set -eu

support="$HOME/Library/Application Support/Role Radar"

osascript -e 'tell application id "com.roleradar.app" to quit' >/dev/null 2>&1 || true
for _ in 1 2 3 4 5 6 7 8 9 10; do
    pgrep -qf "Role Radar.app/Contents/MacOS/RoleRadarMenu" || break
    sleep 1
done
launchctl bootout "gui/$(id -u)/com.roleradar.app.checker" >/dev/null 2>&1 || true
rm -f "$HOME/Library/LaunchAgents/com.roleradar.app.checker.plist"
rm -rf "$support"
rm -f "$HOME/Library/Logs/com.roleradar.app.checker.log"* "$HOME/Library/Logs/com.roleradar.app.checker.out.log"
for name in EMAIL_TO EMAIL_FROM SMTP_HOST SMTP_PORT SMTP_SECURITY SMTP_USERNAME SMTP_PASSWORD DISCORD_WEBHOOK_URL; do
    security delete-generic-password -s com.roleradar.app -a "$name" >/dev/null 2>&1 || true
done
echo "Reset the packaged app: its next launch is a first run."
