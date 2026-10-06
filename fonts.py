# fonts.py
"""The font poster labels are drawn in, and fallback by script.

Labels — the genre / rating line, the sash and notch, the Bar, the landscape
info line and badge, the trending ribbon's caption — are drawn in the *label
font*.  Inter is the default; Rubik is the one that has Hebrew, Almarai the
one that has Arabic, Persian and Urdu; the rest are
Google Fonts families with Inter's ★ added (tools/build_label_fonts.py), and an
operator can upload more (custom_fonts, keys "custom-…").  A choice that has
no glyphs for the poster's language falls back to the first shipped label font
that does, so a Hebrew poster is always drawn in Rubik, an Arabic-script one
in Almarai (unless Rubik, which has Arabic and Persian, is picked) and a Greek
or Vietnamese one in Inter.

Arabic-script posters (i18n.needs_shaping) lay labels out with Pillow's raqm
— HarfBuzz joins the letters, FriBiDi orders the line — when it is available
(libfribidi installed).  Every other render, and every font outside the label
font, keeps the basic layout, so their text is drawn exactly as before.

The label font is set around a render by build_poster / build_landscape
(``label_font_scope``), a ContextVar for the same reason pxscale's scale is
one: renders run on thread-pool threads, and each sees only its own.  Outside a
render it is Inter.

Numerals and codes that are never translated (quality and age badges, the
trending numeral, rating badge logos) stay in Inter.
"""
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache

from PIL import ImageFont, features

import custom_fonts
import i18n

FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

# label_font choice → file, as shipped.  Order is the fallback order when the
# chosen font can't draw a language; an operator's own fonts are never a
# fallback.
LABEL_FONTS: dict[str, str] = {
    "inter": "Inter-Bold.ttf",
    # Ahead of Rubik, which has Arabic and Persian too: Almarai is the one
    # with Urdu's letters, so all three are drawn alike.  It has no Hebrew.
    "almarai": "Almarai-Bold.ttf",
    "rubik": "Rubik-Bold.ttf",
    "jakarta": "PlusJakartaSans-Bold.ttf",
    "manrope": "Manrope-Bold.ttf",
    "montserrat": "Montserrat-Bold.ttf",
    "robotocondensed": "RobotoCondensed-Bold.ttf",
    "barlowcondensed": "BarlowCondensed-Bold.ttf",
    "oswald": "OswaldLabel-Bold.ttf",
    "spacegrotesk": "SpaceGrotesk-Bold.ttf",
    "exo2": "Exo2-Bold.ttf",
    "fira": "FiraSans-Bold.ttf",
    "opensans": "OpenSans-Bold.ttf",
}
DEFAULT_LABEL_FONT = "inter"

# Zero-width non-joiner and joiner: shaping controls, not drawn characters.
JOINERS = frozenset("\u200c\u200d")

# Pillow picks raqm for every font once libraqm loads; the basic layout is
# asked for explicitly everywhere else, so installing libfribidi changes only
# the renders that shape.
HAVE_RAQM = features.check_feature("raqm")
BASIC = ImageFont.Layout.BASIC

_LABEL_PATH: ContextVar[str] = ContextVar(
    "label_font_path", default=os.path.join(FONTS_DIR, LABEL_FONTS[DEFAULT_LABEL_FONT]))


def _path(choice: str) -> str:
    return os.path.join(FONTS_DIR, LABEL_FONTS[choice])


@lru_cache(maxsize=8)
def _probe_font(path: str) -> tuple[ImageFont.FreeTypeFont, bytes]:
    font = ImageFont.truetype(path, 40, layout_engine=BASIC)
    return font, bytes(font.getmask("\U0010FFFD"))   # .notdef, a missing glyph


@lru_cache(maxsize=4096)
def _has_glyph(path: str, ch: str) -> bool:
    font, notdef = _probe_font(path)
    return bytes(font.getmask(ch)) != notdef


def covers(path: str, text: str) -> bool:
    """Whether the font at *path* has a glyph for every character of *text*
    (ASCII is taken as read: every font here has it).  The joiner controls —
    Persian's zero-width non-joiner — need no glyph: raqm acts on them, and
    i18n.visual drops them where it doesn't run."""
    return all(_has_glyph(path, ch) for ch in set(text)
               if ord(ch) > 127 and ch not in JOINERS)


def is_label_font(choice: str) -> bool:
    """Whether *choice* names a label font: a shipped one or one the
    operator uploaded."""
    return choice in LABEL_FONTS or (choice.startswith(custom_fonts.KEY_PREFIX)
                                      and choice in custom_fonts.paths())


def resolve_label_font(choice: str | None, lang: str | None) -> str:
    """Path of the label font for *choice* on a poster in *lang*: the chosen
    font if it has the language's labels, else the first label font that
    does, else the chosen one anyway."""
    path = None
    if choice in LABEL_FONTS:
        path = _path(choice)
    elif choice and choice.startswith(custom_fonts.KEY_PREFIX):
        path = custom_fonts.paths().get(choice)
        # Deleted or replaced on another worker a moment ago: its file is
        # gone before this worker has read the new index.
        if path and not os.path.isfile(path):
            path = None
    # Keyed by the labels themselves, not the code, so nothing resolved
    # before the language files load (or since they changed) sticks.
    return _resolve(path or _path(DEFAULT_LABEL_FONT), i18n.language_text(lang))


@lru_cache(maxsize=64)
def _resolve(path: str, text: str) -> str:
    if covers(path, text):
        return path
    for key in LABEL_FONTS:
        if covers(_path(key), text):
            return _path(key)
    return path


def font_for_text(path: str, text: str) -> str:
    """*path* if its font can draw *text*, else the first label font that can
    — for text from outside the language files, such as a fallback title in
    the poster's language."""
    if covers(path, text):
        return path
    for key in LABEL_FONTS:
        if covers(_path(key), text):
            return _path(key)
    return path


def drawable(text: str) -> bool:
    """Whether some shipped label font can draw every character of *text*."""
    return any(covers(_path(key), text) for key in LABEL_FONTS)


@contextmanager
def label_font_scope(choice: str | None, lang: str | None):
    """Draw labels in *choice* (a LABEL_FONTS key) for the duration, or the
    font that can draw *lang* if *choice* can't, shaped if *lang* needs it."""
    token = _LABEL_PATH.set(resolve_label_font(choice, lang))
    shaping = i18n.SHAPING.set(HAVE_RAQM and i18n.needs_shaping(lang))
    try:
        yield
    finally:
        i18n.SHAPING.reset(shaping)
        _LABEL_PATH.reset(token)


def label_path() -> str:
    """The current render's label font file."""
    return _LABEL_PATH.get()


def shaping() -> bool:
    """Whether the current render lays labels out with raqm."""
    return i18n.SHAPING.get()


def truetype(path: str, size: float, shaped: bool | None = None) -> ImageFont.FreeTypeFont:
    """The font at *path* and *size*, laid out with raqm if *shaped* (by
    default, the current render's shaping()).  A cache keyed on the font path
    that holds measurements or drawn text must key on shaping() too."""
    return _truetype(path, size, shaping() if shaped is None else shaped)


@lru_cache(maxsize=256)
def _truetype(path: str, size: float, shaped: bool) -> ImageFont.FreeTypeFont:
    """ImageFont.truetype, kept: a font is immutable once built."""
    return ImageFont.truetype(path, size, layout_engine=ImageFont.Layout.RAQM if shaped else BASIC)


def label_font(size: float) -> ImageFont.FreeTypeFont:
    """The current render's label font at *size*."""
    return truetype(label_path(), size)


_ARABIC = re.compile("[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")


def has_arabic(text: str | None) -> bool:
    """Whether *text* has any Arabic-script letters."""
    return bool(text) and bool(_ARABIC.search(text))


def arabic_top(font: ImageFont.FreeTypeFont, ink: tuple, cy: float) -> float:
    """Where to draw Arabic text (its top, as ImageDraw.text takes it) to
    centre it on *cy*, given its ink box *ink* drawn at the origin.

    Halfway between centring the ink and centring the band from baseline to
    alef top: an Arabic font's line is taller than its letters (room for
    stacked vowel marks), so the offset tuned for Latin capitals misplaces
    it.  On its ink alone, ي's tail and the dots under the line lift a word
    high; on the alef band alone, a word with no alef ("عربي") sits low."""
    ascent = font.getmetrics()[0]
    alef_height = -font.getbbox("\u0627", anchor="ls")[1]
    return cy - (ink[1] + ink[3]) / 4 - (ascent - alef_height / 2) / 2


def fit_label_size(text: str, size: float, max_width: float) -> float:
    """*size*, or the largest size below it at which *text* in the label font
    is no wider than *max_width* — for a line whose translation runs longer
    than the English it was laid out for ("Документальный ★ 79").  A line
    that fits keeps its size exactly."""
    width = label_font(size).getlength(text)
    if not text or width <= max_width:
        return size
    fitted = size * max_width / width
    # Hinted advances don't scale exactly; step down until it really fits.
    while fitted > 1 and label_font(fitted).getlength(text) > max_width:
        fitted *= 0.98
    return fitted
