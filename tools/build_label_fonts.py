#!/usr/bin/env python3
"""Build label fonts: a family's Bold from Google Fonts, with Inter Bold's ★
added where the family has none.

    docker run --rm -v "$PWD":/app -w /app python:3.11-slim \
        sh -c "pip install -q fonttools && python3 tools/build_label_fonts.py [OUT_DIR] [FAMILY ...]"

Almost no Google Fonts family has the ★ the labels draw; fontprep's
add_label_symbols copies Inter Bold's in (and any separator a family lacks), and strip_heavy_hinting takes out the per-glyph
hinting that made some families (Fira Sans, Barlow Condensed) ~9x slower to
draw.  The families here are under the SIL Open Font License 1.1 and declare
no Reserved Font Name, so a modified font keeps its name.

OUT_DIR defaults to fonts/; FAMILY defaults to every entry in FAMILIES.  Needs
fontTools, which the image doesn't ship — run it in a throwaway container.
"""
import io
import os
import re
import sys
import urllib.parse
import urllib.request

from fontTools.ttLib import TTFont

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fontprep import add_label_symbols, strip_heavy_hinting  # noqa: E402

# Google Fonts family → output file.
FAMILIES: dict[str, str] = {
    "Plus Jakarta Sans": "PlusJakartaSans-Bold.ttf",
    "Manrope": "Manrope-Bold.ttf",
    "Montserrat": "Montserrat-Bold.ttf",
    "Roboto Condensed": "RobotoCondensed-Bold.ttf",
    "Barlow Condensed": "BarlowCondensed-Bold.ttf",
    "Oswald": "OswaldLabel-Bold.ttf",   # Oswald-Bold.ttf is the title font, left as is
    "Space Grotesk": "SpaceGrotesk-Bold.ttf",
    "Exo 2": "Exo2-Bold.ttf",
    "Fira Sans": "FiraSans-Bold.ttf",
    "Open Sans": "OpenSans-Bold.ttf",
    "Almarai": "Almarai-Bold.ttf",
}

# An old browser's UA gets one static TrueType file per family from the CSS
# API, rather than per-script WOFF2 subsets.
_UA = "Mozilla/5.0 (Windows NT 6.1) AppleWebKit/534.30"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONTS = os.path.join(ROOT, "fonts")


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req) as resp:
        return resp.read()


def download_bold(family: str) -> TTFont:
    css = _fetch("https://fonts.googleapis.com/css2?family="
                 + urllib.parse.quote_plus(family) + ":wght@700").decode()
    urls = re.findall(r"url\((https://[^)]+\.ttf)\)", css)
    if len(urls) != 1:
        raise RuntimeError(f"{family}: expected one .ttf in the CSS, got {urls}")
    return TTFont(io.BytesIO(_fetch(urls[0])))


def main() -> None:
    out_dir = sys.argv[1] if len(sys.argv) > 1 else FONTS
    wanted = sys.argv[2:] or list(FAMILIES)
    inter = TTFont(os.path.join(FONTS, "Inter-Bold.ttf"))
    os.makedirs(out_dir, exist_ok=True)
    for family in wanted:
        font = download_bold(family)
        stripped = strip_heavy_hinting(font)
        added = add_label_symbols(font, inter)
        out = os.path.join(out_dir, FAMILIES[family])
        font.save(out)
        note = f"{' '.join(added)} added from Inter" if added else "has every label symbol"
        print(f"wrote {out} ({note}{', hinting stripped' if stripped else ''})")


if __name__ == "__main__":
    main()
