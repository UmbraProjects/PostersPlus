"""Label fonts the instance's operator uploads in the dashboard (Fonts), for
fonts Posters+ can't ship: a bought font, a house font.

An upload is made ready the way the shipped label fonts are
(fontprep): a .otf's outlines become TrueType ones, heavy hinting comes
out, and Inter's ★, separators and dashes go in where the font has none —
the labels draw "★ 87" and "Drama · 2024".
It is kept under CUSTOM_FONT_DIR by content hash, and the index there
(fonts.json) maps each font's key — "custom-" and a slug of its name, the
value label_font takes — to its file.  Uploading under a name already in use
replaces that font; the key stays, so URLs keep working.

Every worker reads the index; one that didn't serve the save notices within a
few seconds (refresh).  The file name is part of a render's cache key
(main._render_config_signature), so replacing a font re-renders only the
posters drawn in it.
"""
from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import re
import threading
import time
from contextlib import contextmanager

import config as _cfg
from reports import clean_text

KEY_PREFIX = "custom-"
MAX_FONTS = 30
MAX_NAME = 40
# Room for a big family's Bold; a CJK font runs to 20 MB and more, and its
# outlines would take a long while to convert.
MAX_FONT_BYTES = 20 * 1024 * 1024

# How an upload is prepared; an entry kept by an older version is prepared
# again at startup (upgrade).  2: the separators and dashes, not only the ★.
PREP_VERSION = 2

_INDEX = "fonts.json"
_FILE_RE = re.compile(r"^[0-9a-f]{16}\.ttf$")
_KEY_RE = re.compile(r"^custom-[a-z0-9]+(?:-[a-z0-9]+)*$")
_CHECK_INTERVAL = 3.0
# What every label needs drawn: a font without it can't be a label font.
_REQUIRED = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

_write_lock = threading.Lock()
_fonts: list[dict] = []
_loaded_stamp: tuple | None = None
_checked_at = float("-inf")


def _dir() -> str:
    return _cfg.CUSTOM_FONT_DIR


def _index_path() -> str:
    return os.path.join(_dir(), _INDEX)


def _stamp() -> tuple | None:
    try:
        st = os.stat(_index_path())
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _read_index() -> list[dict]:
    try:
        with open(_index_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    items = data.get("fonts") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [f for f in items if isinstance(f, dict)
            and _KEY_RE.match(str(f.get("key", ""))) and _FILE_RE.match(str(f.get("file", "")))]


def _reload() -> None:
    global _fonts, _loaded_stamp
    stamp = _stamp()
    _fonts = _read_index()
    _loaded_stamp = stamp


def refresh(force: bool = False) -> None:
    """Re-read the index if it changed since it was read — a save lands on
    whichever worker served it.  A clock read on most calls, one stat every
    few seconds."""
    global _checked_at
    now = time.monotonic()
    if not force and now - _checked_at < _CHECK_INTERVAL:
        return
    _checked_at = now
    if force or _stamp() != _loaded_stamp:
        _reload()


def paths() -> dict[str, str]:
    """key → font file, for every uploaded font."""
    refresh()
    return {f["key"]: os.path.join(_dir(), f["file"]) for f in _fonts}


def file_of(key: str) -> str | None:
    """The file name behind a custom key (a content hash), or None."""
    refresh()
    for f in _fonts:
        if f["key"] == key:
            return f["file"]
    return None


def public_list() -> list[dict]:
    """What the configurator's Font list is handed."""
    refresh()
    return [{"key": f["key"], "name": f.get("name") or f["key"]} for f in _fonts]


def admin_list() -> list[dict]:
    refresh()
    return [{
        "key": f["key"],
        "name": f.get("name") or f["key"],
        "family": f.get("family", ""),
        "size": f.get("size", 0),
        "notes": f.get("notes", []),
        "added": f.get("added", 0),
    } for f in _fonts]


def key_for(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:MAX_NAME].strip("-")
    if not slug:
        raise ValueError("the name needs a letter or digit in it")
    return KEY_PREFIX + slug


# ---------------------------------------------------------------------------
# Preparing an upload
# ---------------------------------------------------------------------------

def prepare(data: bytes) -> tuple[bytes, str, list[str]]:
    """An uploaded font made ready to draw labels in: (TrueType bytes, family
    name, what was done to it).  ValueError, worded for the operator, when it
    can't be a label font.  Blocking (a parse, maybe an outline conversion)."""
    if len(data) > MAX_FONT_BYTES:
        raise ValueError(f"the font is larger than {MAX_FONT_BYTES // (1024 * 1024)} MB")
    magic = data[:4]
    if magic == b"ttcf":
        raise ValueError("that's a font collection (.ttc); upload the single font (.ttf or .otf)")
    if magic in (b"wOFF", b"wOF2"):
        raise ValueError("that's a web font (.woff); upload the .ttf or .otf")
    if magic not in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
        raise ValueError("that isn't a .ttf or .otf font")

    # fontTools only here: rendering never needs it.
    from fontTools.ttLib import TTFont
    import fontprep

    try:
        font = TTFont(io.BytesIO(data), lazy=False)
        cmap = font.getBestCmap() or {}
    except Exception:
        raise ValueError("the font file couldn't be read")
    if "fvar" in font or "CFF2" in font:
        raise ValueError("variable fonts aren't supported; upload a single weight "
                         "(the Bold looks best) as a static .ttf or .otf")
    missing = [ch for ch in _REQUIRED if ord(ch) not in cmap]
    if missing:
        raise ValueError(f"the font has no {''.join(missing[:12])}{'…' if len(missing) > 12 else ''}"
                         " — labels need every basic Latin letter and digit")
    notes = []
    try:
        if "CFF " in font:
            fontprep.cff_to_glyf(font)
            notes.append("converted from CFF outlines")
        if "glyf" not in font:
            raise ValueError("the font has no outlines Posters+ can draw")
        if fontprep.strip_heavy_hinting(font):
            notes.append("hinting removed")
        added = fontprep.add_label_symbols(font)
        if added:
            notes.append(f"{' '.join(added)} added from Inter")
        family = clean_text(font["name"].getBestFamilyName() or "", 80) if "name" in font else ""
        sub = clean_text(font["name"].getBestSubFamilyName() or "", 40) if "name" in font else ""
        buf = io.BytesIO()
        font.save(buf)
    except ValueError:
        raise
    except Exception:
        raise ValueError("the font couldn't be prepared (its tables may be damaged)")
    out = buf.getvalue()
    _check_draws(out)
    return out, f"{family} {sub}".strip() if sub != "Regular" else family, notes


def _check_draws(data: bytes) -> None:
    """Both renderers can load the prepared font and draw a label with it."""
    from PIL import ImageFont
    import fontprep
    try:
        font = ImageFont.truetype(io.BytesIO(data), 40, layout_engine=ImageFont.Layout.BASIC)
        notdef = bytes(font.getmask("\U0010FFFD"))
        for text in (*fontprep.LABEL_SYMBOLS, "Sci-Fi 88"):
            mask = font.getmask(text)
            if mask.getbbox() is None or bytes(mask) == notdef:
                raise ValueError
    except Exception:
        raise ValueError("the prepared font doesn't draw (Pillow couldn't use it)")
    try:
        import skia
    except ImportError:
        return
    if skia.Typeface.MakeFromData(skia.Data.MakeWithCopy(data)) is None:
        raise ValueError("the prepared font doesn't draw (Skia couldn't use it)")


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------

@contextmanager
def _locked():
    """One writer at a time across threads and worker processes: a save is a
    read-modify-write of the index."""
    with _write_lock:
        os.makedirs(_dir(), exist_ok=True)
        with open(os.path.join(_dir(), ".lock"), "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            yield


def _write_index(items: list[dict]) -> None:
    final = _index_path()
    tmp = f"{final}.tmp-{os.getpid()}-{threading.get_ident()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"fonts": items}, fh, indent=1)
    os.replace(tmp, final)


def _sweep(items: list[dict]) -> None:
    """Delete font files no entry uses.  A worker that hasn't seen the new
    index yet falls back to Inter for the few seconds until it does
    (fonts.resolve_label_font checks the file is there)."""
    used = {f["file"] for f in items}
    for name in os.listdir(_dir()):
        if _FILE_RE.match(name) and name not in used:
            try:
                os.remove(os.path.join(_dir(), name))
            except OSError:
                pass


def _write_font(file: str, data: bytes) -> None:
    final = os.path.join(_dir(), file)
    if not os.path.exists(final):
        tmp = f"{final}.tmp-{os.getpid()}-{threading.get_ident()}"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, final)


def store(data: bytes, name: str) -> dict:
    """Prepare and keep an uploaded font under *name*, replacing the font
    already under it; returns its admin entry.  ValueError when it can't be
    used.  Blocking."""
    name = clean_text(name, MAX_NAME)
    key = key_for(name)
    ready, family, notes = prepare(data)
    file = f"{hashlib.sha256(ready).hexdigest()[:16]}.ttf"
    with _locked():
        items = _read_index()
        replacing = any(f["key"] == key for f in items)
        if not replacing and len(items) >= MAX_FONTS:
            raise ValueError(f"that's the limit of {MAX_FONTS} fonts; delete one first")
        _write_font(file, ready)
        entry = {"key": key, "name": name, "file": file, "family": family, "size": len(ready),
                 "notes": notes, "added": int(time.time()), "prep": PREP_VERSION}
        items = [entry if f["key"] == key else f for f in items] if replacing else items + [entry]
        _write_index(items)
        _sweep(items)
    refresh(force=True)
    return next(f for f in admin_list() if f["key"] == key)


def delete(key: str) -> bool:
    with _locked():
        items = _read_index()
        kept = [f for f in items if f["key"] != key]
        if len(kept) == len(items):
            return False
        _write_index(kept)
        _sweep(kept)
    refresh(force=True)
    return True


def upgrade() -> int:
    """Prepare again every font kept by an older PREP_VERSION — its stored
    file is itself a font prepare() takes — so a font uploaded before a
    preparation fix gets it without a new upload.  The number upgraded.  Run
    once at startup; every worker may, and the lock makes the rest find
    nothing to do.  Blocking."""
    if not os.path.exists(_index_path()):
        return 0
    done = 0
    with _locked():
        items = _read_index()
        for i, f in enumerate(items):
            if f.get("prep", 1) >= PREP_VERSION:
                continue
            try:
                with open(os.path.join(_dir(), f["file"]), "rb") as fh:
                    ready, _family, notes = prepare(fh.read())
            except (OSError, ValueError):
                continue   # left as it was: still draws, as before
            file = f"{hashlib.sha256(ready).hexdigest()[:16]}.ttf"
            _write_font(file, ready)
            # This pass only adds; what the first one did still stands.
            items[i] = {**f, "file": file, "size": len(ready), "prep": PREP_VERSION,
                        "notes": _merge_notes(f.get("notes", []), notes)}
            done += 1
        if done:
            _write_index(items)
            _sweep(items)
    if done:
        refresh(force=True)
    return done


def _merge_notes(old: list, new: list) -> list:
    import fontprep
    added = {ch for n in (*old, *new) if n.endswith(" added from Inter")
             for ch in n[:-len(" added from Inter")].split()}
    notes = [n for n in old if not n.endswith(" added from Inter")]
    notes += [n for n in new if n not in notes and not n.endswith(" added from Inter")]
    ordered = [ch for ch in fontprep.LABEL_SYMBOLS if ch in added]
    return notes + ([f"{' '.join(ordered)} added from Inter"] if ordered else [])
