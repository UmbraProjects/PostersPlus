# admin.py
"""
The admin dashboard's API: the settings registry as a form, and the
instance's status as an overview.  The page itself is admin.html.

Access is gated by ADMIN_KEY, read straight from the environment and never
from the settings file — it is the one bootstrap secret, and a key that could
be changed from the page it protects would be no gate at all.  ACCESS_KEY is
deliberately not reused: it travels in every poster URL a client holds, so it
is shared with every user of the instance, not just its operator.  With no
ADMIN_KEY (or one shorter than ADMIN_KEY_MIN_LENGTH) the dashboard is
disabled and the page says how to enable it.

The key is accepted only in the X-Admin-Key header, never as a query
parameter: a query string is written to access logs and can be fired from an
<img> tag on another site.  (The page itself accepts ?admin_key= once, moves
it into session storage and scrubs the address bar; that URL is a page load,
not an API call.)  Wrong keys are slowed down and, past a handful per client,
locked out for a while, so the key cannot be guessed online.  The API sets
Cache-Control: no-store and the page refuses to be framed.

Saving writes the settings file (see settings.py).  Nothing is applied live —
every module captured its config at import — so the response says which keys
are waiting on a restart and the page shows the notice.

The SIMKL watchlist account is linked from here as well (the Watchlist group
of the page).  It is an operator action, one account per instance, and the
pending link code must not reach ordinary users: whoever approves it points
the instance at their own watchlist.
"""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import logging
import os
import signal
import time
from typing import Awaitable, Callable

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

import settings as _settings
import watchlist as _watchlist

router = APIRouter()
logger = logging.getLogger(__name__)

# Shorter than this and the dashboard stays off: an online-guessable key is
# worse than no dashboard, and the operator is told why in the log.
ADMIN_KEY_MIN_LENGTH = 12


def _validated_key(raw: str) -> str:
    raw = raw.strip()
    if raw and len(raw) < ADMIN_KEY_MIN_LENGTH:
        logger.error(
            f"ADMIN_KEY is {len(raw)} characters; the admin dashboard needs at least "
            f"{ADMIN_KEY_MIN_LENGTH} and stays disabled until it gets them"
        )
        return ""
    logger.info("Admin dashboard enabled at /admin" if raw else "Admin dashboard disabled (no ADMIN_KEY)")
    return raw


ADMIN_KEY = _validated_key(os.environ.get("ADMIN_KEY", ""))

# Wrong-key throttle, per client address: every failure costs a short delay,
# and past FAIL_LIMIT failures within FAIL_WINDOW the client is refused for
# LOCKOUT_SECS without the key even being compared.  Per process, so a
# multi-worker instance is a little more lenient; it is still nowhere near an
# online guess of a 12+ character key.
#
# "Client address" is whatever uvicorn puts in request.client, and uvicorn
# only believes X-Forwarded-For from FORWARDED_ALLOW_IPS (127.0.0.1 unless
# set).  Behind a reverse proxy on another address every visitor is the
# proxy, so anyone's eight bad keys lock the operator out too — the lockout
# log line says so, and CONFIGURATION.md documents the variable.
_FAIL_LIMIT   = 8
_FAIL_WINDOW  = 600.0
_LOCKOUT_SECS = 600.0
_FAIL_DELAY   = 0.4
# Bounds the two tables below: one entry per address that ever sent a bad key
# would otherwise grow for the life of the process.
_MAX_TRACKED  = 4096
_failures: dict[str, list[float]] = {}
_lockouts: dict[str, float] = {}

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_PAGE_HEADERS = {
    **_NO_STORE,
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
# Saved values are strings the environment could carry; nothing legitimate
# is anywhere near this, and the file must not become a dumping ground.
MAX_VALUE_LENGTH = 4096

_started_at = time.time()
_status_provider: Callable[[], Awaitable[dict]] | None = None
_html_loader: Callable[[], str] | None = None
_simkl_unlinker: Callable[[], Awaitable[dict]] | None = None


def register(
    status_provider: Callable[[], Awaitable[dict]],
    html_loader: Callable[[], str],
    simkl_unlinker: Callable[[], Awaitable[dict]],
) -> None:
    """main.py hands over the /stats builder (it owns the runtime counters),
    the page loader and the SIMKL unlink (it owns the poster cache the
    marker comes off), so this module stays free of main's imports."""
    global _status_provider, _html_loader, _simkl_unlinker
    _status_provider = status_provider
    _html_loader = html_loader
    _simkl_unlinker = simkl_unlinker


def enabled() -> bool:
    return bool(ADMIN_KEY)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _locked(ip: str) -> bool:
    until = _lockouts.get(ip)
    if until is None:
        return False
    if time.monotonic() >= until:
        _lockouts.pop(ip, None)
        _failures.pop(ip, None)
        return False
    return True


def _sweep(now: float) -> None:
    """Drop failure histories that have aged out of the window and lockouts
    that have lapsed, then, if the tables are still over _MAX_TRACKED, the
    addresses whose last failure is oldest.  An address that is currently
    locked out keeps its lockout either way."""
    for ip in [ip for ip, until in _lockouts.items() if now >= until]:
        del _lockouts[ip]
    for ip in [ip for ip, ts in _failures.items() if not ts or now - ts[-1] >= _FAIL_WINDOW]:
        del _failures[ip]
    overflow = len(_failures) - _MAX_TRACKED
    if overflow > 0:
        for ip in sorted(_failures, key=lambda k: _failures[k][-1])[:overflow]:
            del _failures[ip]
    overflow = len(_lockouts) - _MAX_TRACKED
    if overflow > 0:
        for ip in sorted(_lockouts, key=_lockouts.__getitem__)[:overflow]:
            del _lockouts[ip]


def _record_failure(ip: str) -> None:
    now = time.monotonic()
    if len(_failures) >= _MAX_TRACKED or len(_lockouts) >= _MAX_TRACKED:
        _sweep(now)
    recent = [t for t in _failures.get(ip, []) if now - t < _FAIL_WINDOW]
    recent.append(now)
    _failures[ip] = recent
    if len(recent) >= _FAIL_LIMIT:
        _lockouts[ip] = now + _LOCKOUT_SECS
        logger.warning(
            f"Admin: {ip} locked out for {int(_LOCKOUT_SECS)}s after {len(recent)} bad keys"
            + (_PROXY_HINT if _looks_like_proxy(ip) else "")
        )


_PROXY_HINT = (
    " — this is a private address, so if PostersPlus is behind a reverse proxy "
    "every visitor shares it and is locked out too. Set FORWARDED_ALLOW_IPS to "
    "the proxy's address so the real client address is used"
)


def _looks_like_proxy(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_private and not addr.is_loopback


def _key_matches(supplied: str) -> bool:
    # Bytes, not str: compare_digest on str raises for non-ASCII input, which
    # would turn a probe into a 500.
    return hmac.compare_digest(supplied.encode("utf-8"), ADMIN_KEY.encode("utf-8"))


async def _authorise(request: Request, x_admin_key: str) -> None:
    if not ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Admin dashboard disabled: set ADMIN_KEY on the server")
    ip = _client_ip(request)
    if _locked(ip):
        raise HTTPException(status_code=429, detail="Too many failed attempts; try again later",
                            headers={"Retry-After": str(int(_LOCKOUT_SECS))})
    if not x_admin_key or not _key_matches(x_admin_key):
        _record_failure(ip)
        await asyncio.sleep(_FAIL_DELAY)
        raise HTTPException(status_code=401, detail="Invalid admin key")
    _failures.pop(ip, None)


def _json(data: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(content=data, status_code=status, headers=_NO_STORE)


def _secret_hint(value: str) -> str:
    """Enough of a secret to recognise it, never enough to use it."""
    if not value:
        return ""
    if len(value) <= 6:
        return "•" * len(value)
    return "…" + value[-4:]


def _writable() -> bool:
    path = _settings.SETTINGS_PATH
    directory = os.path.dirname(path) or "."
    if os.path.exists(path):
        return os.access(path, os.W_OK)
    return os.access(directory, os.W_OK)


def describe_settings() -> dict:
    pending = set(_settings.pending_restart())
    groups: list[dict] = []
    by_group: dict[str, list[dict]] = {}
    for key, s in _settings.REGISTRY.items():
        value = _settings.resolve(key, s.default)
        env_value = os.environ.get(key)
        item = {
            "key":         key,
            "label":       s.label,
            "help":        s.help,
            "kind":        s.kind,
            "choices":     list(s.choices),
            "min":         s.min,
            "max":         s.max,
            "advanced":    s.advanced,
            "placeholder": s.placeholder,
            "default":     s.default,
            "show_if":     [s.show_if[0], list(s.show_if[1])] if s.show_if else None,
            "source":      _settings.source_of(key),
            "pending":     key in pending,
        }
        if s.kind == "secret":
            # The page never receives a secret; it learns whether one is set
            # and its tail, and sends a value only to replace it.
            item["value"] = ""
            item["has_value"] = bool(value)
            item["hint"] = _secret_hint(value)
            item["env_hint"] = _secret_hint(env_value) if env_value else ""
        else:
            item["value"] = value
            item["env_value"] = env_value
        by_group.setdefault(s.group, []).append(item)
    for name in _settings.ordered_groups():
        groups.append({"name": name, "settings": by_group.get(name, [])})
    return {
        "groups":          groups,
        "pending_restart": sorted(pending),
        "settings_path":   _settings.SETTINGS_PATH,
        "writable":        _writable(),
    }


@router.get("/admin", response_class=HTMLResponse)
async def admin_page():
    if _html_loader is None:
        raise HTTPException(status_code=503, detail="Admin page not registered")
    return HTMLResponse(_html_loader(), headers=_PAGE_HEADERS)


@router.get("/admin/api/session")
async def admin_session(request: Request, x_admin_key: str = Header(default="")):
    """Whether the dashboard is enabled at all, and whether the supplied key
    opens it.  Answers without a key so the page can explain itself — but a
    wrong key counts against the caller like everywhere else, so this is not
    a free oracle."""
    if not ADMIN_KEY:
        return _json({"enabled": False, "ok": False})
    if not x_admin_key:
        return _json({"enabled": True, "ok": False})
    try:
        await _authorise(request, x_admin_key)
    except HTTPException as exc:
        if exc.status_code == 429:
            raise
        return _json({"enabled": True, "ok": False})
    return _json({"enabled": True, "ok": True})


@router.get("/admin/api/settings")
async def admin_get_settings(request: Request, x_admin_key: str = Header(default="")):
    await _authorise(request, x_admin_key)
    return _json(describe_settings())


@router.put("/admin/api/settings")
async def admin_put_settings(request: Request, x_admin_key: str = Header(default="")):
    """Body: {"changes": {KEY: value | null}}.  A string sets the key in the
    settings file, null removes it so env / default show through.  Every
    value is validated before anything is written; one bad field rejects
    the whole save so the file never holds a half-applied edit."""
    await _authorise(request, x_admin_key)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Body must be JSON")
    changes = body.get("changes") if isinstance(body, dict) else None
    if not isinstance(changes, dict) or not changes:
        raise HTTPException(status_code=400, detail="No changes supplied")
    if len(changes) > len(_settings.REGISTRY):
        raise HTTPException(status_code=400, detail="Too many changes")

    normalised: dict[str, str | None] = {}
    errors: dict[str, str] = {}
    for key, raw in changes.items():
        setting = _settings.REGISTRY.get(str(key))
        if setting is None:
            errors[str(key)[:64]] = "unknown setting"
            continue
        if raw is None:
            normalised[key] = None
            continue
        if not isinstance(raw, (str, int, float, bool)):
            errors[key] = "must be a string"
            continue
        if isinstance(raw, str) and len(raw) > MAX_VALUE_LENGTH:
            errors[key] = f"longer than {MAX_VALUE_LENGTH} characters"
            continue
        try:
            normalised[key] = _settings.normalise(setting, raw)
        except ValueError as exc:
            errors[key] = str(exc)
    if errors:
        return _json({"errors": errors}, status=400)

    if not _writable():
        raise HTTPException(
            status_code=500,
            detail=f"{_settings.SETTINGS_PATH} is not writable — is the cache volume mounted?",
        )
    try:
        _settings.save(normalised)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not write settings: {exc}")
    out = describe_settings()
    out["saved"] = sorted(normalised)
    return _json(out)


def _server_pid() -> int:
    """The process to signal for a graceful restart.

    Under `--workers N` this request runs in a worker whose parent is the
    uvicorn supervisor; signalling the supervisor stops every worker and
    exits.  With one worker uvicorn runs in-process and the parent is tini,
    so we signal ourselves.  Outside Docker the parent could be a shell —
    only a parent that is itself uvicorn is ever signalled.
    """
    ppid = os.getppid()
    try:
        with open(f"/proc/{ppid}/cmdline", "rb") as fh:
            if b"uvicorn" in fh.read():
                return ppid
    except OSError:
        pass
    return os.getpid()


def _terminate(pid: int) -> None:
    logger.warning(f"Admin: restart requested — sending SIGTERM to pid {pid}")
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        logger.error(f"Admin: could not signal pid {pid}: {exc}")


@router.post("/admin/api/restart")
async def admin_restart(request: Request, x_admin_key: str = Header(default="")):
    """Stop the server so the container's restart policy starts it again
    with the saved settings.  The response goes out first; the signal
    follows a moment later.  Without a restart policy (compose.yaml ships
    with `restart: unless-stopped`) the container simply stays stopped,
    which is why the page asks before calling this."""
    await _authorise(request, x_admin_key)
    pid = _server_pid()
    asyncio.get_running_loop().call_later(0.5, _terminate, pid)
    return _json({"restarting": True, "pid": pid, "pending_restart": _settings.pending_restart()})


@router.get("/admin/api/status")
async def admin_status(request: Request, x_admin_key: str = Header(default="")):
    await _authorise(request, x_admin_key)
    stats = await _status_provider() if _status_provider is not None else {}
    # /stats carries the public snapshot; the operator also gets the link state.
    stats["watchlist"] = _watchlist.link_status()
    stats["uptime_secs"] = int(time.time() - _started_at)
    stats["pending_restart"] = _settings.pending_restart()
    stats["settings_file"] = {
        "path":     _settings.SETTINGS_PATH,
        "keys":     len(_settings.file_values()),
        "writable": _writable(),
    }
    return _json(stats)


# ---------------------------------------------------------------------------
# SIMKL account linking.  The refresh loop in watchlist.py issues the code
# and polls SIMKL for the approval; these endpoints only expose that state
# and nudge the loop.
# ---------------------------------------------------------------------------

def _require_simkl() -> None:
    if _watchlist.source_mode() != "simkl":
        raise HTTPException(status_code=400, detail="WATCHLIST_SOURCE is not simkl in the running process")


@router.get("/admin/api/watchlist")
async def admin_watchlist(request: Request, x_admin_key: str = Header(default="")):
    """The watchlist snapshot's state plus, for SIMKL, whether the account
    is linked and the link code awaiting approval.  Polled by the page
    while a link is pending."""
    await _authorise(request, x_admin_key)
    return _json(_watchlist.link_status())


@router.post("/admin/api/watchlist/simkl/link")
async def admin_simkl_link(request: Request, x_admin_key: str = Header(default="")):
    """Issue a SIMKL link code now instead of on the loop's hourly re-prompt.
    A code already awaiting approval is returned as-is rather than replaced —
    SIMKL only honours the one being polled."""
    await _authorise(request, x_admin_key)
    _require_simkl()
    current = _watchlist.link_status()
    simkl_state = current.get("simkl") or {}
    if not simkl_state.get("linked") and not simkl_state.get("pending"):
        _watchlist.request_refresh()
    return _json(current)


@router.post("/admin/api/watchlist/simkl/unlink")
async def admin_simkl_unlink(request: Request, x_admin_key: str = Header(default="")):
    """Forget the linked SIMKL account (revoking a V2 grant upstream), drop
    the snapshot and re-render the posters that carried the marker."""
    await _authorise(request, x_admin_key)
    _require_simkl()
    if _simkl_unlinker is None:
        raise HTTPException(status_code=503, detail="Unlink not registered")
    result = await _simkl_unlinker()
    result["status"] = _watchlist.link_status()
    return _json(result)
