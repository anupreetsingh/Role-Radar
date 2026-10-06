"""Role Radar for Windows: the tray app, with Setup and Live Tracking. The Mac app's counterpart
(macos/RoleRadarMenu.swift), sharing everything else: the checker, its rules and its files are
role_radar's, and every read and write goes through the role-radar command line (cli.py), as on
the Mac, so the rules live in one place.

  Tray icon     in the notification area while the app runs: click it for the panel (the checker's
                and the alerts' switches, the round, Live Tracking), right-click for its menu
  The window    Setup's pages until they're done (or asked for again), otherwise Live Tracking:
                the new jobs, newest first, those marked as seen, the alerts sent, the round and
                the last 24 hours' activity
  The checker   `role-radar start` as a background process of its own (role_radar/winchecker.py):
                the app starts it, restarts it within a minute if it stops, and stops it on quit
  At login      it opens in the tray (Open at Login, on once Setup is done) and starts checking
  Updates       WinSparkle (updates.py) reads the latest GitHub release's appcast-windows.xml
                every six hours, and installs an update only if it's signed with the update key

Its files live in %LOCALAPPDATA%\\Role Radar, the alert settings in Windows Credential Manager,
and the app itself in %LOCALAPPDATA%\\Programs\\Role Radar (scripts/package_windows.sh builds the
installer). From the repo, scripts/run_windows_app.sh runs it as "Role Radar Dev" (place.py), an
app of its own that checks no job sites, on Windows or on a Mac.
"""
