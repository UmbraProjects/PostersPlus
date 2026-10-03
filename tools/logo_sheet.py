#!/usr/bin/env python3
"""Every network and studio logo the instance has downloaded, on one sheet,
drawn as the graphic badges draw them.

    docker exec postersplus python3 tools/logo_sheet.py [row_height]

Writes /app/cache/logo_sheet.png (the cache volume, so it is reachable from
the host).  Each logo sits between two guide lines a row apart, at the row
height a 22-size group has on a 750-tall poster unless one is given, with
the downloaded original beside it (on grey, so white and black logos both
show).  It is labelled with its kind and TMDB id, its drawn size, its ink
share and the ink it lays down: the numbers graphic_badges.logo_size is
tuned on.  Sorted by aspect, so the shapes that come out small or large sit
together.
"""
from __future__ import annotations

import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

import graphic_badges as gb  # noqa: E402

OUT = "/app/cache/logo_sheet.png"


def main() -> int:
    row = int(sys.argv[1]) if len(sys.argv) > 1 else round(gb.DEFAULT_SIZE * 1.5)
    entries = []
    for path in sorted(glob.glob(os.path.join(gb.LOGO_DIR, "*.png"))):
        try:
            a = gb.logo_alpha(Image.open(path))
        except Exception as exc:
            print(f"skipped {os.path.basename(path)}: {exc}")
            continue
        if a.shape[0] < 2:
            print(f"skipped {os.path.basename(path)}: no ink")
            continue
        entries.append((a.shape[1] / a.shape[0], os.path.basename(path), a, path))
    if not entries:
        print(f"no logos in {gb.LOGO_DIR}")
        return 1
    entries.sort(key=lambda e: e[0])

    line_h, col_w, cols = row * 2 + 24, 900, 2
    rows = (len(entries) + cols - 1) // cols
    sheet = Image.new("RGB", (col_w * cols, rows * line_h + 20), (40, 44, 52))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for i, (aspect, name, a, path) in enumerate(entries):
        x0, y0 = (i % cols) * col_w, 10 + (i // cols) * line_h
        cy = y0 + line_h / 2
        fill = float(a.mean()) / 255
        w, h = gb.logo_size(a.shape, row, fill)
        m = Image.fromarray(a).resize((w, h), Image.Resampling.LANCZOS)
        for gy in (cy - row / 2, cy + row / 2):
            draw.line((x0 + 10, gy, x0 + col_w - 10, gy), fill=(80, 84, 92))
        sheet.paste((255, 255, 255), (x0 + 220, int(round(cy - h / 2))), m)
        # The original as downloaded, the same height, on a grey swatch.
        orig = Image.open(path).convert("RGBA")
        orig.thumbnail((round(row * 3), round(row * 1.6)), Image.Resampling.LANCZOS)
        ox = x0 + col_w - orig.width - 20
        sheet.paste((128, 128, 128), (ox - 4, int(cy - orig.height / 2) - 4,
                                      ox + orig.width + 4, int(cy + orig.height / 2) + 4))
        sheet.paste(orig, (ox, int(cy - orig.height / 2)), orig)
        kind, ident = name.split("_")[:2]
        draw.text((x0 + 10, cy - 14), f"{kind} {ident}", font=font, fill=(200, 200, 200))
        draw.text((x0 + 10, cy + 2), f"{w}x{h}  aspect {aspect:.1f}", font=font, fill=(150, 150, 150))
        draw.text((x0 + 220 + round(row * gb._LOGO_MAX_W) + 10, cy - 6),
                  f"ink share {fill:.2f}  ink {fill * w * h:.0f}", font=font, fill=(150, 150, 150))
    sheet.save(OUT)
    print(f"{len(entries)} logos at row height {row} -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
