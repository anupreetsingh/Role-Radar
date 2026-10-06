"""The steps of scripts/package_windows.sh that are easier in Python, and the app's icons.

    python windows/build.py check FOLDER    every program and library there finds what it loads: in the
                                            folder, or part of Windows (a static check, from a Mac; needs pefile)
    python windows/build.py check-app       every text size and theme name the app (windows/RoleRadar) uses
                                            exists, so none of its windows fails to draw on Windows
    python windows/build.py icons           draw the app's icons (windows/RoleRadar/Assets: the app's, and the
                                            gray one the tray shows while nothing checks) from the Mac icon's
                                            glyph; needs Qt (uv run --with PySide6-Essentials ...)
"""

from __future__ import annotations

import re
import struct
import sys
from pathlib import Path

WINDOWS_FOLDER = Path(__file__).resolve().parent
APP = WINDOWS_FOLDER / "RoleRadar"
GLYPH = WINDOWS_FOLDER.parent / "macos" / "AppIcon.icon" / "Assets" / "glyph.svg"

# Libraries every Windows 10 (version 1703 on) and 11 has (lowercase): what a program may load without carrying it.
WINDOWS = {
    "advapi32.dll", "authz.dll", "bcrypt.dll", "cfgmgr32.dll", "comctl32.dll", "comdlg32.dll", "crypt32.dll",
    "d2d1.dll", "d3d11.dll", "d3d12.dll", "d3d9.dll", "dcomp.dll", "dnsapi.dll", "dwmapi.dll", "dwrite.dll", "dxgi.dll",
    "gdi32.dll", "gdiplus.dll", "imm32.dll", "iphlpapi.dll", "kernel32.dll", "mpr.dll", "msimg32.dll", "msvcrt.dll",
    "ncrypt.dll", "netapi32.dll", "ntdll.dll", "ole32.dll", "oleacc.dll", "oleaut32.dll", "opengl32.dll",
    "powrprof.dll", "propsys.dll", "rpcrt4.dll", "secur32.dll", "setupapi.dll", "shcore.dll", "shell32.dll",
    "shlwapi.dll", "ucrtbase.dll", "urlmon.dll", "user32.dll", "userenv.dll", "uxtheme.dll", "version.dll",
    "winhttp.dll", "wininet.dll", "winmm.dll", "winspool.drv", "wintrust.dll", "ws2_32.dll", "wtsapi32.dll",
    "dxva2.dll", "mfplat.dll", "wevtapi.dll", "msi.dll", "normaliz.dll", "cryptbase.dll", "sspicli.dll",
    "uiautomationcore.dll", "icu.dll", "icuuc.dll", "icuin.dll", "mscms.dll", "wldp.dll", "xmllite.dll",
    "dbghelp.dll", "psapi.dll", "msctf.dll", "combase.dll", "mscoree.dll", "windowscodecs.dll",
}


def check(folder: Path) -> list[str]:
    """What each .exe, .dll and .pyd loads that's neither part of Windows nor carried in the app (the same
    folder, or another in it). Empty means everything is there."""
    import pefile

    files = [p for p in folder.rglob("*") if p.suffix.lower() in (".exe", ".dll", ".pyd")]
    carried = {p.name.lower() for p in files}
    missing = []
    for path in files:
        pe = pefile.PE(str(path), fast_load=True)
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                                               pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"]])
        managed = pe.OPTIONAL_HEADER.DATA_DIRECTORY[14].VirtualAddress != 0  # .NET: any CPU, run as 64-bit
        if pe.FILE_HEADER.Machine != 0x8664 and not managed:
            missing.append(f"{path.relative_to(folder)}: not a 64-bit Intel/AMD file")
        imports = getattr(pe, "DIRECTORY_ENTRY_IMPORT", []) + getattr(pe, "DIRECTORY_ENTRY_DELAY_IMPORT", [])
        for entry in imports:
            name = entry.dll.decode().lower()
            if name in carried or name in WINDOWS or name.startswith(("api-ms-win-", "ext-ms-")):
                continue
            missing.append(f"{path.relative_to(folder)}: needs {name}")
        pe.close()
    return missing


def check_app() -> list[str]:
    """Every text size and theme resource the app's code and Styles.xaml name: the sizes TextSize offers,
    and keys Styles.xaml or WPF's Fluent theme defines. A missing one would leave text unsized or a style
    unapplied, which shows only once the app runs on Windows."""
    code = "\n".join(p.read_text(encoding="utf-8") for p in APP.rglob("*.cs") if not {"bin", "obj"} & set(p.parts))
    styles = (APP / "Styles.xaml").read_text(encoding="utf-8")
    ui = (APP / "Ui.cs").read_text(encoding="utf-8")
    sizes = {int(n) for n in re.search(r"Sizes = \[([^\]]+)\]", ui).group(1).replace(" ", "").split(",")}
    used = {int(n) for n in re.findall(r"\b(?:Size|Text|Secondary|Glyph|Button|Link)\((?:[^()]|\([^()]*\))*?,\s*(\d+)\s*[,)]", code)}
    used |= {int(n) for n in re.findall(r"\.Size\((\d+)", code)} | {int(n) for n in re.findall(r"RR\.Size\.(\d+)", styles)}
    problems = [f"text size {size} isn't one TextSize offers (Ui.cs)" for size in sorted(used - sizes)]
    theme = dict(re.findall(r'public const string (\w+) = "([^"]+)"', ui))
    keys = set(re.findall(r'SetResourceReference\([^,]+,\s*"([^"]+)"\)', code))
    keys |= {theme[name] for name in re.findall(r"Theme\.(\w+)", code) if name in theme}
    keys |= set(re.findall(r"(?:Static|Dynamic)Resource ([\w.]+)", styles))
    defined = set(re.findall(r'x:Key="([^"]+)"', styles)) | {f"RR.Size.{size}" for size in sizes}
    fluent = _fluent_theme()
    problems += [f"no resource {key!r}" for key in sorted(keys) if key not in defined and key.encode() not in fluent]
    return problems


def _fluent_theme() -> bytes:
    """WPF's Fluent theme (it names every Windows 11 brush and style): as `dotnet publish -r win-x64` restores
    it (scripts/package_windows.sh), or as .NET installs it on Windows."""
    import shutil

    found = sorted(Path.home().glob(".nuget/packages/microsoft.windowsdesktop.app.runtime.win-x64/*/runtimes/win-x64/lib/"
                                    "net10.0/PresentationFramework.Fluent.dll"))
    if dotnet := shutil.which("dotnet"):
        found += sorted(Path(dotnet).resolve().parent.glob("shared/Microsoft.WindowsDesktop.App/10.*/PresentationFramework.Fluent.dll"))
    if not found:
        sys.exit("No WPF Fluent theme found: publish the app first (scripts/package_windows.sh does)")
    return found[-1].read_bytes()


# -- the icons ------------------------------------------------------------------------------------------


SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def _image(size: int, gray: bool):
    """The icon at `size` pixels square: the Mac icon's glyph on its teal gradient (gray for the tray while
    nothing checks), in Windows 11's rounded square."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter
    from PySide6.QtSvg import QSvgRenderer

    picture = QImage(size, size, QImage.Format.Format_ARGB32)
    picture.fill(Qt.GlobalColor.transparent)
    painter = QPainter(picture)
    painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
    margin = 0 if size <= 24 else size * 0.06  # at tray sizes the square fills the space, so the glyph stays big
    square = QRectF(margin, margin, size - 2 * margin, size - 2 * margin)
    gradient = QLinearGradient(square.topLeft(), square.bottomLeft())
    gradient.setColorAt(0, QColor("#8A9599") if gray else QColor.fromRgbF(0.10, 0.73, 0.66))
    gradient.setColorAt(1, QColor("#5C676B") if gray else QColor.fromRgbF(0.05, 0.46, 0.43))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(gradient)
    painter.drawRoundedRect(square, square.width() * 0.22, square.width() * 0.22)
    glyph = square.width() * (1.25 if size <= 24 else 1.0)  # a little bigger at small sizes, to stay legible
    QSvgRenderer(str(GLYPH)).render(painter, QRectF(square.center().x() - glyph / 2, square.center().y() - glyph / 2, glyph, glyph))
    painter.end()
    return picture


def _entry(size: int, gray: bool) -> bytes:
    """One size in an .ico: a 32-bit bitmap, as every Windows API reads them; 256 as a PNG, as Windows does."""
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice

    picture = _image(size, gray)
    if size == 256:
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        picture.save(buffer, "PNG")
        return bytes(data.data())
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    rows = [bytes(picture.constScanLine(y))[: size * 4] for y in range(size)]  # BGRA, top to bottom
    mask = b"\x00" * (((size + 31) // 32) * 4 * size)  # the alpha channel decides; nothing masked
    return header + b"".join(reversed(rows)) + mask


def ico(path: Path, gray: bool = False) -> None:
    entries = [(size, _entry(size, gray)) for size in SIZES]
    out = struct.pack("<HHH", 0, 1, len(entries))
    offset = 6 + 16 * len(entries)
    for size, data in entries:
        out += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    path.write_bytes(out + b"".join(data for _, data in entries))


def icons() -> None:
    from PySide6.QtGui import QGuiApplication

    QGuiApplication.instance() or QGuiApplication(["icons", "-platform", "offscreen"])
    ico(APP / "Assets" / "AppIcon.ico")
    ico(APP / "Assets" / "AppIconGray.ico", gray=True)


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action == "check" and len(sys.argv) == 3:
        found = check(Path(sys.argv[2]))
    elif action == "check-app":
        found = check_app()
    elif action == "icons":
        icons()
        found = []
    else:
        sys.exit(__doc__)
    for problem in found:
        print(f"  {problem}", file=sys.stderr)
    sys.exit(1 if found else 0)
