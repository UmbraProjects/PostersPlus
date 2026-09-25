# watchlist.py
"""
One user's watchlist, held as an instance-wide snapshot, so the renderer can
put a "Watchlist" sash on titles the user has queued.

Self-hosted only, by design.  The composite poster cache is shared by every
client of an instance, so a watchlist keyed per request would fragment that
cache per user and multiply upstream quota; a per-instance snapshot costs one
cheap call per refresh and nothing per poster.  On the public instance
WATCHLIST_SOURCE is simply unset and none of this runs.

Sources (WATCHLIST_SOURCE):

  mdblist   GET api.mdblist.com/watchlist/items with the server's MDBList key.
            MDBList mirrors a linked Trakt watchlist, which makes this the free
            route for Trakt users — Trakt's own API needs an app key that is
            VIP-gated as of August 2026.
  simkl     GET api.simkl.com/sync/all-items/{type}/{status} for the statuses
            in WATCHLIST_SIMKL_STATUSES.  Follows SIMKL's API rules: the list is
            only re-fetched after /sync/activities reports a change, the three
            media types are fetched sequentially, and every call carries the
            required client_id / app-name / app-version and a User-Agent.
            Auth is a device flow — the approval link is logged on first run.
            A V2 app gets a 7-day access token refreshed from its 180-day
            refresh token; a V1 app (what the developer page hands out today,
            retiring ~April 2027) gets a ~5-year token via the older PIN flow
            instead.  Both are persisted in app_state; which flow applies is
            discovered by asking SIMKL, not configured.
  trakt     GET api.trakt.tv/users/{TRAKT_USERNAME}/watchlist/{type} with a
            client id header only (public profile), or /sync/watchlist/{type}
            as the owner when TRAKT_ACCESS_TOKEN is set (private profile).
  <URL>     any MDBList list page, via its /json export — no key needed.

The snapshot is persisted so a restart neither blanks the marker nor treats
every title as newly added.  Each refresh diffs the new list against the old
one and hands the changed keys to main.py, which drops and re-renders only the
cached composites for those titles — the same replay mechanism the trending
refresh uses.  Membership itself is never baked into a cache key: adding a
title on the tracker changes the poster within one refresh interval, and
removing it does too.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable

import httpx

import config as _cfg
from cache import get_app_state, set_app_state
from tmdb import _normalise_trending_url, sanitise_source_url

logger = logging.getLogger(__name__)

_STATE_KEY_SNAPSHOT   = "watchlist_snapshot"
_STATE_KEY_SIMKL_TOK  = "simkl_tokens"
_STATE_KEY_SIMKL_ACT  = "simkl_activities"

_SIMKL_API   = "https://api.simkl.com"
_SIMKL_UA    = f"postersplus/{_cfg.APP_VERSION} (https://github.com/UmbraProjects/PostersPlus)"
_SIMKL_TYPES = ("movies", "shows", "anime")
# Movies have no watching/hold list; asking for one is a wasted call.
_SIMKL_MOVIE_STATUSES = frozenset({"plantowatch", "completed", "dropped"})
_SIMKL_ALL_STATUSES   = frozenset({"plantowatch", "watching", "hold", "completed", "dropped"})
# Refresh this far ahead of the access token's expiry rather than on a 401.
_SIMKL_REFRESH_AHEAD_SECS = 86400
# A device code lives 15 minutes; after that a new one is requested and logged
# again.  Spacing the re-prompts stops the log filling up on an instance whose
# operator has not noticed yet.
_SIMKL_DEVICE_RETRY_SECS = 3600

_TRAKT_API = "https://api.trakt.tv"

_MDBLIST_API       = "https://api.mdblist.com"
_MDBLIST_PAGE_SIZE = 500
_MDBLIST_MAX_PAGES = 40     # 20k titles — nobody's watchlist, but bounded

# Only the imdb / tmdb id and the kind are kept: those are the two keys a
# /poster request can be matched on.  "show" covers tv/series/anime series.
Kind = str  # "movie" | "show"


@dataclass(frozen=True)
class WatchlistEntry:
    imdb_id: str | None
    tmdb_id: str | None
    kind: Kind

    @property
    def keys(self) -> tuple[str | None, tuple[str, str] | None]:
        return (
            self.imdb_id,
            (self.tmdb_id, self.kind) if self.tmdb_id else None,
        )


@dataclass(frozen=True)
class Snapshot:
    imdb: frozenset[str]
    tmdb: frozenset[tuple[str, Kind]]
    fetched_at: float
    count: int

    @staticmethod
    def empty() -> "Snapshot":
        return Snapshot(frozenset(), frozenset(), 0.0, 0)

    @staticmethod
    def from_entries(entries: Iterable[WatchlistEntry], fetched_at: float) -> "Snapshot":
        imdb: set[str] = set()
        tmdb: set[tuple[str, Kind]] = set()
        count = 0
        for e in entries:
            count += 1
            if e.imdb_id:
                imdb.add(e.imdb_id)
            if e.tmdb_id:
                tmdb.add((e.tmdb_id, e.kind))
        return Snapshot(frozenset(imdb), frozenset(tmdb), fetched_at, count)


# Keys that changed between two snapshots, for main.py to regenerate.
@dataclass(frozen=True)
class SnapshotDiff:
    imdb: frozenset[str]
    tmdb: frozenset[tuple[str, Kind]]

    def __bool__(self) -> bool:
        return bool(self.imdb or self.tmdb)


_snapshot: Snapshot = Snapshot.empty()
_last_error: str | None = None
_loaded = False

# The SIMKL link code currently awaiting approval, published for the admin
# dashboard: {"user_code", "link", "expires_at", "flow"}.  None when no
# code is outstanding.  Set by the device/PIN flows, cleared when they end.
_simkl_pending: dict | None = None
# Set by request_refresh() to pull the loop out of its sleep — used to issue
# a link code on demand rather than on the hourly re-prompt.
_wake: asyncio.Event | None = None


# ---------------------------------------------------------------------------
# Public read API — what main.py calls per request
# ---------------------------------------------------------------------------

def normalise_kind(media_type: str | None) -> Kind:
    return "show" if (media_type or "").lower() in ("tv", "series", "show", "anime") else "movie"


def source_mode() -> str:
    """"mdblist" | "simkl" | "trakt" | "url" | "" (disabled)."""
    src = _cfg.WATCHLIST_SOURCE.strip()
    if not src:
        return ""
    low = src.lower()
    if low in ("mdblist", "simkl", "trakt"):
        return low
    if low.startswith("http://") or low.startswith("https://"):
        return "url"
    return "invalid"


def is_enabled() -> bool:
    return source_mode() in ("mdblist", "simkl", "trakt", "url")


def is_listed(imdb_id: str | None, tmdb_id: str | None, media_type: str | None) -> bool:
    """True when the title is in the current snapshot, by IMDb id or by
    (TMDB id, kind).  Cheap: two frozenset lookups, no I/O."""
    snap = _snapshot
    if not snap.count:
        return False
    if imdb_id and imdb_id in snap.imdb:
        return True
    if tmdb_id and (str(tmdb_id), normalise_kind(media_type)) in snap.tmdb:
        return True
    return False


def status() -> dict:
    """The snapshot's state, for /server-caps and /stats — what any client
    holding the access key may see.  Deliberately without the SIMKL link
    state: the pending code lets whoever approves it point the instance at
    their own watchlist, so that stays with the operator (link_status())."""
    return {
        "source":      source_mode(),
        "titles":      _snapshot.count,
        "fetched_at":  int(_snapshot.fetched_at) if _snapshot.fetched_at else None,
        "last_error":  _last_error,
    }


def link_status() -> dict:
    """status() plus, for SIMKL, whether the account is linked and the link
    code awaiting approval.  For the admin dashboard only."""
    out = status()
    if source_mode() == "simkl":
        pending = _simkl_pending
        if pending and pending["expires_at"] <= time.time():
            pending = None
        out["simkl"] = {
            "linked":  bool(_cfg.SIMKL_ACCESS_TOKEN or _simkl_load_tokens()),
            "pending": pending,
        }
    return out


def request_refresh() -> None:
    """Ask the loop to run a cycle now.  For SIMKL that also means issuing a
    fresh link code immediately when the account is not linked yet."""
    global _simkl_next_device_prompt
    _simkl_next_device_prompt = 0.0


async def simkl_unlink(client: httpx.AsyncClient) -> dict:
    """Forget the SIMKL grant: revoke it upstream when we can, drop the
    stored tokens, the activities fingerprint and the snapshot, and clear
    every cached poster that carried the marker.  The next refresh issues a
    fresh link code.

    A V2 grant is revoked through /oauth2/revoke (either token ends the
    grant).  V1 has no revoke endpoint, so that token stays valid at SIMKL
    until the user removes PostersPlus at simkl.com/settings/connected-apps
    — the result says which happened.
    """
    global _simkl_next_device_prompt
    tokens = _simkl_load_tokens()
    revoked = False
    if tokens and tokens.get("refresh_token"):
        try:
            form = {"client_id": _cfg.SIMKL_CLIENT_ID, "token": tokens["refresh_token"]}
            if _cfg.SIMKL_CLIENT_SECRET:
                form["client_secret"] = _cfg.SIMKL_CLIENT_SECRET
            resp = await client.post(
                f"{_SIMKL_API}/oauth2/revoke", data=form,
                headers={"User-Agent": _SIMKL_UA, "Content-Type": "application/x-www-form-urlencoded"},
                timeout=20.0,
            )
            # RFC 7009: always 200, so success cannot be confirmed — only attempted.
            revoked = resp.status_code == 200
        except Exception as exc:
            logger.warning(f"Watchlist: SIMKL revoke request failed: {exc}")
    set_app_state(_STATE_KEY_SIMKL_TOK, "")
    set_app_state(_STATE_KEY_SIMKL_ACT, "")
    changed = _apply([])
    _simkl_next_device_prompt = 0.0
    logger.info("Watchlist: SIMKL account unlinked" + (" (grant revoked)" if revoked else ""))
    return {
        "unlinked":  True,
        "revoked":   revoked,
        "had_token": bool(tokens),
        "flow":      "v2" if tokens and tokens.get("refresh_token") else ("v1" if tokens else None),
        "changed":   changed,
    }
    if _wake is not None:
        _wake.set()


# ---------------------------------------------------------------------------
# Snapshot persistence and diffing
# ---------------------------------------------------------------------------

def _source_signature() -> str:
    """Identity of the configured source, so a persisted snapshot from a
    different one is discarded rather than served.  Secrets never appear in
    it: the MDBList key is not part of the source, and a URL is reduced to
    its host and path."""
    mode = source_mode()
    if mode == "url":
        return "url:" + sanitise_source_url(_normalise_trending_url(_cfg.WATCHLIST_SOURCE))
    if mode == "simkl":
        return "simkl:" + ",".join(sorted(_cfg.WATCHLIST_SIMKL_STATUSES))
    if mode == "trakt":
        return "trakt:" + (_cfg.TRAKT_USERNAME.lower() if not _cfg.TRAKT_ACCESS_TOKEN else "me")
    return mode


def _persist(snapshot: Snapshot) -> None:
    set_app_state(_STATE_KEY_SNAPSHOT, json.dumps({
        "source":     _source_signature(),
        "fetched_at": snapshot.fetched_at,
        "imdb":       sorted(snapshot.imdb),
        "tmdb":       sorted(list(pair) for pair in snapshot.tmdb),
        "count":      snapshot.count,
    }))


def load_persisted() -> Snapshot:
    """Restore the last snapshot written for the *same* source, else empty."""
    global _snapshot, _loaded
    _loaded = True
    raw = get_app_state(_STATE_KEY_SNAPSHOT)
    if not raw:
        return _snapshot
    try:
        data = json.loads(raw)
        if data.get("source") != _source_signature():
            logger.info("Watchlist: persisted snapshot is for a different source — starting empty")
            return _snapshot
        _snapshot = Snapshot(
            imdb=frozenset(str(i) for i in data.get("imdb", [])),
            tmdb=frozenset((str(t), str(k)) for t, k in data.get("tmdb", [])),
            fetched_at=float(data.get("fetched_at") or 0.0),
            count=int(data.get("count") or 0),
        )
        logger.info(f"Watchlist: restored snapshot of {_snapshot.count} titles from the last run")
    except Exception as exc:
        logger.warning(f"Watchlist: could not restore persisted snapshot: {exc}")
    return _snapshot


def diff_snapshots(old: Snapshot, new: Snapshot) -> SnapshotDiff:
    return SnapshotDiff(
        imdb=frozenset(old.imdb ^ new.imdb),
        tmdb=frozenset(old.tmdb ^ new.tmdb),
    )


def _apply(entries: list[WatchlistEntry]) -> SnapshotDiff:
    """Replace the live snapshot, persist it, return what changed."""
    global _snapshot
    new = Snapshot.from_entries(entries, time.time())
    changed = diff_snapshots(_snapshot, new)
    _snapshot = new
    _persist(new)
    return changed


# ---------------------------------------------------------------------------
# Payload parsing — pure functions, unit-tested
# ---------------------------------------------------------------------------

def _clean_imdb(value) -> str | None:
    if not value:
        return None
    s = str(value).strip()
    return s if s.startswith("tt") and s[2:].isdigit() else None


def _clean_tmdb(value) -> str | None:
    if value is None or value == "":
        return None
    s = str(value).strip()
    return s if s.isdigit() else None


def parse_mdblist_items(items, default_kind: Kind | None = None) -> list[WatchlistEntry]:
    """MDBList rows — the same shape on /watchlist/items (under "movies" /
    "shows") and on a list's /json export (a bare array with "mediatype")."""
    out: list[WatchlistEntry] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        kind_raw = str(item.get("mediatype") or "").lower()
        kind = normalise_kind(kind_raw) if kind_raw else default_kind
        if kind is None:
            continue
        imdb = _clean_imdb(item.get("imdb_id"))
        tmdb = _clean_tmdb(item.get("id", item.get("tmdb_id", item.get("tmdbid"))))
        if imdb or tmdb:
            out.append(WatchlistEntry(imdb, tmdb, kind))
    return out


def parse_mdblist_watchlist(payload) -> list[WatchlistEntry]:
    if not isinstance(payload, dict):
        return []
    return (
        parse_mdblist_items(payload.get("movies"), "movie")
        + parse_mdblist_items(payload.get("shows"), "show")
    )


def parse_trakt_items(items, kind: Kind) -> list[WatchlistEntry]:
    """Trakt watchlist rows: {"type": "movie", "movie": {"ids": {...}}}."""
    out: list[WatchlistEntry] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        media = item.get("movie") if kind == "movie" else item.get("show")
        if not isinstance(media, dict):
            continue
        ids = media.get("ids") or {}
        imdb = _clean_imdb(ids.get("imdb"))
        tmdb = _clean_tmdb(ids.get("tmdb"))
        if imdb or tmdb:
            out.append(WatchlistEntry(imdb, tmdb, kind))
    return out


def parse_simkl_items(payload) -> list[WatchlistEntry]:
    """SIMKL /sync/all-items rows.  The top-level keys are "movies", "shows"
    and "anime"; each row wraps its media object under "movie" or "show"
    (anime rows use "show" too, with an "anime_type" saying whether it is a
    film).  An empty result is ``{}`` rather than empty lists."""
    out: list[WatchlistEntry] = []
    if not isinstance(payload, dict):
        return out
    for section in ("movies", "shows", "anime"):
        rows = payload.get(section)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            media = row.get("movie") or row.get("show") or row.get("anime")
            if not isinstance(media, dict):
                continue
            ids = media.get("ids") or {}
            if section == "movies":
                kind: Kind = "movie"
            elif section == "anime":
                kind = "movie" if str(row.get("anime_type") or "").lower() == "movie" else "show"
            else:
                kind = "show"
            imdb = _clean_imdb(ids.get("imdb"))
            tmdb = _clean_tmdb(ids.get("tmdb"))
            if imdb or tmdb:
                out.append(WatchlistEntry(imdb, tmdb, kind))
    return out


def simkl_activity_fingerprint(activities, statuses: Iterable[str]) -> str:
    """The parts of /sync/activities that can change the watchlist: the
    per-status timestamps for each media type, plus removed_from_list, which
    is the only signal for a deletion.  Ratings, history and settings are
    left out so they do not trigger a re-fetch."""
    if not isinstance(activities, dict):
        return ""
    wanted = sorted(set(statuses) | {"removed_from_list"})
    parts: list[str] = []
    for domain in ("movies", "tv_shows", "anime"):
        block = activities.get(domain)
        if not isinstance(block, dict):
            continue
        for key in wanted:
            value = block.get(key)
            if value:
                parts.append(f"{domain}.{key}={value}")
    return "|".join(parts)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

async def _fetch_mdblist(client: httpx.AsyncClient) -> list[WatchlistEntry]:
    key = _cfg.SERVER_MDBLIST_KEY
    if not key:
        raise RuntimeError("WATCHLIST_SOURCE=mdblist needs MDBLIST_API_KEY on the server")
    # Recorded so the quota-aware cache warmer sees these calls too.
    from ratings import _record_mdblist_quota

    entries: list[WatchlistEntry] = []
    offset = 0
    for _page in range(_MDBLIST_MAX_PAGES):
        logger.info(f"External API Call: MDBList watchlist (offset {offset})")
        resp = await client.get(
            f"{_MDBLIST_API}/watchlist/items",
            params={"apikey": key, "limit": _MDBLIST_PAGE_SIZE, "offset": offset},
            timeout=20.0,
        )
        _record_mdblist_quota(key, resp.headers)
        resp.raise_for_status()
        payload = resp.json()
        entries.extend(parse_mdblist_watchlist(payload))
        pagination = payload.get("pagination") if isinstance(payload, dict) else None
        if not isinstance(pagination, dict) or not pagination.get("has_more"):
            break
        offset += int(pagination.get("limit") or _MDBLIST_PAGE_SIZE)
    return entries


async def _fetch_mdblist_url(client: httpx.AsyncClient) -> list[WatchlistEntry]:
    url = _normalise_trending_url(_cfg.WATCHLIST_SOURCE)
    logger.info(f"External API Call: watchlist list ({sanitise_source_url(url)})")
    resp = await client.get(url, timeout=20.0, follow_redirects=True)
    resp.raise_for_status()
    payload = resp.json()
    if isinstance(payload, dict):
        # An MDBList API list-items response, should someone point us at one.
        return parse_mdblist_watchlist(payload) or parse_mdblist_items(payload.get("results"))
    return parse_mdblist_items(payload)


async def _fetch_trakt(client: httpx.AsyncClient) -> list[WatchlistEntry]:
    if not _cfg.TRAKT_CLIENT_ID:
        raise RuntimeError("WATCHLIST_SOURCE=trakt needs TRAKT_CLIENT_ID")
    headers = {
        "Content-Type":      "application/json",
        "trakt-api-version": "2",
        "trakt-api-key":     _cfg.TRAKT_CLIENT_ID,
    }
    if _cfg.TRAKT_ACCESS_TOKEN:
        headers["Authorization"] = f"Bearer {_cfg.TRAKT_ACCESS_TOKEN}"
        base = f"{_TRAKT_API}/sync/watchlist"
    else:
        if not _cfg.TRAKT_USERNAME:
            raise RuntimeError("WATCHLIST_SOURCE=trakt needs TRAKT_USERNAME (or TRAKT_ACCESS_TOKEN)")
        base = f"{_TRAKT_API}/users/{_cfg.TRAKT_USERNAME}/watchlist"

    entries: list[WatchlistEntry] = []
    for trakt_type, kind in (("movies", "movie"), ("shows", "show")):
        logger.info(f"External API Call: Trakt watchlist {trakt_type}")
        resp = await client.get(f"{base}/{trakt_type}", headers=headers, timeout=20.0)
        if resp.status_code in (401, 403):
            raise RuntimeError(
                f"Trakt returned HTTP {resp.status_code} — the profile is private "
                "(set TRAKT_ACCESS_TOKEN) or the client id is not valid"
            )
        resp.raise_for_status()
        entries.extend(parse_trakt_items(resp.json(), kind))
    return entries


# --- SIMKL -----------------------------------------------------------------

def _simkl_params() -> dict:
    return {
        "client_id":   _cfg.SIMKL_CLIENT_ID,
        "app-name":    "postersplus",
        "app-version": _cfg.APP_VERSION,
    }


def _simkl_headers(token: str | None) -> dict:
    h = {"User-Agent": _SIMKL_UA, "Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _simkl_load_tokens() -> dict | None:
    raw = get_app_state(_STATE_KEY_SIMKL_TOK)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if data.get("access_token") else None
    except Exception:
        return None


def _simkl_save_tokens(payload: dict) -> dict:
    now = time.time()
    data = {
        "access_token":  payload["access_token"],
        "refresh_token": payload.get("refresh_token"),
        "expires_at":    now + float(payload.get("expires_in") or 7 * 86400),
    }
    set_app_state(_STATE_KEY_SIMKL_TOK, json.dumps(data))
    return data


async def _simkl_token_request(client: httpx.AsyncClient, form: dict) -> httpx.Response:
    form = {"client_id": _cfg.SIMKL_CLIENT_ID, **form}
    if _cfg.SIMKL_CLIENT_SECRET:
        form["client_secret"] = _cfg.SIMKL_CLIENT_SECRET
    return await client.post(
        f"{_SIMKL_API}/oauth2/token",
        data=form,
        headers={"User-Agent": _SIMKL_UA, "Content-Type": "application/x-www-form-urlencoded"},
        timeout=20.0,
    )


async def _simkl_device_flow(client: httpx.AsyncClient) -> dict | None:
    """Run one device-code cycle: log the approval link, poll until the user
    approves or the code expires.  Returns saved tokens, or None.

    Tries the V2 device flow first; a V1 registration is refused there with
    ``invalid_client`` and is sent through the V1 PIN flow instead."""
    resp = await client.post(
        f"{_SIMKL_API}/oauth2/device",
        data={"client_id": _cfg.SIMKL_CLIENT_ID, "scope": "media:read"},
        headers={"User-Agent": _SIMKL_UA, "Content-Type": "application/x-www-form-urlencoded"},
        timeout=20.0,
    )
    if resp.status_code == 401:
        return await _simkl_pin_flow_v1(client)
    resp.raise_for_status()
    data = resp.json()
    device_code = data["device_code"]
    interval    = max(1, int(data.get("interval") or 5))
    expires_in  = float(data.get("expires_in") or 900)
    deadline    = time.monotonic() + expires_in
    link        = data.get("verification_uri_complete") or data.get("verification_uri")

    logger.warning(
        "Watchlist: SIMKL account not linked yet. Open this link, sign in and "
        f"approve PostersPlus: {link}  (code {data.get('user_code')}, valid 15 min) "
        "— the admin dashboard shows the same link and code"
    )
    _set_pending(data.get("user_code"), link, expires_in, "v2")
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            poll = await _simkl_token_request(client, {
                "grant_type":  "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
            })
            if poll.status_code == 200:
                tokens = _simkl_save_tokens(poll.json())
                logger.info("Watchlist: SIMKL account linked")
                return tokens
            try:
                err = poll.json().get("error")
            except Exception:
                err = None
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += 5
                continue
            if err == "expired_token":
                break
            if err == "access_denied":
                logger.warning("Watchlist: SIMKL link was declined by the user")
                return None
            raise RuntimeError(f"SIMKL device flow failed: HTTP {poll.status_code} {poll.text[:200]}")
        logger.warning("Watchlist: the SIMKL link code expired before it was approved — a new one will be issued")
        return None
    finally:
        _clear_pending()


def _set_pending(user_code: str | None, link: str | None, expires_in: float, flow: str) -> None:
    global _simkl_pending
    _simkl_pending = {
        "user_code":  user_code,
        "link":       link,
        "expires_at": int(time.time() + expires_in),
        "flow":       flow,
    }


def _clear_pending() -> None:
    global _simkl_pending
    _simkl_pending = None


async def _simkl_pin_flow_v1(client: httpx.AsyncClient) -> dict | None:
    """AUTH V1 PIN flow: GET /oauth/pin for a 5-character code, then poll
    GET /oauth/pin/{code} until ``{"result": "OK", "access_token": ...}``.
    The token lives about five years and has no refresh token, so it is
    saved with a far-off expiry and never refreshed."""
    params = {"client_id": _cfg.SIMKL_CLIENT_ID}
    headers = {"User-Agent": _SIMKL_UA}
    resp = await client.get(f"{_SIMKL_API}/oauth/pin", params=params, headers=headers, timeout=20.0)
    resp.raise_for_status()
    data = resp.json()
    user_code = data.get("user_code")
    if not user_code:
        raise RuntimeError(f"SIMKL PIN request returned no code: {resp.text[:200]}")
    interval   = max(1, int(data.get("interval") or 5))
    expires_in = float(data.get("expires_in") or 900)
    deadline   = time.monotonic() + expires_in
    verify     = data.get("verification_uri") or data.get("verification_url") or "https://simkl.com/pin"

    logger.warning(
        f"Watchlist: SIMKL account not linked yet. Open {verify} , sign in and "
        f"enter the code {user_code}  (valid 15 min) — the admin dashboard shows the same code"
    )
    _set_pending(user_code, verify, expires_in, "v1")
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            poll = await client.get(
                f"{_SIMKL_API}/oauth/pin/{user_code}", params=params, headers=headers, timeout=20.0,
            )
            try:
                body = poll.json()
            except Exception:
                body = {}
            if body.get("result") == "OK" and body.get("access_token"):
                # No expires_in on V1; ~5 years per the docs.
                tokens = _simkl_save_tokens({"access_token": body["access_token"], "expires_in": 5 * 365 * 86400})
                logger.info("Watchlist: SIMKL account linked (AUTH V1)")
                return tokens
            if body.get("result") == "KO":
                continue
            # A response carrying a fresh device_code means our code is gone —
            # expired and collected, or a typo on the user's side.
            if "device_code" in body:
                break
            raise RuntimeError(f"SIMKL PIN poll failed: HTTP {poll.status_code} {poll.text[:200]}")
        logger.warning("Watchlist: the SIMKL PIN code expired before it was entered — a new one will be issued")
        return None
    finally:
        _clear_pending()


async def _simkl_refresh(client: httpx.AsyncClient, tokens: dict) -> dict | None:
    if not tokens.get("refresh_token"):
        return None
    resp = await _simkl_token_request(client, {
        "grant_type":    "refresh_token",
        "refresh_token": tokens["refresh_token"],
    })
    if resp.status_code != 200:
        logger.warning(f"Watchlist: SIMKL token refresh failed (HTTP {resp.status_code}) — re-linking")
        set_app_state(_STATE_KEY_SIMKL_TOK, "")
        return None
    payload = resp.json()
    # Refresh is non-rotating: the same refresh token comes back.  Keep ours
    # in case the response omits it.
    payload.setdefault("refresh_token", tokens["refresh_token"])
    return _simkl_save_tokens(payload)


_simkl_next_device_prompt = 0.0


async def simkl_unlink(client: httpx.AsyncClient) -> dict:
    """Forget the SIMKL grant: revoke it upstream when we can, drop the
    stored tokens, the activities fingerprint and the snapshot, and clear
    every cached poster that carried the marker.  The next refresh issues a
    fresh link code.

    A V2 grant is revoked through /oauth2/revoke (either token ends the
    grant).  V1 has no revoke endpoint, so that token stays valid at SIMKL
    until the user removes PostersPlus at simkl.com/settings/connected-apps
    — the result says which happened.
    """
    global _simkl_next_device_prompt
    tokens = _simkl_load_tokens()
    revoked = False
    if tokens and tokens.get("refresh_token"):
        try:
            form = {"client_id": _cfg.SIMKL_CLIENT_ID, "token": tokens["refresh_token"]}
            if _cfg.SIMKL_CLIENT_SECRET:
                form["client_secret"] = _cfg.SIMKL_CLIENT_SECRET
            resp = await client.post(
                f"{_SIMKL_API}/oauth2/revoke", data=form,
                headers={"User-Agent": _SIMKL_UA, "Content-Type": "application/x-www-form-urlencoded"},
                timeout=20.0,
            )
            # RFC 7009: always 200, so success cannot be confirmed — only attempted.
            revoked = resp.status_code == 200
        except Exception as exc:
            logger.warning(f"Watchlist: SIMKL revoke request failed: {exc}")
    set_app_state(_STATE_KEY_SIMKL_TOK, "")
    set_app_state(_STATE_KEY_SIMKL_ACT, "")
    changed = _apply([])
    _simkl_next_device_prompt = 0.0
    logger.info("Watchlist: SIMKL account unlinked" + (" (grant revoked)" if revoked else ""))
    return {
        "unlinked":  True,
        "revoked":   revoked,
        "had_token": bool(tokens),
        "flow":      "v2" if tokens and tokens.get("refresh_token") else ("v1" if tokens else None),
        "changed":   changed,
    }


async def _simkl_access_token(client: httpx.AsyncClient) -> str | None:
    """A usable access token, refreshing or (re-)linking as needed.  None
    means "not linked yet" — the caller skips this cycle quietly."""
    global _simkl_next_device_prompt
    if _cfg.SIMKL_ACCESS_TOKEN:
        return _cfg.SIMKL_ACCESS_TOKEN
    tokens = _simkl_load_tokens()
    if tokens and tokens.get("expires_at", 0) - time.time() < _SIMKL_REFRESH_AHEAD_SECS:
        tokens = await _simkl_refresh(client, tokens)
    if tokens:
        return tokens["access_token"]
    if time.monotonic() < _simkl_next_device_prompt:
        return None
    _simkl_next_device_prompt = time.monotonic() + _SIMKL_DEVICE_RETRY_SECS
    tokens = await _simkl_device_flow(client)
    return tokens["access_token"] if tokens else None


async def _simkl_get(client: httpx.AsyncClient, path: str, token: str, **params) -> httpx.Response:
    return await client.get(
        f"{_SIMKL_API}{path}",
        params={**_simkl_params(), **params},
        headers=_simkl_headers(token),
        timeout=30.0,
    )


async def _fetch_simkl(client: httpx.AsyncClient) -> list[WatchlistEntry] | None:
    """Returns None when SIMKL reports nothing changed since the last fetch
    (or the account is not linked yet) — the caller keeps the snapshot."""
    if not _cfg.SIMKL_CLIENT_ID:
        raise RuntimeError("WATCHLIST_SOURCE=simkl needs SIMKL_CLIENT_ID")
    statuses = [s for s in _cfg.WATCHLIST_SIMKL_STATUSES if s in _SIMKL_ALL_STATUSES]
    if not statuses:
        raise RuntimeError("WATCHLIST_SIMKL_STATUSES names no valid SIMKL status")

    token = await _simkl_access_token(client)
    if not token:
        return None

    # Rule: never read the lists without asking /sync/activities first.
    logger.info("External API Call: SIMKL activities")
    resp = await _simkl_get(client, "/sync/activities", token)
    if resp.status_code == 401:
        # Expired early, or revoked.  One refresh attempt, then re-link.
        tokens = _simkl_load_tokens()
        tokens = await _simkl_refresh(client, tokens) if tokens else None
        if not tokens:
            return None
        token = tokens["access_token"]
        resp = await _simkl_get(client, "/sync/activities", token)
    resp.raise_for_status()
    fingerprint = simkl_activity_fingerprint(resp.json(), statuses)
    have_snapshot = _snapshot.fetched_at > 0
    if have_snapshot and fingerprint and fingerprint == (get_app_state(_STATE_KEY_SIMKL_ACT) or ""):
        return None

    # Sequential by design (SIMKL asks for it); a watchlist is small, so
    # extended=ids_only keeps each response to the ids we need.
    entries: list[WatchlistEntry] = []
    for simkl_type in _SIMKL_TYPES:
        for status_name in statuses:
            if simkl_type == "movies" and status_name not in _SIMKL_MOVIE_STATUSES:
                continue
            logger.info(f"External API Call: SIMKL {simkl_type}/{status_name}")
            r = await _simkl_get(
                client, f"/sync/all-items/{simkl_type}/{status_name}", token,
                extended="ids_only",
            )
            r.raise_for_status()
            entries.extend(parse_simkl_items(r.json()))
    set_app_state(_STATE_KEY_SIMKL_ACT, fingerprint)
    return entries


# ---------------------------------------------------------------------------
# Refresh cycle and loop
# ---------------------------------------------------------------------------

def _describe_error(exc: Exception) -> str:
    """A refresh failure as status() reports it.

    status() is public (/server-caps, /stats), and an httpx error's message
    carries the full request URL, query included — for the mdblist source
    that is ?apikey=<the server's key>.  So an httpx error is described by its
    status and a sanitised URL only.  Anything else is one of this module's
    own RuntimeErrors, whose message is written to be shown.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return (f"HTTP {exc.response.status_code} from "
                f"{sanitise_source_url(str(exc.request.url))}")
    if isinstance(exc, httpx.HTTPError):
        try:
            where = sanitise_source_url(str(exc.request.url))
        except RuntimeError:   # .request is unset on an error raised outside a request
            where = "upstream"
        return f"{type(exc).__name__} contacting {where}"
    return f"{type(exc).__name__}: {exc}"


async def refresh(client: httpx.AsyncClient) -> SnapshotDiff | None:
    """Fetch the configured source once and update the snapshot.

    Returns the diff (possibly empty) on success, None when the cycle was a
    no-op (source unchanged, account not linked) or failed.  A failure keeps
    the previous snapshot: a blip must not strip every marker until the next
    cycle puts them back.
    """
    global _last_error
    mode = source_mode()
    try:
        if mode == "mdblist":
            entries = await _fetch_mdblist(client)
        elif mode == "simkl":
            entries = await _fetch_simkl(client)
        elif mode == "trakt":
            entries = await _fetch_trakt(client)
        elif mode == "url":
            entries = await _fetch_mdblist_url(client)
        else:
            _last_error = f"WATCHLIST_SOURCE={_cfg.WATCHLIST_SOURCE!r} is not a recognised source"
            logger.error(f"Watchlist: {_last_error}")
            return None
    except Exception as exc:
        _last_error = _describe_error(exc)
        logger.error(f"Watchlist: refresh failed ({mode}): {_last_error} — keeping the previous snapshot")
        return None

    _last_error = None
    if entries is None:
        return None
    changed = _apply(entries)
    logger.info(
        f"Watchlist: {mode} snapshot has {_snapshot.count} titles"
        + (f", {len(changed.imdb) + len(changed.tmdb)} keys changed" if changed else ", unchanged")
    )
    return changed


async def watchlist_refresh_loop(
    client: httpx.AsyncClient,
    on_change: Callable[[SnapshotDiff], Awaitable[None]] | None = None,
) -> None:
    """Background task: restore the persisted snapshot, then refresh every
    WATCHLIST_REFRESH_MINUTES, calling *on_change* with each non-empty diff."""
    if not is_enabled():
        if source_mode() == "invalid":
            logger.error(
                f"Watchlist: WATCHLIST_SOURCE={_cfg.WATCHLIST_SOURCE!r} is not mdblist, simkl, "
                "trakt or an MDBList list URL — feature disabled"
            )
        return
    load_persisted()
    logger.info(
        f"Watchlist: source {source_mode()}, refreshing every {_cfg.WATCHLIST_REFRESH_MINUTES} min"
    )
    global _wake
    _wake = asyncio.Event()
    await asyncio.sleep(20)   # let startup settle before the first outbound call
    while True:
        _wake.clear()
        try:
            changed = await refresh(client)
            if changed and on_change is not None:
                await on_change(changed)
        except Exception as exc:
            logger.error(f"Watchlist: loop error: {exc}")
        # Sleep the interval, or less if request_refresh() wakes us.
        try:
            await asyncio.wait_for(_wake.wait(), timeout=_cfg.WATCHLIST_REFRESH_MINUTES * 60)
        except asyncio.TimeoutError:
            pass
