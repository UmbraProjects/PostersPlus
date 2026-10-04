"""Operator presets: looks the instance's operator adds to the configurator's
Load preset gallery, next to the ones shipped with Posters+ (Core, in
configurator.html) and the ones each user keeps in their own browser (User).

Every visitor who can open the configurator sees these, so what is stored is
kept to what a preset needs: a name, a description, the render parameters
and an optional preview image.  The parameters come from a poster URL the
operator pastes; every key, token and title id is taken out before it is
kept, so pasting a URL with the instance's access key in it can't publish
the key.

The list is one JSON document in app_state, so every worker reads the same
thing.  Preview images are re-encoded by Pillow and kept under
PRESET_ART_DIR by content hash, served from /preset-art/<name>.
"""
from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit

from PIL import Image, ImageOps

import config as _cfg
from cache import get_app_state, set_app_state
from reports import clean_text

STATE_KEY = "operator_presets"
MAX_PRESETS = 50
MAX_NAME = 60
MAX_DESCRIPTION = 300
MAX_PARAMS = 6000
MAX_VALUE = 1000
MAX_IMAGE_BYTES = 15 * 1024 * 1024
_MAX_PIXELS = 60_000_000
# Stored small: it is a card thumbnail, and every visitor downloads it.
_IMAGE_BOX = (720, 720)
# An upload no preset has picked up yet is kept this long, then swept.
_ORPHAN_GRACE = 3600

_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_IMAGE_RE = re.compile(r"^[0-9a-f]{16}\.jpg$")
_PARAM_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# Who the poster is for, not how it looks — and never anything secret.
_DROPPED_PARAMS = {"tmdb_id", "imdb_id", "type", "stremio_id", "anilist_id", "kitsu_id", "mal_id",
                   "access_key", "tmdb_key", "mdblist_key", "fanart_key", "tvdb_key",
                   "simkl_key", "api_key", "key", "token", "_ts"}
_SECRET_WORDS = ("key", "token", "secret", "password", "auth")
_write_lock = threading.Lock()


def clean_params(url_or_query: str) -> tuple[str, str]:
    """(query string, shape) from a pasted poster URL or query string: title
    ids, keys and anything that isn't a plain parameter name dropped.
    ValueError when nothing usable is left."""
    text = str(url_or_query or "").strip()
    if len(text) > MAX_PARAMS * 2:
        raise ValueError("the URL is too long")
    query = urlsplit(text).query if ("?" in text or "://" in text) else text
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    shape = "portrait"
    for name, value in parse_qsl(query, keep_blank_values=True):
        name = name.strip().lower()
        if (name in seen or name in _DROPPED_PARAMS or not _PARAM_NAME_RE.match(name)
                or any(w in name for w in _SECRET_WORDS)):
            continue
        seen.add(name)
        value = clean_text(value, MAX_VALUE)
        if name == "shape":
            # Portrait is the default.  A "{shape}" URL (a share link) is
            # both: it lists under Portrait and loading it fills both shapes.
            if value == "{shape}":
                pass
            elif value.lower() == "landscape":
                shape, value = "landscape", "landscape"
            else:
                continue
        pairs.append((name, value))
    if not any(name != "shape" for name, _ in pairs):
        raise ValueError("the URL carries no settings")
    out = urlencode(pairs)
    if len(out) > MAX_PARAMS:
        raise ValueError("the URL carries too many settings")
    return out, shape


@contextmanager
def _locked():
    """One writer at a time across threads and worker processes: a save is a
    read-modify-write of the whole list."""
    with _write_lock:
        os.makedirs(_cfg.PRESET_ART_DIR, exist_ok=True)
        with open(os.path.join(_cfg.PRESET_ART_DIR, ".lock"), "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            yield


def _load() -> list[dict]:
    raw = get_app_state(STATE_KEY)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    return [p for p in data if isinstance(p, dict) and _ID_RE.match(str(p.get("id", "")))] \
        if isinstance(data, list) else []


def _save(items: list[dict]) -> None:
    set_app_state(STATE_KEY, json.dumps(items, separators=(",", ":")))


def list_presets() -> list[dict]:
    return _load()


def public_list() -> list[dict]:
    """What the configurator is handed: no timestamps, the image as a URL."""
    return [{
        "id": p["id"],
        "name": p.get("name", ""),
        # Optional: null when the operator left it blank.
        "description": p.get("description") or None,
        "shape": "landscape" if p.get("shape") == "landscape" else "portrait",
        "params": p.get("params", ""),
        "screenshot": f"/preset-art/{p['image']}" if _IMAGE_RE.match(str(p.get("image") or "")) else None,
    } for p in _load()]


def _validate(body: dict) -> dict:
    name = clean_text(body.get("name"), MAX_NAME)
    if not name:
        raise ValueError("a name is needed")
    description = clean_text(body.get("description"), MAX_DESCRIPTION)
    params, shape = clean_params(body.get("params"))
    image = body.get("image") or None
    if image is not None:
        image = str(image)
        if not _IMAGE_RE.match(image) or not os.path.isfile(os.path.join(_cfg.PRESET_ART_DIR, image)):
            raise ValueError("that preview image isn't on this server; upload it again")
    return {"name": name, "description": description, "params": params,
            "shape": shape, "image": image}


def save(body: dict) -> list[dict]:
    """Add a preset (no id) or replace one (its id).  ValueError when the
    body is unusable or the list is full."""
    fields = _validate(body)
    pid = str(body.get("id") or "")
    with _locked():
        items = _load()
        before = {p.get("image") for p in items}
        if pid:
            for i, p in enumerate(items):
                if p["id"] == pid:
                    items[i] = {**p, **fields, "updated": int(time.time())}
                    break
            else:
                raise ValueError("that preset no longer exists")
        else:
            if len(items) >= MAX_PRESETS:
                raise ValueError(f"an instance can hold {MAX_PRESETS} presets")
            items.append({"id": secrets.token_hex(6), **fields, "created": int(time.time())})
        _save(items)
    _sweep_images(items, before)
    return items


def delete(pid: str) -> list[dict]:
    with _locked():
        old = _load()
        items = [p for p in old if p["id"] != pid]
        _save(items)
    _sweep_images(items, {p.get("image") for p in old})
    return items


def reorder(ids: list[str]) -> list[dict]:
    """The same presets in the order given; ids it doesn't name keep their
    place at the end."""
    with _locked():
        items = _load()
        rank = {pid: i for i, pid in enumerate(str(x) for x in ids)}
        items.sort(key=lambda p: rank.get(p["id"], len(rank)))
        _save(items)
    return items


def store_image(data: bytes) -> str:
    """Decode, shrink and re-encode an uploaded preview; returns its name.
    Nothing the operator sent is served as it came.  Blocking."""
    if not data:
        raise ValueError("the image is empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"the image is over {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
    try:
        image = Image.open(io.BytesIO(data))
        if image.width * image.height > _MAX_PIXELS:
            raise ValueError("the image is too large")
        image.load()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"not an image this server can read ({type(exc).__name__})")
    if image.width < 50 or image.height < 50:
        raise ValueError("the image is too small")
    image = ImageOps.exif_transpose(image).convert("RGB")
    image.thumbnail(_IMAGE_BOX, Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=88)
    out = buf.getvalue()
    name = f"{hashlib.sha256(out).hexdigest()[:16]}.jpg"
    os.makedirs(_cfg.PRESET_ART_DIR, exist_ok=True)
    final = os.path.join(_cfg.PRESET_ART_DIR, name)
    if os.path.exists(final):
        os.utime(final)   # a fresh upload, as far as the orphan sweep goes
    else:
        tmp = f"{final}.tmp-{os.getpid()}-{threading.get_ident()}"
        with open(tmp, "wb") as fh:
            fh.write(out)
        os.replace(tmp, final)
    return name


def image_bytes(name: str) -> bytes | None:
    if not _IMAGE_RE.match(name or ""):
        return None
    try:
        with open(os.path.join(_cfg.PRESET_ART_DIR, name), "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _sweep_images(items: list[dict], before: set) -> None:
    """Delete previews no preset uses: at once when a preset let go of it
    (in *before*, the images the list used until now), after an hour when it
    was uploaded and never saved."""
    used = {p.get("image") for p in items}
    try:
        names = os.listdir(_cfg.PRESET_ART_DIR)
    except OSError:
        return
    now = time.time()
    for name in names:
        if name in used or name == ".lock":
            continue
        path = os.path.join(_cfg.PRESET_ART_DIR, name)
        try:
            if name not in before and now - os.path.getmtime(path) < _ORPHAN_GRACE:
                continue
            os.remove(path)
        except OSError:
            pass
