"""Write each profession's company list where the app reads it (config.profession_list): Tech's is
config/companies.yaml, the others sit beside it. Each written file keeps the companies and the
countries they post jobs in, without anyone's filters. A profession with no list yet has none written.

    python scripts/write_lists.py TARGET_DIR

Run by scripts/package_app.sh and scripts/package_windows.sh (into the app's role_radar/lists),
and by scripts/build_dev_app.sh and scripts/run_windows_app.sh (into the repo's, git-ignored).
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

CONFIG = Path(__file__).resolve().parent.parent / "config"
LISTS = {"tech": "companies.yaml", "accounting": "accounting.yaml", "healthcare": "healthcare.yaml"}


def write(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    for profession, name in LISTS.items():
        path = target / f"{profession}.yaml"
        if not (CONFIG / name).exists():
            path.unlink(missing_ok=True)
            print(f"  {profession}: no list yet")
            continue
        companies = yaml.load((CONFIG / name).read_text(encoding="utf-8"), Loader=loader)["companies"]
        keep = [{k: v for k, v in c.items() if k != "filters"} for c in companies]
        with path.open("w", encoding="utf-8") as fh:
            fh.write(f"# Role Radar's {profession} companies, with the countries each posts jobs in; built from config/{name}.\n")
            yaml.safe_dump({"companies": keep}, fh, sort_keys=False, allow_unicode=True, width=200)
        on = sum(c.get("enabled", True) for c in keep)
        print(f"  {profession}: {on} companies" + (f" ({len(keep)} with those switched off)" if on != len(keep) else ""))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python scripts/write_lists.py TARGET_DIR")
    write(Path(sys.argv[1]))
