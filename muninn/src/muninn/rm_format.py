"""Parse reMarkable `.rm` page files into vector strokes.

Supports v5 (RM1 native) and v6 (newer firmware). The v5 parser is vendored
inline (~30 lines of struct.unpack) because the upstream `rmlines` library is
archived and not on PyPI, and its pillow pin conflicts with rmrl. v6 parsing
delegates to `rmscene`.

Strokes are returned as lists of (x, y, pressure) tuples in a top-left-origin
screen-pixel coordinate space (1404 × 1872, 226 DPI).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

X_MAX, Y_MAX = 1404.0, 1872.0
X_MAX_INT, Y_MAX_INT = int(X_MAX), int(Y_MAX)


@dataclass
class Stroke:
    points: list[tuple[float, float, float]]  # (x, y, pressure ∈ [0, 1])


def parse_strokes(rm_path: Path) -> list[Stroke]:
    """Parse a `.rm` file (v5 or v6) into strokes.

    Raises ValueError on unknown/unsupported format headers.
    """
    data = rm_path.read_bytes()
    header = data[:43]
    if b"version=5" in header:
        return _parse_v5(data)
    if b"version=6" in header:
        return _parse_v6(data)
    raise ValueError(f"Unsupported .rm format header: {header!r}")


def _parse_v5(data: bytes) -> list[Stroke]:
    """Vendored v5 parser. Layout: 43-byte header + uint32 nlayers + per-layer
    (uint32 nstrokes + per-stroke (pen, color, _pad, width, _pad, nsegs as
    little-endian IIIfI) + per-segment 6 floats (x, y, speed, tilt, width,
    pressure)).
    """
    offset = 43
    strokes: list[Stroke] = []

    (n_layers,) = struct.unpack_from("<I", data, offset)
    offset += 4
    for _ in range(n_layers):
        (n_strokes,) = struct.unpack_from("<I", data, offset)
        offset += 4
        for _ in range(n_strokes):
            # pen, colour, _, width, _padding
            struct.unpack_from("<IIIfI", data, offset)
            offset += struct.calcsize("<IIIfI")
            (n_segs,) = struct.unpack_from("<I", data, offset)
            offset += 4
            points: list[tuple[float, float, float]] = []
            for _ in range(n_segs):
                x, y, _speed, _tilt, _width, pressure = struct.unpack_from(
                    "ffffff", data, offset
                )
                offset += struct.calcsize("ffffff")
                # v5 coords are already in 0..1404 / 0..1872 top-left space.
                # v5 pressure is float in [0, 1].
                points.append((x, y, float(pressure)))
            if points:
                strokes.append(Stroke(points=points))
    return strokes


def render_to_png(
    rm_path: Path,
    *,
    stroke_width: int = 3,
    max_long_edge: int = 2576,
) -> bytes:
    """Rasterize strokes from a `.rm` file onto a white canvas; return PNG bytes.

    Canvas starts at the native 1404 × 1872 page and grows vertically to fit
    strokes that extend past the default viewport (rM1 "scrolled" pages can
    reach y > 1872). The result is downscaled if either edge would exceed
    `max_long_edge` — Claude vision currently tops out at 2576 on Opus 4.7.
    Strokes are solid black, constant width — pressure is ignored.
    """
    from PIL import Image, ImageDraw

    strokes = parse_strokes(rm_path)

    width = X_MAX_INT
    height = Y_MAX_INT
    if strokes:
        max_y = max(p[1] for s in strokes for p in s.points)
        if max_y > Y_MAX:
            height = int(max_y) + 50  # small bottom margin

    img = Image.new("RGB", (width, height), (255, 255, 255))
    if strokes:
        draw = ImageDraw.Draw(img)
        for s in strokes:
            pts = [(p[0], p[1]) for p in s.points]
            if len(pts) < 2:
                x, y = pts[0]
                r = stroke_width / 2
                draw.ellipse((x - r, y - r, x + r, y + r), fill=(0, 0, 0))
                continue
            draw.line(pts, fill=(0, 0, 0), width=stroke_width, joint="curve")

    long_edge = max(width, height)
    if long_edge > max_long_edge:
        scale = max_long_edge / long_edge
        img = img.resize(
            (max(1, int(width * scale)), max(1, int(height * scale))),
            Image.LANCZOS,
        )

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _parse_v6(data: bytes) -> list[Stroke]:
    """v6 parser via rmscene. v6 stores coords in a page-centered space
    (x ∈ ~[-702, 702], y ∈ ~[0, 1872]) and pressure as a uint8 (0..255 nominally,
    sometimes higher in practice). Translate x to top-left origin; normalize
    pressure to [0, 1].
    """
    import io

    import rmscene
    from rmscene import scene_items

    strokes: list[Stroke] = []
    x_offset = X_MAX / 2.0  # v6 x is centered on the page; shift to top-left

    for block in rmscene.read_blocks(io.BytesIO(data)):
        if not isinstance(block, rmscene.SceneLineItemBlock):
            continue
        line = block.item.value
        if not isinstance(line, scene_items.Line):
            continue
        pts: list[tuple[float, float, float]] = []
        for p in line.points:
            x = p.x + x_offset
            y = p.y
            # v6 pressure can exceed 255 in practice; clamp + normalize to [0,1].
            pressure = max(0.0, min(1.0, p.pressure / 255.0))
            pts.append((x, y, pressure))
        if pts:
            strokes.append(Stroke(points=pts))
    return strokes
