#!/usr/bin/env python3
"""Build fonts/Rubik-Bold.ttf: Rubik Bold with Inter Bold's ★ added.

    docker run --rm -v "$PWD":/app -w /app python:3.14-slim \
        sh -c "pip install -q fonttools && python3 tools/build_rubik_font.py"

Rubik is the label font for languages Inter has no glyphs for (Hebrew), and
the labels draw "★ 87" in the same font as the words, but Rubik has no ★.  The
star is copied from Inter Bold, scaled so it stands as tall against Rubik's
capitals as it does against Inter's, and given Inter's side bearings in
proportion.  Both fonts are under the SIL Open Font License 1.1 (see
fonts/OFL-Rubik.txt); neither declares a Reserved Font Name, so the modified
font keeps its name.

Needs fontTools, which the image doesn't ship — run it in a throwaway
container as above.
"""
import io
import os
import urllib.request

from fontTools.pens.transformPen import TransformPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

# Google Fonts' static Bold (700) instance of Rubik v31.
RUBIK_URL = "https://fonts.gstatic.com/s/rubik/v31/iJWZBXyIfDnIV5PNhY1KTN7Z-Yh-4I-1UA.ttf"
STAR = 0x2605

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONTS = os.path.join(ROOT, "fonts")


def main() -> None:
    with urllib.request.urlopen(RUBIK_URL) as resp:
        rubik = TTFont(io.BytesIO(resp.read()))
    inter = TTFont(os.path.join(FONTS, "Inter-Bold.ttf"))

    src_name = inter.getBestCmap()[STAR]
    k = rubik["OS/2"].sCapHeight / inter["OS/2"].sCapHeight
    inter_gs = inter.getGlyphSet()
    pen = TTGlyphPen(None)
    inter_gs[src_name].draw(TransformPen(pen, (k, 0, 0, k, 0, 0)))
    glyph = pen.glyph()

    name = "uni2605"
    rubik["glyf"][name] = glyph   # appends to the glyph order; maxp recounts on save
    glyph.recalcBounds(rubik["glyf"])
    adv, _lsb = inter["hmtx"][src_name]
    rubik["hmtx"][name] = (round(adv * k), glyph.xMin)
    for table in rubik["cmap"].tables:
        if table.isUnicode():
            table.cmap[STAR] = name

    out = os.path.join(FONTS, "Rubik-Bold.ttf")
    rubik.save(out)
    print(f"wrote {out} (★ scaled {k:.3f} from Inter)")


if __name__ == "__main__":
    main()
