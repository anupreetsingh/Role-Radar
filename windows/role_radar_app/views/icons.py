"""Role Radar's icon, drawn as the Mac app's is (macos/AppIcon.icon): its white glyph, a dot between
radio waves, on a teal gradient. The tray shows it in gray while nothing is checking.

The glyph is the Mac icon's own glyph.svg: the installed app carries a copy beside this package
(scripts/package_windows.sh), and a dev run reads the repo's.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QLinearGradient, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

PACKAGE = Path(__file__).resolve().parent.parent
GLYPH = next((path for path in (PACKAGE / "glyph.svg", PACKAGE.parents[1] / "macos" / "AppIcon.icon" / "Assets" / "glyph.svg")
              if path.exists()), PACKAGE / "glyph.svg")
TOP, BOTTOM = QColor.fromRgbF(0.10, 0.73, 0.66), QColor.fromRgbF(0.05, 0.46, 0.43)  # the Mac icon's gradient
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def image(size: int, gray: bool = False) -> QImage:
    """The icon at `size` pixels square: a rounded square (Windows 11's shape) holding the glyph."""
    picture = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    picture.fill(Qt.GlobalColor.transparent)
    painter = QPainter(picture)
    painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
    # At tray sizes the square fills the space, so the glyph stays as big as it can be.
    margin = 0 if size <= 24 else size * 0.06
    square = QRectF(margin, margin, size - 2 * margin, size - 2 * margin)
    gradient = QLinearGradient(square.topLeft(), square.bottomLeft())
    gradient.setColorAt(0, QColor("#8A9599") if gray else TOP)
    gradient.setColorAt(1, QColor("#5C676B") if gray else BOTTOM)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(gradient)
    radius = square.width() * 0.22
    painter.drawRoundedRect(square, radius, radius)
    # The glyph's waves span most of its 1024 square: scaled up a little at small sizes, to stay legible.
    zoom = 1.25 if size <= 24 else 1.0
    glyph = square.width() * zoom
    renderer = QSvgRenderer(str(GLYPH))
    renderer.render(painter, QRectF(square.center().x() - glyph / 2, square.center().y() - glyph / 2, glyph, glyph))
    painter.end()
    return picture


@cache
def app_icon(gray: bool = False) -> QIcon:
    icon = QIcon()
    for size in SIZES:
        icon.addPixmap(QPixmap.fromImage(image(size, gray)))
    return icon


def ico(path: Path) -> None:
    """Write the icon as a Windows .ico, every size a PNG inside (as Windows has read them since Vista):
    for Role Radar.exe, its installer and its shortcuts."""
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice

    images = []
    for size in SIZES:
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image(size).save(buffer, "PNG")
        images.append((size, bytes(data.data())))
    header = (0).to_bytes(2, "little") + (1).to_bytes(2, "little") + len(images).to_bytes(2, "little")
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for size, png in images:
        side = size % 256  # 256 is written as 0
        entries += bytes([side, side, 0, 0]) + (1).to_bytes(2, "little") + (32).to_bytes(2, "little")
        entries += len(png).to_bytes(4, "little") + offset.to_bytes(4, "little")
        offset += len(png)
        blobs += png
    path.write_bytes(header + entries + blobs)
