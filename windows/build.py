"""The steps of scripts/package_windows.sh that are easier in Python:

    python windows/build.py icon OUT.ico     Role Radar's icon as a Windows .ico (views/icons.py)
    python windows/build.py prune SITE       leave only the parts of Qt the app uses, in its site-packages
    python windows/build.py check FOLDER     every program and library there finds what it loads:
                                             in the folder, or part of Windows (a static check, from a Mac)
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

# The Qt the app uses: its modules, the plugins Windows needs (the platform, the Windows 11 style,
# .ico and .svg images, SVG icons), and the C++ runtime they're built with.
QT_KEEP = {
    "__init__.py", "_config.py", "_git_pyside_version.py", "support",
    "QtCore.pyd", "QtGui.pyd", "QtWidgets.pyd", "QtNetwork.pyd", "QtSvg.pyd",
    "pyside6.abi3.dll", "Qt6Core.dll", "Qt6Gui.dll", "Qt6Widgets.dll", "Qt6Network.dll", "Qt6Svg.dll",
    "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "msvcp140_codecvt_ids.dll", "concrt140.dll",
    "vcruntime140.dll", "vcruntime140_1.dll", "plugins",
}
QT_PLUGINS = {"platforms/qwindows.dll", "styles/qmodernwindowsstyle.dll", "imageformats/qico.dll",
              "imageformats/qsvg.dll", "iconengines/qsvgicon.dll"}
# Shiboken (Qt's bindings) carries the whole C++ runtime: these parts of it nothing here uses.
SHIBOKEN_DROP = {"include", "lib", "vcamp140.dll", "vccorlib140.dll", "vcomp140.dll"}

# Libraries every Windows 10 and 11 has (lowercase): what a program may load without carrying it.
WINDOWS = {
    "advapi32.dll", "authz.dll", "bcrypt.dll", "cfgmgr32.dll", "comctl32.dll", "comdlg32.dll", "crypt32.dll",
    "d2d1.dll", "d3d11.dll", "d3d12.dll", "d3d9.dll", "dcomp.dll", "dnsapi.dll", "dwmapi.dll", "dwrite.dll", "dxgi.dll",
    "gdi32.dll", "gdiplus.dll", "imm32.dll", "iphlpapi.dll", "kernel32.dll", "mpr.dll", "msimg32.dll", "msvcrt.dll",
    "ncrypt.dll", "netapi32.dll", "ntdll.dll", "ole32.dll", "oleacc.dll", "oleaut32.dll", "opengl32.dll",
    "powrprof.dll", "propsys.dll", "rpcrt4.dll", "secur32.dll", "setupapi.dll", "shcore.dll", "shell32.dll",
    "shlwapi.dll", "ucrtbase.dll", "urlmon.dll", "user32.dll", "userenv.dll", "uxtheme.dll", "version.dll",
    "winhttp.dll", "wininet.dll", "winmm.dll", "winspool.drv", "wintrust.dll", "ws2_32.dll", "wtsapi32.dll",
    "dxva2.dll", "mfplat.dll", "wevtapi.dll", "msi.dll", "normaliz.dll", "cryptbase.dll", "sspicli.dll",
    "uiautomationcore.dll",  # accessibility (Qt's platform plugin)
    "icu.dll", "icuuc.dll", "icuin.dll",  # Unicode, part of Windows since Windows 10 version 1703 (Qt uses it)
}


def icon(out: Path) -> None:
    from PySide6.QtGui import QGuiApplication

    sys.path.insert(0, str(Path(__file__).parent))
    from role_radar_app.views import icons

    QGuiApplication.instance() or QGuiApplication(["build"])
    icons.ico(out)


def prune(site: Path) -> None:
    qt = site / "PySide6"
    for path in qt.iterdir():
        if path.name not in QT_KEEP:
            shutil.rmtree(path) if path.is_dir() else path.unlink()
    for path in sorted((qt / "plugins").rglob("*.dll")):
        if path.relative_to(qt / "plugins").as_posix() not in QT_PLUGINS:
            path.unlink()
    for folder in sorted((qt / "plugins").iterdir()):
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    for path in (site / "shiboken6").iterdir():
        if path.name in SHIBOKEN_DROP or path.suffix in (".lib", ".pyi"):
            shutil.rmtree(path) if path.is_dir() else path.unlink()
    for path in site.rglob("*.pyi"):
        path.unlink()


def check(folder: Path) -> list[str]:
    """What each .exe, .dll and .pyd loads that's neither part of Windows nor carried in the app: from the
    same folder (or Python's), or for Qt's, its own. Empty means everything is there."""
    import pefile

    files = [p for p in folder.rglob("*") if p.suffix.lower() in (".exe", ".dll", ".pyd")]
    carried = {p.name.lower() for p in files}
    missing = []
    for path in files:
        pe = pefile.PE(str(path), fast_load=True)
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                                               pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"]])
        if pe.FILE_HEADER.Machine != 0x8664:  # x64
            missing.append(f"{path.relative_to(folder)}: not a 64-bit Intel/AMD file")
        imports = getattr(pe, "DIRECTORY_ENTRY_IMPORT", []) + getattr(pe, "DIRECTORY_ENTRY_DELAY_IMPORT", [])
        for entry in imports:
            name = entry.dll.decode().lower()
            if name in carried or name in WINDOWS or name.startswith(("api-ms-win-", "ext-ms-")):
                continue
            missing.append(f"{path.relative_to(folder)}: needs {name}")
        pe.close()
    return missing


if __name__ == "__main__":
    action, where = sys.argv[1], Path(sys.argv[2])
    if action == "icon":
        icon(where)
    elif action == "prune":
        prune(where)
    elif action == "check":
        problems = check(where)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        sys.exit(1 if problems else 0)
    else:
        sys.exit(__doc__)
