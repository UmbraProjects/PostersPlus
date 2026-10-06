# i18n.py
#
# Poster-output translation with per-key English fallback.
#
# Each languages/<code>.json supplies genreLabels / sashLabels maps keyed by the
# CANONICAL ENGLISH strings the renderer produces (see languages/en.json for the
# reference vocabulary).  Translation is display-only: every internal decision
# (award-star matching, sash priority, font/colour lookups) stays in English, so
# a missing key, a malformed file, or a language with no JSON at all simply falls
# back to the English canonical string.  Nothing breaks if a translation is
# absent — it just renders in English.
import json
import logging
import os
import re
import unicodedata
from contextvars import ContextVar

from bidi import get_display

logger = logging.getLogger(__name__)

_LANG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "languages")
_LANGS: dict[str, dict] = {}

# Trending labels are produced as "#<rank> Today"; translated via the
# "trendingToday" template key (e.g. "#{rank} Aujourd'hui") so the rank stays.
_TRENDING_RE = re.compile(r"^#(\d+)\s+Today$")

# Composite nominee labels are joined with this separator in discovery.pick_sash.
_NOM_SEP = " • "

# Dated release-status labels come out of discovery.release_date_label as
# "Oct 16 Cinema" or "Dec 2027 Cinema".  They translate through the
# "releaseDay" / "releaseMonth" templates ({month}, {day}, {year}, {window})
# so a language can reorder the parts; the window is the plain status label
# ("Cinema" / "Streaming" / "Physical") and translates through its own entry,
# and the month name comes from the top-level "monthsShort" list (twelve
# entries, January first).  A window can have its own pair of templates,
# "releaseDay<Window>" / "releaseMonth<Window>", for when the plain status
# word reads badly with a date — English dates Streaming as "Streams Sep 29".
_RELEASE_DATE_RE = re.compile(
    r"^([A-Z][a-z]{2}) (\d{1,2}|\d{4}) (Cinema|Streaming|Physical|Premiere|Returns|Season \d+)$"
)
_SEASON_WINDOW_RE = re.compile(r"^Season (\d+)$")
_MONTHS_EN = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def load_languages() -> None:
    """Load every languages/*.json into memory once (call at startup)."""
    _LANGS.clear()
    if not os.path.isdir(_LANG_DIR):
        return
    for fn in os.listdir(_LANG_DIR):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(_LANG_DIR, fn), encoding="utf-8") as f:
                data = json.load(f)
            code = (data.get("code") or os.path.splitext(fn)[0]).strip().lower()
            if code:
                _LANGS[code] = data
        except Exception as e:  # malformed file → skip, English fallback stands
            logger.warning(f"i18n: could not load language file {fn!r}: {e}")
    if _LANGS:
        logger.info(f"i18n: loaded languages {sorted(_LANGS)}")


def _lang_candidates(lang: str | None) -> list[str]:
    code = (lang or "").strip().lower().replace("_", "-")
    if not code:
        return []
    base = code.split("-", 1)[0]
    return list(dict.fromkeys([code, base]))


def has_language(lang: str | None) -> bool:
    return any(code in _LANGS for code in _lang_candidates(lang))


def _table(lang: str | None, key: str) -> dict:
    for code in _lang_candidates(lang):
        table = _LANGS.get(code, {}).get(key, {}) or {}
        if table:
            return table
    return {}


def _months_short(lang: str | None) -> tuple[str, ...]:
    for code in _lang_candidates(lang):
        months = _LANGS.get(code, {}).get("monthsShort")
        if isinstance(months, list) and len(months) == 12:
            return tuple(str(m) for m in months)
    return _MONTHS_EN


def _translate_release_date(match: "re.Match[str]", sl: dict, lang: str | None) -> str:
    month_en, rest, window_en = match.group(1), match.group(2), match.group(3)
    if month_en not in _MONTHS_EN:
        return match.group(0)
    month = _months_short(lang)[_MONTHS_EN.index(month_en)]
    is_year = len(rest) == 4
    rest = native_digits(rest, lang)
    season = _SEASON_WINDOW_RE.match(window_en)
    if season:
        # A template rather than a word: most locales put the window before
        # the day, and "Temporada 3 4 mar" runs two numbers together, so
        # they use the short form their TV apps do ("T3 4 mar").
        tmpl = sl.get("seasonWindow")
        window = tmpl.replace("{n}", native_digits(season.group(1), lang)) if tmpl else window_en
    else:
        window = sl.get(window_en, window_en)
    if is_year:
        tmpl = sl.get(f"releaseMonth{window_en}") or sl.get("releaseMonth")
        return (tmpl.replace("{month}", month).replace("{year}", rest).replace("{window}", window)
                if tmpl else match.group(0))
    tmpl = sl.get(f"releaseDay{window_en}") or sl.get("releaseDay")
    return (tmpl.replace("{month}", month).replace("{day}", rest).replace("{window}", window)
            if tmpl else match.group(0))


def native_digits(text: str | None, lang: str | None) -> str:
    """*text*'s digits written in the language's own, as its file's "digits"
    gives them (ten, zero first: Arabic's "٠١٢٣٤٥٦٧٨٩"); unchanged for a
    language that writes 0-9.  Used for the trending rank, dates and years;
    scores and ratings keep 0-9, and so do names and codes such as "A24" and
    "4K"."""
    for code in _lang_candidates(lang):
        digits = str(_LANGS.get(code, {}).get("digits") or "")
        if len(digits) == 10:
            return "".join(digits[ord(ch) - 48] if "0" <= ch <= "9" else ch for ch in (text or ""))
    return text or ""


def translate_genre(name: str | None, lang: str | None) -> str:
    """Canonical English genre name → localized, or unchanged if no translation."""
    if not name:
        return name or ""
    return _table(lang, "genreLabels").get(name, name)


def translate_sash(label: str | None, lang: str | None) -> str:
    """Canonical English sash label → localized, or unchanged if no translation.

    Handles two special shapes: the "#<rank> Today" trending template and the
    " • "-joined composite nominee label (each part translated independently).
    Proper nouns and operator-defined labels (studio / director / cast) usually
    aren't in the JSON, so they pass straight through.
    """
    if not label:
        return label or ""
    # A language without a file reads the English table, so English's own
    # wording (e.g. "Streams Sep 29") still applies rather than the raw label.
    sl = _table(lang, "sashLabels") or _table("en", "sashLabels")
    if not sl:
        return label

    m = _TRENDING_RE.match(label)
    if m:
        # The rank in the language's own numerals, where it has them ("#٨").
        tmpl = sl.get("trendingToday")
        return tmpl.replace("{rank}", native_digits(m.group(1), lang)) if tmpl else label

    m = _RELEASE_DATE_RE.match(label)
    if m:
        return _translate_release_date(m, sl, lang)

    if _NOM_SEP in label:
        return _NOM_SEP.join(sl.get(part, part) for part in label.split(_NOM_SEP))

    return sl.get(label, label)


def upper_label(text: str | None, lang: str | None) -> str:
    """Uppercase a translated label the way the language itself would.

    str.upper() is locale-blind, which is wrong for two shipped languages:
    Turkish (and Azerbaijani) dotted i uppercases to İ, not I; and Greek
    drops the tonos in all-caps ("Ταινία" → "ΤΑΙΝΙΑ", not "ΤΑΙΝΊΑ") while
    keeping the diaeresis.
    """
    if not text:
        return text or ""
    base = (_lang_candidates(lang) or [""])[-1]
    if base in ("tr", "az"):
        text = text.replace("i", "İ")
    text = text.upper()
    if base == "el":
        text = unicodedata.normalize(
            "NFC", unicodedata.normalize("NFD", text).replace("́", ""))
    return text


def language_text(lang: str | None) -> str:
    """Every character a language's labels can put on a poster, upper case
    included (the landscape badge uppercases), for checking a font has them.
    Empty for a language with no file: it draws the English labels."""
    parts: list[str] = []
    for code in _lang_candidates(lang):
        data = _LANGS.get(code)
        if data:
            for key in ("genreLabels", "sashLabels"):
                table = data.get(key)
                if isinstance(table, dict):
                    parts.extend(str(v) for v in table.values())
            months = data.get("monthsShort")
            if isinstance(months, list):
                parts.extend(str(m) for m in months)
            parts.append(str(data.get("digits") or ""))
    text = "".join(parts)
    return text + upper_label(text, lang)


# Languages written in a script whose letters change shape with their
# neighbours (Arabic joins every letter to the next), which only a shaping
# layout (Pillow's raqm: HarfBuzz + FriBiDi) draws correctly.  fonts sets
# SHAPING around a render of one of them when raqm is available.
_SHAPED_LANGS = frozenset({"ar", "fa", "ur"})
SHAPING: ContextVar[bool] = ContextVar("label_shaping", default=False)


def needs_shaping(lang: str | None) -> bool:
    """Whether *lang* is written in a script that needs shaping."""
    return (_lang_candidates(lang) or [""])[-1] in _SHAPED_LANGS


# Right-to-left scripts: Hebrew, Arabic, Syriac, Thaana, NKo, Samaritan,
# Mandaic, and their presentation forms.
_RTL_RE = re.compile("[\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFF]")


def visual(text: str | None) -> str:
    """A line of text in the order it is drawn, left to right.

    Pillow here has no bidi layout (no libraqm) and Skia's drawString has none
    either: both draw characters in the order they're stored.  A line holding
    right-to-left script is reordered with the Unicode bidi algorithm, as a
    right-to-left paragraph — so "דרמה · 2024 ★ 87" reads genre first from the
    right, with numbers and Latin names still left to right inside it.  Lines
    with no right-to-left character come back unchanged.

    Apply it to a whole line just before measuring and drawing, never before
    joining or wrapping: reordering is per line.  Hebrew needs nothing more.
    Arabic also needs its letters joined, so its renders lay text out with
    raqm (SHAPING), which reorders as it shapes: there the line comes back
    unchanged, in reading order.
    """
    if not text or SHAPING.get() or not _RTL_RE.search(text):
        return text or ""
    # Joiner controls mean nothing to a layout that doesn't join, and a font
    # may have no glyph for them (fonts.covers lets them through).
    return get_display(text.replace("\u200c", "").replace("\u200d", ""), base_dir="R")
