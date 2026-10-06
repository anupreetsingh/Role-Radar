#!/bin/sh
# Run the Windows app (windows/role_radar_app) from the working tree, for trying changes out: on
# Windows (Git Bash) or on a Mac, where it runs as it would on Windows, in the menu bar.
# It's "Role Radar Dev" ("Role Radar Windows Dev" on a Mac): an app of its own, with its own files
# (%LOCALAPPDATA%\Role Radar Dev, or ~/Library/Application Support/Role Radar Windows Dev), checker
# and credentials, so an installed Role Radar is never touched. It checks no job sites, so this
# computer's requests aren't doubled: RR_DEV_CHECKS=1 for one that does.
# Needs the project's .venv (uv venv && uv pip install -e .); it adds Qt (windows/requirements.txt)
# if it's missing, and writes the professions' company lists to role_radar/lists/ (git-ignored).
set -eu

project="$(cd "$(dirname "$0")/.." && pwd)"
python="$project/.venv/bin/python"
[ -x "$python" ] || python="$project/.venv/Scripts/python.exe"  # Windows
[ -x "$python" ] || { echo "Needs the project's .venv (uv venv && uv pip install -e .)" >&2; exit 1; }

"$python" -c "import PySide6" 2>/dev/null || uv pip install --quiet --python "$python" -r "$project/windows/requirements.txt"
"$python" "$project/scripts/write_lists.py" "$project/role_radar/lists" >/dev/null
PYTHONPATH="$project/windows${PYTHONPATH:+:$PYTHONPATH}" exec "$python" -m role_radar_app "$@"
