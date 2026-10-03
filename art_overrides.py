"""Operator-chosen art per title: the dashboard's Artwork view.

An override replaces the poster or logo the pipeline would pick for one title,
for everyone on the instance.  It is operator-scoped on purpose: each title
holds at most a handful of chosen images, fetched once when a render first
needs them and cached like any other art, however many users there are.

Five slots:
  textless  the art under our logo (every mode but original art).  One per
            title; it carries no text, so it has no language.
  original  the poster served as-is under original art, per request language.
  logo      the logo, per request language, or "null" for a language-neutral
            one.
  landscape           the landscape layout's textless backdrop (our logo on
                      top); one per title.
  landscape_original  its text-bearing backdrop under landscape original art,
                      per request language.

A poster override names the poster sources it stands in for (tmdb, fanart,
tvdb, cinemeta): a user whose source is not in the list gets that source's own
pick.  Cinemeta posters are always served as-is, so only an original override
can stand in for one; cinemeta is dropped from a textless override's list.
Logos and landscape art are not tied to a poster source (landscape always
draws from TMDB) and apply to every request.

A language-keyed override follows the configurator's language order rather
than jumping it: walking the request's order, the first language with either
an override or TMDB art of its own wins.  So a German user whose title has a
German poster keeps it when only an English override exists.

An override can also be an image the operator pasted a link to or uploaded
(ThePosterDB has no API, but its download links work).  That image is
fetched once, when it is chosen, normalised and kept under CUSTOM_ART_DIR as
"custom:<hash>.<ext>" — never hot-linked, since an arbitrary host can vanish
or throttle, and never in the pruned art cache.  A file goes when no override
uses it any more.

A textless poster can carry a manual crop: the operator picks a backdrop (or
any wider image) and places a 2:3 window on it, optionally zoomed in.  See
Crop.

Rows live in SQLite (art_overrides) and each worker keeps a snapshot, checked
against a revision in app_state every few seconds.  A worker that sees the
revision move drops its in-memory composites for the titles that changed;
the writing worker has already deleted them from SQLite.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import ipaddress
import logging
import os
import re
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image

import config as _cfg
from cache import (
    _db_lock,
    get_app_state,
    get_db,
    invalidate_final_posters,
    set_app_state,
)

logger = logging.getLogger(__name__)

SLOTS = ("textless", "original", "logo", "landscape", "landscape_original")
# Slots with one image per title rather than one per language.
_NO_LANGUAGE = ("textless", "landscape")
# Slots that apply whatever the user's poster source.
_SOURCELESS = ("logo", "landscape", "landscape_original")
SOURCES = ("tmdb", "fanart", "tvdb", "cinemeta")
_REV_KEY = "art_overrides_rev"
# How stale a worker's snapshot may get before it rechecks the revision.
_CHECK_INTERVAL = 5.0

_TMDB_PATH_RE = re.compile(r"^/[A-Za-z0-9_-]{1,128}\.(?:jpg|jpeg|png|svg|webp)$", re.I)
_LANGUAGE_RE = re.compile(r"^[a-z]{2,3}(?:-[a-z0-9]{2,4})?$")
# Hosts an absolute override may point at, and the provider each one is.
_HOSTS = {"assets.fanart.tv": "fanart", "artworks.thetvdb.com": "tvdb"}
_MAX_PATH = 512

CUSTOM_PREFIX = "custom:"
_CUSTOM_RE = re.compile(r"^custom:[0-9a-f]{16}\.(?:jpg|png)$")
# A pasted link's download and an upload are capped here; ThePosterDB's
# originals run to a few MB.
MAX_CUSTOM_BYTES = 25 * 1024 * 1024
# Decoded size cap, well under Pillow's bomb guard but above any real poster.
_MAX_PIXELS = 60_000_000
# Stored no larger than the largest canvas a render can ask for.
_MAX_SIZE = {"poster": (2000, 3000), "landscape": (2560, 1440), "logo": (2000, 1000)}
_MAX_REDIRECTS = 5


@dataclass(frozen=True)
class Crop:
    """A 2:3 window on the source image.  At zoom 1 it is the largest 2:3
    window that fits (a backdrop's full height); zoom 2 is half that size.
    x and y place it within the room left over: 0 = left/top edge, 1 =
    right/bottom, 0.5 = centred."""
    x: float = 0.5
    y: float = 0.5
    zoom: float = 1.0

    def box(self, width: int, height: int) -> tuple[int, int, int, int]:
        """The window in pixels on a width x height image."""
        base_w = min(width, height * 2 / 3)
        crop_w = base_w / self.zoom
        crop_h = crop_w * 3 / 2
        left = (width - crop_w) * self.x
        top = (height - crop_h) * self.y
        return (round(left), round(top), round(left + crop_w), round(top + crop_h))

    def token(self) -> str:
        return f"{self.x:.4f},{self.y:.4f},{self.zoom:.3f}"


MAX_ZOOM = 4.0


def parse_crop(value) -> Crop | None:
    """A Crop from the page ({"x", "y", "zoom"}) or the table ("x,y,zoom");
    None for none.  ValueError when out of range."""
    if value in (None, "", {}):
        return None
    try:
        if isinstance(value, dict):
            x, y, zoom = (float(value.get(k, d)) for k, d in (("x", 0.5), ("y", 0.5), ("zoom", 1)))
        else:
            x, y, zoom = (float(p) for p in str(value).split(","))
    except (TypeError, ValueError):
        raise ValueError("bad crop")
    if not (0 <= x <= 1 and 0 <= y <= 1 and 1 <= zoom <= MAX_ZOOM):
        raise ValueError("crop out of range")
    return Crop(round(x, 4), round(y, 4), round(zoom, 3))


@dataclass(frozen=True)
class Override:
    slot: str
    language: str
    path: str
    provider: str
    sources: frozenset
    title: str
    updated_at: float
    crop: Crop | None = None

    def as_dict(self) -> dict:
        return {
            "slot": self.slot, "language": self.language, "path": self.path,
            "provider": self.provider, "sources": sorted(self.sources),
            "title": self.title, "updated_at": self.updated_at,
            "crop": ({"x": self.crop.x, "y": self.crop.y, "zoom": self.crop.zoom}
                     if self.crop else None),
        }


# {(media_type, tmdb_id): {slot: {language: Override}}}
Entry = dict[str, dict[str, Override]]
_snapshot: dict[tuple[str, str], Entry] = {}
_rev: str | None = None
_checked_at = float("-inf")
_lock = threading.Lock()


def media_kind(media_type: str) -> str:
    """Stremio says "series", TMDB and the warmer say "tv": one key either way."""
    return "tv" if media_type in ("tv", "series") else "movie"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def provider_of(path: str) -> str:
    """The provider an image path belongs to, or ValueError when it is not
    one we fetch from.  The server downloads whatever is stored here, so the
    host is checked rather than taken from the page."""
    if not isinstance(path, str) or not path or len(path) > _MAX_PATH:
        raise ValueError("path missing or too long")
    if _TMDB_PATH_RE.match(path):
        return "tmdb"
    if _CUSTOM_RE.match(path):
        return "custom"
    parts = urlsplit(path)
    if (parts.scheme not in ("http", "https") or parts.username or parts.password
            or parts.port or parts.query or parts.fragment):
        raise ValueError("not a TMDB path or a plain fanart.tv / TVDB url")
    provider = _HOSTS.get((parts.hostname or "").lower())
    if provider is None:
        raise ValueError(f"host {parts.hostname!r} is not an art provider")
    return provider


def normalise_language(slot: str, language: str | None) -> str:
    language = (language or "").strip().lower().replace("_", "-")
    if slot in _NO_LANGUAGE:
        return ""
    if slot == "logo" and language == "null":
        return language
    if not _LANGUAGE_RE.match(language):
        raise ValueError(f"bad language {language!r}")
    return language


def normalise_sources(slot: str, sources: Iterable[str] | None) -> frozenset:
    if slot in _SOURCELESS:
        return frozenset()
    chosen = frozenset(s for s in (sources or ()) if s in SOURCES)
    if slot == "textless":
        chosen -= {"cinemeta"}
    if not chosen:
        raise ValueError("a poster override needs at least one source")
    return chosen


# ---------------------------------------------------------------------------
# Custom images (a pasted link or an upload)
# ---------------------------------------------------------------------------

def is_custom(path: str | None) -> bool:
    return bool(path) and path.startswith(CUSTOM_PREFIX)


def custom_file(path: str) -> str | None:
    """The file behind a "custom:" path, or None when the path is malformed."""
    if not _CUSTOM_RE.match(path or ""):
        return None
    return os.path.join(_cfg.CUSTOM_ART_DIR, path[len(CUSTOM_PREFIX):])


def custom_art_bytes(path: str) -> bytes | None:
    """A stored custom image's bytes, or None when it is missing."""
    file = custom_file(path)
    if file is None:
        return None
    try:
        with open(file, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def image_kind(slot: str) -> str:
    """What a slot's image is: "poster", "landscape" or "logo"."""
    return "logo" if slot == "logo" else "landscape" if slot.startswith("landscape") else "poster"


def store_custom_image(data: bytes, *, kind: str) -> str:
    """Decode, check and normalise an operator's image and keep it; returns
    its "custom:" path.  A poster or landscape image becomes an RGB JPEG no
    larger than the biggest canvas; a logo an alpha-trimmed PNG.  ValueError
    when the bytes aren't a usable image.  Blocking (a decode and an encode)."""
    if not data:
        raise ValueError("the image is empty")
    if len(data) > MAX_CUSTOM_BYTES:
        raise ValueError(f"the image is over {MAX_CUSTOM_BYTES // (1024 * 1024)} MB")
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
    buf = io.BytesIO()
    if kind == "logo":
        image = image.convert("RGBA")
        bbox = image.getchannel("A").getbbox()
        if bbox is None:
            raise ValueError("the logo is fully transparent")
        image = image.crop(bbox)
        image.thumbnail(_MAX_SIZE["logo"], Image.Resampling.LANCZOS)
        image.save(buf, format="PNG")
        ext = "png"
    else:
        image = image.convert("RGB")
        image.thumbnail(_MAX_SIZE.get(kind, _MAX_SIZE["poster"]), Image.Resampling.LANCZOS)
        image.save(buf, format="JPEG", quality=92)
        ext = "jpg"
    out = buf.getvalue()
    name = f"{hashlib.sha256(out).hexdigest()[:16]}.{ext}"
    os.makedirs(_cfg.CUSTOM_ART_DIR, exist_ok=True)
    final = os.path.join(_cfg.CUSTOM_ART_DIR, name)
    if not os.path.exists(final):
        tmp = f"{final}.tmp-{os.getpid()}-{threading.get_ident()}"
        with open(tmp, "wb") as fh:
            fh.write(out)
        os.replace(tmp, final)
    return CUSTOM_PREFIX + name


async def _check_public_host(host: str, port: int) -> None:
    """ValueError unless every address *host* resolves to is a public one:
    a pasted link must not turn the server into a probe of its own network
    (the QualiCache container, the Docker host, a cloud metadata endpoint)."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise ValueError(f"can't resolve {host}")
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if not address.is_global:
            raise ValueError(f"{host} is not a public address")


async def download_custom_url(client: httpx.AsyncClient, url: str) -> bytes:
    """Download an operator's pasted image link, following redirects by hand
    so every hop is checked.  ValueError with a readable reason on failure."""
    for _ in range(_MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("the link must be an http(s) URL")
        if parts.username or parts.password:
            raise ValueError("the link can't carry a username or password")
        await _check_public_host(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
        try:
            async with client.stream("GET", url, follow_redirects=False,
                                     timeout=httpx.Timeout(20.0, connect=8.0)) as resp:
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                    url = urljoin(url, resp.headers["location"])
                    continue
                if resp.status_code != 200:
                    raise ValueError(f"the link returned HTTP {resp.status_code}")
                ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if ctype and not (ctype.startswith("image/") or ctype == "application/octet-stream"):
                    raise ValueError(f"the link is a {ctype} page, not an image")
                chunks, size = [], 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_CUSTOM_BYTES:
                        raise ValueError(f"the image is over {MAX_CUSTOM_BYTES // (1024 * 1024)} MB")
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.HTTPError as exc:
            raise ValueError(f"download failed ({type(exc).__name__})")
    raise ValueError("too many redirects")


def _drop_unused_custom(paths: Iterable[str]) -> None:
    """Delete stored custom images no override refers to any more."""
    paths = [p for p in paths if is_custom(p)]
    if not paths:
        return
    try:
        used = {row[0] for row in get_db().execute(
            "SELECT path FROM art_overrides WHERE path LIKE 'custom:%'")}
    except Exception as exc:
        logger.error(f"Art overrides: couldn't check custom image use ({exc})")
        return
    for path in set(paths) - used:
        file = custom_file(path)
        try:
            if file:
                os.remove(file)
                logger.info(f"Art overrides: removed unused custom image {path}")
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning(f"Art overrides: couldn't remove {path}: {exc}")


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def _load() -> dict[tuple[str, str], Entry]:
    rows = get_db().execute(
        "SELECT media_type, tmdb_id, slot, language, path, provider, sources, "
        "title, updated_at, crop FROM art_overrides"
    ).fetchall()
    out: dict[tuple[str, str], Entry] = {}
    for media_type, tmdb_id, slot, language, path, provider, sources, title, updated_at, crop in rows:
        if slot not in SLOTS:
            continue
        try:
            crop = parse_crop(crop)
        except ValueError:
            crop = None
        out.setdefault((media_type, tmdb_id), {}).setdefault(slot, {})[language] = Override(
            slot, language, path, provider,
            frozenset(s for s in (sources or "").split(",") if s),
            title or "", float(updated_at), crop,
        )
    return out


def refresh(force: bool = False) -> None:
    """Bring this worker's snapshot up to the stored revision.  Cheap between
    checks (a clock read); a check is one app_state row."""
    global _snapshot, _rev, _checked_at
    now = time.monotonic()
    if not force and now - _checked_at < _CHECK_INTERVAL:
        return
    with _lock:
        if not force and now - _checked_at < _CHECK_INTERVAL:
            return
        _checked_at = now
        rev = get_app_state(_REV_KEY) or ""
        if rev == _rev and not force:
            return
        try:
            fresh = _load()
        except Exception as exc:
            logger.error(f"Art overrides: load failed ({exc}) — keeping the last snapshot")
            return
        first = _rev is None
        changed = {k for k in fresh.keys() | _snapshot.keys()
                   if fresh.get(k) != _snapshot.get(k)}
        _snapshot, _rev = fresh, rev
    if not first:
        for media_type, tmdb_id in changed:
            invalidate_final_posters(tmdb_id, media_type, l1_only=True)


def for_title(media_type: str, tmdb_id: str | None) -> Entry | None:
    if not tmdb_id:
        return None
    refresh()
    return _snapshot.get((media_kind(media_type), str(tmdb_id)))


# ---------------------------------------------------------------------------
# Selection (pure; the render path passes in what TMDB has)
# ---------------------------------------------------------------------------

def pick_poster(
    entry: Entry | None,
    *,
    original: bool,
    source: str | None,
    language_order: list[str],
    has_language: Callable[[str], bool],
) -> Override | None:
    """The override that replaces this request's poster, or None.

    *source* is the poster source the request resolved to (None for art that
    is not from one of the three, e.g. an anime provider's cover).  Under
    original art, *language_order* is the request's language order and
    *has_language* says whether TMDB has a poster in a language, which ends
    the walk there when no override does."""
    if not entry or source is None:
        return None
    if not original:
        override = entry.get("textless", {}).get("")
        return override if override and source in override.sources else None
    by_language = entry.get("original", {})
    for language in language_order:
        override = by_language.get(language)
        if override and source in override.sources:
            return override
        if has_language(language):
            return None
    return None


def pick_landscape(
    entry: Entry | None,
    *,
    original: bool,
    language_order: list[str],
    has_language: Callable[[str], bool],
) -> Override | None:
    """The landscape override for this request, or None: the textless one,
    or under landscape original art the per-language walk pick_poster does."""
    if not entry:
        return None
    if not original:
        return entry.get("landscape", {}).get("")
    by_language = entry.get("landscape_original", {})
    for language in language_order:
        if by_language.get(language):
            return by_language[language]
        if has_language(language):
            return None
    return None


def pick_logo(
    entry: Entry | None,
    steps: list[str],
    has_step: Callable[[str], bool],
) -> Override | None:
    """The logo override for a request whose logo steps are *steps* (as
    tmdb.logo_language_steps gives them), or None.  A step TMDB has a logo
    for ends the walk, as it would end fetch_logo's."""
    by_language = (entry or {}).get("logo", {})
    if not by_language:
        return None
    for step in steps:
        if step == "metahub":
            continue
        override = by_language.get(step)
        if override:
            return override
        if has_step(step):
            return None
    return None


# ---------------------------------------------------------------------------
# Writes (dashboard only)
# ---------------------------------------------------------------------------

def _changed(media_type: str, tmdb_id: str) -> None:
    set_app_state(_REV_KEY, str(time.time_ns()))
    invalidate_final_posters(tmdb_id, media_type)
    refresh(force=True)


def set_override(
    media_type: str,
    tmdb_id: str,
    slot: str,
    language: str | None,
    path: str,
    sources: Iterable[str] | None = None,
    title: str = "",
    crop=None,
) -> Override:
    """Store (or replace) one override.  ValueError on anything invalid.
    *crop* ({"x", "y", "zoom"}) is for the textless slot only."""
    if slot not in SLOTS:
        raise ValueError(f"bad slot {slot!r}")
    if not re.fullmatch(r"\d{1,10}", str(tmdb_id or "")):
        raise ValueError("bad tmdb_id")
    media_type = media_kind(media_type)
    language = normalise_language(slot, language)
    provider = provider_of(path)
    chosen = normalise_sources(slot, sources)
    crop = parse_crop(crop)
    if crop is not None and slot != "textless":
        raise ValueError("only the textless poster takes a crop")
    title = (title or "")[:200]
    now = time.time()
    with _db_lock:
        db = get_db()
        previous = db.execute(
            "SELECT path FROM art_overrides WHERE media_type = ? AND tmdb_id = ? "
            "AND slot = ? AND language = ?",
            (media_type, str(tmdb_id), slot, language),
        ).fetchone()
        db.execute(
            """
            INSERT INTO art_overrides
                (media_type, tmdb_id, slot, language, path, provider, sources, title,
                 updated_at, crop)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(media_type, tmdb_id, slot, language) DO UPDATE SET
                path=excluded.path, provider=excluded.provider,
                sources=excluded.sources, crop=excluded.crop,
                title=CASE WHEN excluded.title != '' THEN excluded.title
                           ELSE art_overrides.title END,
                updated_at=excluded.updated_at
            """,
            (media_type, str(tmdb_id), slot, language, path, provider,
             ",".join(sorted(chosen)), title, now, crop.token() if crop else None),
        )
        db.commit()
    logger.info(f"Art override set: {media_type} {tmdb_id} {slot}"
                f"{'/' + language if language else ''} -> {path}")
    _changed(media_type, str(tmdb_id))
    if previous and previous[0] != path:
        _drop_unused_custom([previous[0]])
    return Override(slot, language, path, provider, chosen, title, now, crop)


def clear_override(
    media_type: str, tmdb_id: str, slot: str | None = None, language: str | None = None,
) -> int:
    """Remove one override, a slot's worth, or (no slot) the whole title."""
    media_type = media_kind(media_type)
    sql = "DELETE FROM art_overrides WHERE media_type = ? AND tmdb_id = ?"
    args: list = [media_type, str(tmdb_id)]
    if slot is not None:
        sql += " AND slot = ?"
        args.append(slot)
        if language is not None:
            sql += " AND language = ?"
            args.append(normalise_language(slot, language))
    with _db_lock:
        db = get_db()
        dropped = [row[0] for row in db.execute(sql.replace("DELETE", "SELECT path", 1), args)]
        removed = db.execute(sql, args).rowcount
        db.commit()
    if removed:
        logger.info(f"Art override cleared: {media_type} {tmdb_id} ({removed} row(s))")
        _changed(media_type, str(tmdb_id))
        _drop_unused_custom(dropped)
    return removed


def list_overrides() -> list[dict]:
    """Every overridden title, newest change first."""
    refresh(force=True)
    titles = []
    for (media_type, tmdb_id), entry in _snapshot.items():
        items = [o.as_dict() for slot in entry.values() for o in slot.values()]
        titles.append({
            "media_type": media_type,
            "tmdb_id": tmdb_id,
            "title": next((o["title"] for o in items if o["title"]), ""),
            "updated_at": max(o["updated_at"] for o in items),
            "overrides": sorted(items, key=lambda o: (SLOTS.index(o["slot"]), o["language"])),
        })
    titles.sort(key=lambda t: -t["updated_at"])
    return titles


def title_overrides(media_type: str, tmdb_id: str) -> list[dict]:
    refresh(force=True)
    entry = _snapshot.get((media_kind(media_type), str(tmdb_id))) or {}
    return [o.as_dict() for slot in entry.values() for o in slot.values()]
