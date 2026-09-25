# settings.py
"""
The settings registry behind config.py and the admin dashboard.

Every operator-facing setting is declared exactly once, in config.py, through
``env()`` below.  The call records the setting's metadata (group, kind, help
text, choices, bounds) in REGISTRY and returns the raw string value config.py
then parses, exactly as ``os.environ.get`` did before.  That single
declaration is what the admin dashboard renders, validates against and
documents from, so a new setting is a new ``env()`` call and nothing else.

Where a value comes from, in order:

  1. the settings file — what the operator saved in the admin dashboard,
  2. the environment — .env / compose, as before,
  3. the declared default.

The file wins over the environment on purpose: a change made in the UI has
to take effect, and an env var that silently pinned a field would look like a
broken dashboard.  Each field shows which of the three it is using, and
"reset" drops the file value so the env or default shows through again.

Values are only read at import, because every module captures its config
constants at import time.  A save therefore takes effect on the next start;
``pending_restart()`` reports which keys differ between the file and the
running process so the dashboard can say so.
"""
from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Lives in the cache volume so it survives image upgrades like the cache does.
SETTINGS_PATH = os.environ.get("SETTINGS_PATH", "/app/cache/settings.json").strip()

# What a field looks like in the dashboard, and how a submitted value is
# checked.  Every kind is stored as a string, the way the environment would
# carry it; parsing stays in config.py.
KINDS = ("text", "secret", "int", "float", "bool", "choice", "list", "url")


@dataclass
class Setting:
    key: str
    default: str
    group: str
    kind: str = "text"
    label: str = ""
    help: str = ""
    choices: tuple[str, ...] = ()
    min: float | None = None
    max: float | None = None
    # Shown behind the group's "advanced" fold — the ADVANCED.md tier.
    advanced: bool = False
    placeholder: str = ""
    # Only meaningful when another setting has one of these values:
    # ("QUALITY_SOURCE", ("scraper",)).  ("KEY", ("*",)) means "when KEY is
    # non-empty".  The dashboard hides the field otherwise and the README
    # says so; the value is still read and stored either way.
    show_if: tuple[str, tuple[str, ...]] | None = None
    order: int = field(default=0, compare=False)


REGISTRY: dict[str, Setting] = {}
GROUPS: list[str] = []

# The dashboard's sidebar order: what an operator sets first at the top,
# tuning knobs at the bottom.  Groups declared but not listed here follow in
# declaration order.
GROUP_ORDER = (
    "API keys",
    "Access & serving",
    "Quality source",
    "Output",
    "Trending",
    "Watchlist",
    "Ratings",
    "Caching",
    "Cache warming",
    "TVDB fallback art",
    "Cinemeta fallback",
    "Anime sources",
    "Rendering",
    "Text detection",
    "Performance",
)


def ordered_groups() -> list[str]:
    known = [g for g in GROUP_ORDER if g in GROUPS]
    return known + [g for g in GROUPS if g not in known]

_lock = threading.Lock()
_file_values: dict[str, str] = {}
_file_loaded = False
# The raw value each key resolved to when this process imported config —
# what the process is actually running with.
_running: dict[str, str] = {}


def _load_file() -> dict[str, str]:
    global _file_values, _file_loaded
    if _file_loaded:
        return _file_values
    _file_loaded = True
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            _file_values = {str(k): str(v) for k, v in data.items() if v is not None}
    except FileNotFoundError:
        pass
    except Exception as exc:
        # A corrupt file must not stop the service: run on env + defaults and
        # say why the dashboard's values are not being honoured.
        logger.error(f"Settings file {SETTINGS_PATH} could not be read ({exc}); using environment and defaults")
    return _file_values


def env(
    key: str,
    default: str = "",
    *,
    group: str,
    kind: str = "text",
    label: str = "",
    help: str = "",
    choices: tuple[str, ...] = (),
    min: float | None = None,
    max: float | None = None,
    advanced: bool = False,
    placeholder: str = "",
    show_if: tuple[str, str | tuple[str, ...]] | None = None,
) -> str:
    """Declare a setting and return its raw string value.

    ``default`` is the string the environment would carry, so ``env("X",
    "3")`` and the old ``os.environ.get("X", "3")`` are interchangeable.
    """
    assert kind in KINDS, kind
    if key not in REGISTRY:
        dep = None
        if show_if is not None:
            dep_key, dep_values = show_if
            if isinstance(dep_values, str):
                dep_values = (dep_values,)
            dep = (dep_key, tuple(v.lower() for v in dep_values))
        REGISTRY[key] = Setting(
            key=key, default=default, group=group, kind=kind, label=label or key,
            help=help, choices=tuple(choices), min=min, max=max, advanced=advanced,
            placeholder=placeholder, show_if=dep, order=len(REGISTRY),
        )
        if group not in GROUPS:
            GROUPS.append(group)
    value = resolve(key, default)
    _running[key] = value
    return value


def resolve(key: str, default: str = "") -> str:
    """The value the precedence rules give right now (file > env > default)."""
    file_values = _load_file()
    if key in file_values:
        return file_values[key]
    return os.environ.get(key, default)


def source_of(key: str) -> str:
    """"file" | "env" | "default" — where the current value comes from."""
    if key in _load_file():
        return "file"
    if key in os.environ:
        return "env"
    return "default"


def file_values() -> dict[str, str]:
    return dict(_load_file())


def running_value(key: str) -> str | None:
    return _running.get(key)


def pending_restart() -> list[str]:
    """Keys whose current resolved value differs from what this process
    started with — saved, but not applied until a restart."""
    out = []
    for key, setting in REGISTRY.items():
        if key in _running and resolve(key, setting.default) != _running[key]:
            out.append(key)
    return out


# ---------------------------------------------------------------------------
# Validation and persistence
# ---------------------------------------------------------------------------

_TRUE  = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def normalise(setting: Setting, raw) -> str | None:
    """Check a submitted value against the setting's kind and bounds and
    return the string to store, or None when the field should fall back to
    the environment / default instead.  Raises ValueError with a message the
    dashboard can show next to the field."""
    if isinstance(raw, bool):
        raw = "true" if raw else "false"
    value = str(raw if raw is not None else "").strip()
    kind = setting.kind

    if value == "":
        # A blank number, switch or choice cannot be parsed by config.py, so
        # it means "no override".  A blank text field is a real value: it is
        # how an operator blanks out a key the environment still carries.
        return None if kind in ("int", "float", "bool", "choice") else ""

    if kind == "bool":
        low = value.lower()
        if low in _TRUE:
            return "true"
        if low in _FALSE:
            return "false"
        raise ValueError("must be true or false")

    if kind == "int":
        try:
            number: float = int(value)
        except ValueError:
            raise ValueError("must be a whole number")
        _check_bounds(setting, number)
        return str(int(number))

    if kind == "float":
        try:
            number = float(value)
        except ValueError:
            raise ValueError("must be a number")
        # float() takes "nan" and "inf".  NaN passes every bounds check (it
        # compares false both ways) and inf passes an unbounded one, and either
        # is saved only to break whatever sleeps or sizes on it after a restart.
        if not math.isfinite(number):
            raise ValueError("must be a finite number")
        _check_bounds(setting, number)
        return value

    if kind == "choice":
        low = value.lower()
        if setting.choices and low not in setting.choices:
            raise ValueError("must be one of " + ", ".join(setting.choices))
        return low

    if kind == "url":
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError("must start with http:// or https://")
        return value

    if kind == "list":
        return ",".join(part.strip() for part in value.split(",") if part.strip())

    return value


def _check_bounds(setting: Setting, number: float) -> None:
    if setting.min is not None and number < setting.min:
        raise ValueError(f"must be at least {setting.min:g}")
    if setting.max is not None and number > setting.max:
        raise ValueError(f"must be at most {setting.max:g}")


def save(changes: dict[str, str | None]) -> dict[str, str]:
    """Apply *changes* to the settings file: a string sets the key, None
    removes it (so env / default show through).  Values are already
    normalised.  Written atomically; returns the new file contents."""
    global _file_values
    with _lock:
        current = dict(_load_file())
        for key, value in changes.items():
            if value is None:
                current.pop(key, None)
            else:
                current[key] = value
        directory = os.path.dirname(SETTINGS_PATH) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(current, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, SETTINGS_PATH)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        _file_values = current
        return dict(current)


def _reset_for_tests(path: str | None = None) -> None:
    """Forget the loaded file (and optionally point at another one)."""
    global SETTINGS_PATH, _file_values, _file_loaded
    if path is not None:
        SETTINGS_PATH = path
    _file_values = {}
    _file_loaded = False
