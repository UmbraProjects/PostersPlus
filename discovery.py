"""
discovery.py — "One interesting thing" sash logic.

Caches all discoverable facts about a title, then picks the single highest-
priority one to show, based on a caller-supplied ordered list.

Priority slots
--------------
wins         Oscar Best Picture win / Major Emmy (Outstanding) Series win
gg_wins      Golden Globe win (all top film + TV categories)
festival     Festival prize (Cannes, Venice, Berlin, Locarno, Sundance) — see festivals.py
pic_noms     Best Picture nomination (film) / Major Emmy nomination (TV)
gg_noms      Golden Globe nomination (all top film + TV categories)
studio       Produced by a notable studio (A24, Pixar, …)
director     Directed by a notable filmmaker
cast         Stars a notable actor / actress
trending     Currently trending on TMDB top 40
new_season   TV show with a fresh/upcoming S2+ season premiere
returning    TV show with a fresh/upcoming non-premiere episode
premiere     Show initial release within the last 14 days
just_added   Movie with a fresh TMDB digital/TV release date
season_finale Recently-ended TV final season
blockbuster  Top-grossing film of its release year (box_office.py)
cult         Cult Classic / Cult Film (MDblist keyword)
foreign      Non-English original language film
new_release  Legacy combined newly released signal
metacritic   Metacritic Must-See badge (curated critical acclaim)
true_story   Based on a true story (MDblist keyword)
structural   Short Film | Mini Series | Binge Ready (whichever matches first)

Legacy aliases (still accepted in sash_priority for backward-compat with old URLs):
    emmy_noms       → pic_noms
    digital_release → new_release
    noms            → any nomination (catch-all)

All facts are stored in DiscoveryMeta so they only need to be computed once
and can be re-prioritised at render time without re-fetching.

Operator customisation
----------------------
Self-hosters can supply their own director / studio / cast lists without
editing this file.  The admin dashboard's Sash lists view edits them (it
writes the file below, and every worker reloads it within seconds), or place
a JSON file by hand at:

    /app/cache/discovery_overrides.json

This sits inside the existing cache volume mount (./postersplus-cache:/app/cache)
so no extra volume entry is needed in compose.yaml.  The path can be changed
via the DISCOVERY_OVERRIDES_PATH environment variable.

File format:
    {
      "mode": "replace",          -- "replace" (default) or "merge"
      "studios":   { "Studio Name": "Display Label", ... },
      "directors": { "Director Name": "Display Label", ... },
      "cast":      { "Actor Name": "Display Label", ... }
    }

"replace" (default): each provided section fully replaces the built-in list.
  Omit a section to keep its built-in defaults untouched.
"merge": provided entries are added to (and override) the built-in lists.
  Useful for adding names without losing the defaults.

See discovery_overrides.example.json for a full sample file.
"""

from __future__ import annotations

import json
import logging
import os
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime

from config import SASH_PRIORITY as DEFAULT_SASH_PRIORITY  # single source of truth
import config as _cfg
from festivals import festival_label as resolve_festival_label, match_festival_keyword

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Curated lists
# ---------------------------------------------------------------------------


# Keys are the exact TMDB credit name to match against.
# Values are the display string shown in the sash.
# To customise the label without changing the match, set a different value:
#   "Studio Ghibli": "Ghibli"   →  sash shows "By Ghibli"
#   "Studio Ghibli": "Studio Ghibli"  →  sash shows "By Studio Ghibli"

NOTABLE_STUDIOS: dict[str, str] = {
    "A24":                    "A24 Films",
    "Pixar":                  "Pixar Studio",
    "Studio Ghibli":          "Studio Ghibli",
    "Blumhouse Productions":  "Blumhouse",
    "Neon":                   "NEON Rated",
    "Searchlight Pictures":   "SL Pictures",
    "BBC Film":               "BBC Films",
    "Bad Robot":              "Bad Robot",
    "HBO":                    "HBO Original",
    "LAIKA":                  "Laika",
}

# Keys are the exact TMDB credit name to match against.
# Values are the display string shown in the sash.
# Example: "Christopher Nolan": "Nolan"  →  sash shows "By Nolan"

NOTABLE_DIRECTORS: dict[str, str] = {
    "Christopher Nolan":   "C. Nolan",
    "Denis Villeneuve":    "D. Villeneuve",
    "Martin Scorsese":     "M. Scorsese",
    "Wes Anderson":        "Wes Anderson",
    "Sofia Coppola":       "S. Coppola",
    "Bong Joon Ho":        "B. Joon-ho",
    "Hayao Miyazaki":      "H. Miyazaki",
    "David Fincher":       "D. Fincher",
    "Paul Thomas Anderson":"P.T. Anderson",
    "Quentin Tarantino":   "Q. Tarantino",
    "Alfonso Cuarón":      "A. Cuarón",
    "Guillermo del Toro":  "G. del Toro",
    "Ridley Scott":        "R. Scott",
    "Steven Spielberg":    "S. Spielberg",
    "Joel Coen":           "Coen Brothers",
    "Ethan Coen":          "Coen Brothers",
    "David Lynch":         "D. Lynch",
    "Darren Aronofsky":    "D. Aronofsky",
    "Yorgos Lanthimos":    "Y. Lanthimos",
    "Ari Aster":           "Ari Aster",
    "Jordan Peele":        "J. Peele",
    "Greta Gerwig":        "G. Gerwig",
    "Robert Eggers":       "R. Eggers",
    "Céline Sciamma":      "C. Sciamma",
    "Park Chan-wook":      "P. Chan-wook",
    "Wong Kar-Wai":        "Wong Kar-wai",
    "Hirokazu Kore-eda":   "H. Kore-eda",
    "Luca Guadagnino":     "L. Guadagnino",
    "Sean Baker":          "Sean Baker",
    "Stanley Kubrick":     "S. Kubrick",
    "Spike Lee":           "Spike Lee",
    "David Cronenberg":    "D. Cronenberg",
    "Michael Mann":        "Michael Mann",
    "Francis Ford Coppola":"F.F Coppola",
    "Jane Campion":        "J. Campion",
    "Terrence Malick":     "T. Malick",
    "Mike Flanagan":       "M. Flanagan",
    "James Cameron":       "J. Cameron",
    "Peter Jackson":       "P. Jackson",
}


# Keys are the exact TMDB credit name to match against.
# Values are the display string shown in the sash.
# Keep values short — the sash is narrow. First name + last name usually fits;
# drop first names or use initials where needed.

NOTABLE_CAST: dict[str, str] = {
    "Cate Blanchett":    "Cate Blanchett",
    "Meryl Streep":      "Meryl Streep",
    "Viola Davis":       "Viola Davis",
    "Tilda Swinton":     "Tilda Swinton",
    "Joaquin Phoenix":   "Joaquin Phoenix",
    "Daniel Day-Lewis":  "D. Day-Lewis",
    "Tom Hanks":         "Tom Hanks",
    "Denzel Washington": "D. Washington",
    "Leonardo DiCaprio": "L. DiCaprio",
    "Natalie Portman":   "Natalie Portman",
    "Nicole Kidman":     "Nicole Kidman",
    "Julianne Moore":    "Julianne Moore",
    "Jessica Lange":     "Jessica Lange",
    "Anthony Hopkins":   "Anthony Hopkins",
    "Gary Oldman":       "Gary Oldman",
    "Ryan Gosling":      "Ryan Gosling",
    "Margot Robbie":     "Margot Robbie",
    "Adam Driver":       "Adam Driver",
    "Saoirse Ronan":     "Saoirse Ronan",
    "Oscar Isaac":       "Oscar Isaac",
    "Mahershala Ali":    "Mahershala Ali",
    "Lupita Nyong'o":    "Lupita Nyong'o",
    "Pedro Pascal":      "Pedro Pascal",
    "Jeff Bridges":      "Jeff Bridges",
    "Charlize Theron":   "Charlize Theron",
    "Timothée Chalamet": "T. Chalamet",
    "Zendaya":           "Zendaya",
    "Florence Pugh":     "Florence Pugh",
    "Austin Butler":     "Austin Butler",
    "Barry Keoghan":     "Barry Keoghan",
    "Paul Mescal":       "Paul Mescal",
    "Carey Mulligan":    "Carey Mulligan",
    "Andrew Garfield":   "Andrew Garfield",
    "Ana de Armas":      "Ana de Armas",
    "Anya Taylor-Joy":   "Anya Taylor-Joy",
    "Frances McDormand":      "F. McDormand",
    "Robert De Niro":         "Robert De Niro",
    "Al Pacino":              "Al Pacino",
    "Willem Dafoe":           "Willem Dafoe",
    "Philip Seymour Hoffman": "P. Hoffman",
    "Jake Gyllenhaal":        "Jake Gyllenhaal",
    "Emma Stone":             "Emma Stone",
    "Christian Bale":         "Christian Bale",
    "Colin Farrell":          "Colin Farrell",
    "Rachel McAdams":         "Rachel McAdams",
    "Amy Adams":              "Amy Adams",
    "Jeremy Strong":          "Jeremy Strong",
    "Ayo Edebiri":            "Ayo Edebiri",
    "Kieran Culkin":          "Kieran Culkin",
    "Jeremy Allen White":     "J. White",
    "Mia Goth":               "Mia Goth",
    "Sebastian Stan":         "Sebastian Stan",
    "Harris Dickinson":       "H. Dickinson",
    "Mikey Madison":          "Mikey Madison",
    "Josh O'Connor":          "Josh O'Connor",
}

# Within the "structural" bucket, checked in this fixed order
_STRUCTURAL_CHECKS = ["short_film", "mini_series", "binge_ready"]

_STRUCTURAL_LABELS: dict[str, str] = {
    "short_film":  "Short Film",
    "mini_series": "Mini Series",
    "binge_ready": "Binge Ready",
}

# Festival sashes live in festivals.py — the keyword table, the top-prize TMDB
# id sets, and the two-tier label rule.  See that module for why an MDblist
# festival keyword cannot be read as "won the top prize" on its own.

# ISO 639-1 language code → display label shown on the sash.
# English is intentionally absent — foreign slot only fires for non-English.
# Add languages here; unlisted non-English languages fall back to
# "Foreign Language Film" to ensure the slot always has a label.
LANGUAGE_LABELS: dict[str, str] = {
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "it": "Italian",
    "pt": "Portuguese",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
    "da": "Danish",
    "sv": "Swedish",
    "no": "Norwegian",
    "fi": "Finnish",
    "nl": "Dutch",
    "pl": "Polish",
    "ru": "Russian",
    "tr": "Turkish",
    "ar": "Arabic",
    "hi": "Hindi",
    "fa": "Persian",
    "ro": "Romanian",
    "hu": "Hungarian",
    "cs": "Czech",
    "he": "Hebrew",
    "el": "Greek",
    "te": "Telugu",
    "ta": "Tamil",
    "ml": "Malayalam",
    "kn": "Kannada",
    "bn": "Bengali",
    "mr": "Marathi",
    "pa": "Punjabi",
    "gu": "Gujarati",
    "ur": "Urdu",
    "th": "Thai",
    "vi": "Vietnamese",
    "id": "Indonesian",
    "ms": "Malay",
    "tl": "Filipino",
    "uk": "Ukrainian",
    "is": "Icelandic",
    "ca": "Catalan",
    "sr": "Serbian",
    "hr": "Croatian",
    "bg": "Bulgarian",
    "sk": "Slovak",
    "et": "Estonian",
    "lv": "Latvian",
    "lt": "Lithuanian",
    "ka": "Georgian",
    "af": "Afrikaans",
    "sw": "Swahili",
}

# Sash type (controls colour) for each priority slot
_SASH_TYPES: dict[str, str] = {
    "watchlist":       "watchlist", # amber — the user's own queue (instance-wide, see watchlist.py)
    "wins":            "win",       # gold — Oscar Winner + Emmy Winner
    "gg_wins":         "win",       # gold — Globe Winner (separate slot)
    "pic_noms":        "nom",       # silver — Oscar Nominee + Emmy Nominee (film vs TV, never coexist)
    "gg_noms":         "nom",       # silver — Globe Nominee
    "emmy_noms":       "nom",       # silver — legacy alias for pic_noms
    "noms":            "nom",       # silver — legacy catch-all for any nomination
    "festival":        "win",       # gold — major festival win is prestige-equivalent to Oscar
    "studio":          "prestige",  # purple — production credit
    "director":        "prestige",  # purple — production credit
    "cast":            "cast",      # green — talent credit
    "trending":        "trending",  # blue
    "trending_broad":  "trending",  # blue — lower-priority ranks 41-100
    "new_season":      "alert",     # red — timely TV lifecycle signal
    "returning":        "alert",     # red — timely TV lifecycle signal
    "premiere":         "alert",     # red — show initial release date recency
    "just_added":       "alert",     # red — fresh movie digital/TV release
    "season_finale":    "alert",     # red — final-season/finale signal
    "blockbuster":     "trending",  # blue — popularity signal, like cult
    "cult":            "trending",  # blue — popularity signal, closest to trending without a new colour
    "foreign":         "info",      # teal — informational / discovery
    "new_release":     "alert",     # red — legacy combined newly released signal
    "digital_release": "alert",     # red — legacy alias for new_release
    "metacritic":      "nom",       # silver — critical award, fits with noms not production
    "true_story":      "info",      # teal
    "structural":      "info",      # teal
    "release_status":  "alert",     # red — Physical / Streaming / Cinema / Production
    "short_film":      "info",
    "mini_series":     "info",
    "binge_ready":     "info",
    "cinema":          "alert",
    "streaming":       "alert",
    "physical":        "alert",
    "production":      "alert",
    "ended":           "alert",
    "cancelled":       "alert",
    "airing":          "alert",
    "renewed":         "alert",
}

# One slot per release status, named after it.  "release_status" in a sash
# priority expands to all of them.
RELEASE_STATUS_SLOTS: tuple[str, ...] = (
    "cinema", "streaming", "physical", "production", "ended", "cancelled", "airing", "renewed",
)

NEW_RELEASE_DAYS = 14
TV_RETURNING_LOOKAHEAD_DAYS = 14
TV_RECENT_EPISODE_DAYS = 14


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _is_recent(release_date: str | None, *, max_days: int = NEW_RELEASE_DAYS) -> bool:
    """Return True if *release_date* (YYYY-MM-DD) is within *max_days* of today."""
    rd = _parse_date(release_date)
    if rd is None:
        return False
    age = (date.today() - rd).days
    return 0 <= age <= max_days


def _is_recent_or_upcoming(value: str | None) -> bool:
    rd = _parse_date(value)
    if rd is None:
        return False
    delta = (rd - date.today()).days
    return -TV_RECENT_EPISODE_DAYS <= delta <= TV_RETURNING_LOOKAHEAD_DAYS


_MONTHS_SHORT = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def release_date_label(
    release_date: str | None,
    window: str | None,
    *,
    today: date | None = None,
) -> str | None:
    """"Oct 16 Cinema" for a future *release_date*, "Dec 2027 Cinema" once it is
    a year or more away, None for anything absent or already passed.

    *window* is the status the date opens — Cinema / Streaming / Physical — and
    it is part of the label because the date alone would overpromise: a
    theatrical date is not one the viewer can watch at home.

    The day form is unambiguous for the next twelve months — "Feb 10" read in
    September can only mean the coming February — and beyond that the day is
    noise TMDB is likely to revise anyway, so the month and year carry it.
    """
    rd = _parse_date(release_date)
    if rd is None or not window:
        return None
    today = today or date.today()
    if rd <= today:
        return None
    month = _MONTHS_SHORT[rd.month - 1]
    if (rd - today).days >= 365:
        return f"{month} {rd.year} {window}"
    return f"{month} {rd.day} {window}"


def season_window(season: int) -> str:
    """The *window* half of a series release label for a season premiere."""
    return "Premiere" if season <= 1 else f"Season {season}"


def tv_release_facts(
    tmdb_status: str | None,
    tmdb_data: dict,
    *,
    today: date | None = None,
) -> "tuple[str | None, str | None, str | None]":
    """``(status, upcoming_date, window)`` for a series, from facts already in hand.

    TMDB's "Returning Series" means "not declared finished", not "on air": it
    covers a show airing this week and one whose last episode was two years ago
    with nothing announced.  Mapped straight to "Airing", most of TMDB's
    catalogue claimed to be on air.  The episode data TMDB ships with the
    series details (``next_episode_to_air``, ``last_episode_to_air`` and the
    season list) says which it is, at no extra API call:

    * **Airing** — an episode aired within TV_RECENT_EPISODE_DAYS, or the next
      one is due within TV_RETURNING_LOOKAHEAD_DAYS and continues the season.
      A mid-season break further out than that is still Airing, dated with the
      episode it resumes on ("Jan 8 Returns").
    * **Renewed** — the next season is known: either its first episode is
      scheduled ("Mar 4 Season 3") or TMDB lists the season undated.
    * nothing — returning in name only: no recent episode and nothing
      announced.  The slot is skipped and a lower sash gets the space, which
      is more honest than any status this could print.

    Unaired shows ("In Production" / "Planned" / "Pilot") are Production,
    dated with their premiere when TMDB has one ("Dec 25 Premiere").  Ended
    and Cancelled pass through.  A series with no episode data at all — the
    anime providers ship none — keeps the plain mapping, since there is
    nothing to refine it with.

    *window* is None whenever *upcoming_date* is; the pair feeds
    release_date_label, which also drops a date that has already passed.
    """
    today = today or date.today()
    status = (tmdb_status or "").strip()
    if status in ("Ended",):
        return "Ended", None, None
    if status in ("Cancelled", "Canceled"):
        return "Cancelled", None, None

    next_ep = tmdb_data.get("next_episode") or None
    last_ep = tmdb_data.get("last_episode") or None
    seasons = tmdb_data.get("seasons") or []

    def _future(value: str | None) -> date | None:
        d = _parse_date(value)
        return d if d is not None and d > today else None

    # TMDB editors sometimes flip a show to "Returning Series" before its first
    # episode airs (East of Eden, a week before its premiere).  Nothing has
    # aired, so it is still a show waiting to premiere, not a renewal.
    _aired = _parse_date(_episode_date(last_ep))
    unaired_returning = (
        status == "Returning Series"
        and (_aired is None or _aired > today)
        and (next_ep is not None or bool(seasons))
    )

    if status in ("In Production", "Planned", "Pilot") or unaired_returning:
        candidates = [d for d in (
            _future(_episode_date(next_ep)),
            _future(tmdb_data.get("tmdb_release_date")),
            *(_future(s.get("air_date")) for s in seasons if _season_number(s) == 1),
        ) if d is not None]
        if candidates:
            return "Production", min(candidates).isoformat(), "Premiere"
        return "Production", None, None

    if status != "Returning Series":
        return None, None, None
    if next_ep is None and last_ep is None and not seasons:
        return "Airing", None, None

    last_date = _parse_date(_episode_date(last_ep))
    next_date = _parse_date(_episode_date(next_ep))
    last_season = _episode_season(last_ep) or 0
    recent = last_date is not None and 0 <= (today - last_date).days <= TV_RECENT_EPISODE_DAYS

    if next_date is not None and next_date >= today:
        next_season = _episode_season(next_ep) or last_season
        opens_season = next_season > last_season or _episode_number(next_ep) == 1
        if opens_season:
            if recent:
                # A season ends and the next starts inside the same fortnight —
                # it has not stopped airing.
                return "Airing", None, None
            return "Renewed", next_date.isoformat(), season_window(next_season)
        if recent or (next_date - today).days <= TV_RETURNING_LOOKAHEAD_DAYS:
            return "Airing", None, None
        return "Airing", next_date.isoformat(), "Returns"

    if recent:
        return "Airing", None, None

    # Nothing scheduled.  A season TMDB lists beyond the last one aired is an
    # announced renewal — dated if TMDB has the date, "Renewed" if not.
    later = sorted(
        (n, s.get("air_date")) for s in seasons
        if (n := _season_number(s)) is not None and n > last_season
    )
    if later:
        n, air = later[0]
        air_date = _future(air)
        if air_date is not None:
            return "Renewed", air_date.isoformat(), season_window(n)
        return "Renewed", None, None
    return None, None, None


def tvdb_revival(
    status: str | None,
    tmdb_data: dict,
    tvdb_series: dict | None,
    *,
    today: date | None = None,
) -> "tuple[str, str | None, str | None] | None":
    """``(status, upcoming_date, window)`` for a series TMDB has closed but
    TVDB has carried on, else None.

    TMDB can sit at "Ended" well after a show is renewed — Cyberpunk:
    Edgerunners stayed a one-season miniseries there with its second season
    already in TVDB.  A revival needs both halves of the evidence from TVDB
    (tvdb.fetch_series_status): a season numbered past the last one TMDB
    knows, and a dated sign of life — a future episode, or one aired in the
    last TV_RECENT_EPISODE_DAYS.  Neither alone is enough: TVDB splits some
    anime into more seasons than TMDB does, and its "continuing" is left on
    some finished shows, so the status word isn't evidence on its own.
    Needs TMDB's episode data to count seasons by, so a series from an anime
    provider (one entry per season, no episode list) is never revived.

    Read the way tv_release_facts reads TMDB's: a future episode dates the
    renewal ("Oct 20 Season 2"), a recent one makes it on air.
    """
    if status not in ("Ended", "Cancelled") or not tvdb_series:
        return None
    today = today or date.today()
    last_ep = tmdb_data.get("last_episode") or None
    tmdb_last = max([_episode_season(last_ep) or 0,
                     *(n for s in tmdb_data.get("seasons") or []
                       if (n := _season_number(s)) is not None)])
    if tmdb_last <= 0:
        return None
    newer = [n for n in tvdb_series.get("seasons") or [] if n > tmdb_last]
    next_date = _parse_date(tvdb_series.get("next_aired"))
    last_date = _parse_date(tvdb_series.get("last_aired"))
    recent = last_date is not None and 0 <= (today - last_date).days <= TV_RECENT_EPISODE_DAYS
    upcoming = next_date is not None and next_date >= today
    if not (newer and (upcoming or recent)):
        return None
    if upcoming and next_date > today and not recent:
        return "Renewed", next_date.isoformat(), season_window(newer[0])
    return "Airing", None, None


def _season_number(season: dict) -> int | None:
    try:
        return int(season.get("season_number"))
    except (TypeError, ValueError):
        return None


def _episode_date(ep: dict | None) -> str | None:
    return (ep or {}).get("air_date") or None


def _episode_season(ep: dict | None) -> int | None:
    try:
        return int((ep or {}).get("season_number"))
    except (TypeError, ValueError):
        return None


def _episode_number(ep: dict | None) -> int | None:
    try:
        return int((ep or {}).get("episode_number"))
    except (TypeError, ValueError):
        return None


def _season_episode_count(seasons: list[dict], season_number: int | None) -> int | None:
    if season_number is None:
        return None
    for season in seasons or []:
        try:
            if int(season.get("season_number")) == season_number:
                count = season.get("episode_count")
                return int(count) if count is not None else None
        except (TypeError, ValueError):
            continue
    return None


# ---------------------------------------------------------------------------
# Data container
# ---------------------------------------------------------------------------

@dataclass
class DiscoveryMeta:
    """All discoverable facts about a title, computed once and cached."""

    # Awards (from MDblist keywords + Emmy ID set)
    award_wins: list[str] = field(default_factory=list)   # "Oscar Winner", "Emmy Winner", "Globe Winner"
    award_noms: list[str] = field(default_factory=list)   # "Oscar Nominee", "Emmy Nominee", "Globe Nominee"

    # Prestige signals (from TMDB credits / production_companies)
    matched_studios:   list[str] = field(default_factory=list)
    matched_directors: list[str] = field(default_factory=list)
    matched_cast:      list[str] = field(default_factory=list)

    # Festival prize, already resolved to a display label by festivals.py:
    # the top prize ("Palme d'Or") when the TMDB id is a known winner of it,
    # otherwise the honest weaker claim ("Cannes Winner").
    festival_label: str | None = None

    # Structural facts (computed from TMDB metadata)
    is_short_film:  bool = False   # movie, runtime < 40 min
    is_mini_series: bool = False   # TV, 1 season, ≤ 8 episodes
    is_binge_ready: bool = False   # TV, ≥ 3 seasons, prestige episode count

    # Language (from TMDB metadata)
    original_language: str | None = None   # ISO 639-1 code, e.g. "ko", "fr"

    # Social proof
    trending_rank: int | None = None

    # The instance's configured watchlist (watchlist.py) lists this title.
    # Never cached with the rest of the meta — membership is looked up live
    # from the in-memory snapshot on every render.
    is_watchlisted: bool = False

    # Timely release / TV lifecycle signals
    is_new_release: bool = False      # legacy combined signal
    is_premiere: bool = False         # show initial release date within the last 2 weeks
    is_just_added: bool = False       # movie digital/TV release within the last 2 weeks
    is_new_season: bool = False       # S2+E1 fresh/upcoming
    is_returning: bool = False        # S2+ non-premiere fresh/upcoming
    is_season_finale: bool = False    # conservative final-season/finale heuristic

    # Top-grossing film of its release year (box_office.py) — movies only
    is_blockbuster: bool = False

    # Keyword-based discovery signals (from MDblist keywords)
    is_cult:              bool = False   # cult-classic or cult-film
    is_true_story:        bool = False   # based-on-true-story
    is_metacritic_must_see: bool = False  # metacritic-must-see

    # Digital release (from r/movieleaks poller — movies only)
    is_digital_release: bool = False

    # Release status — populated on demand when "release_status" is in sash_priority.
    # Movies: "Physical" | "Streaming" | "Cinema" | "Production"
    # TV:     "Returning" | "Ended" | "Cancelled" | "Production"
    release_status: str | None = None
    # The next date a "Cinema" / "Production" movie moves on (YYYY-MM-DD) and
    # the status it moves to ("Cinema" / "Streaming" / "Physical"), when TMDB
    # has published one.  The sash then reads "Oct 16 Cinema" rather than the
    # bare status; release_status itself stays semantic for the greyscale
    # treatment, cache tiers and slot matching.
    upcoming_release_date: str | None = None
    upcoming_release_window: str | None = None


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def extract_discovery_meta(
    tmdb_data: dict,
    media_type: str,
    award_wins: list[str],
    award_noms: list[str],
    trending_rank: int | None,
    *,
    tmdb_id: int | str | None = None,
    release_date: str | None = None,
    keywords: list[dict] | None = None,
    festival_keyword:         str | None  = None,
    is_cult_override:         bool | None = None,
    is_true_story_override:   bool | None = None,
    is_metacritic_override:   bool | None = None,
    is_digital_release_override: bool | None = None,
    release_status_override: str | None = None,
    upcoming_release_date: str | None = None,
    upcoming_release_window: str | None = None,
    recent_digital_release_date: str | None = None,
    notable_studios:   dict[str, str] | None = None,
    notable_directors: dict[str, str] | None = None,
    notable_cast:      dict[str, str] | None = None,
    language_labels:   dict[str, str] | None = None,
    is_watchlisted:    bool = False,
    is_blockbuster:    bool = False,
) -> DiscoveryMeta:
    studios        = notable_studios   or NOTABLE_STUDIOS
    directors      = notable_directors or NOTABLE_DIRECTORS
    cast_list      = notable_cast      or NOTABLE_CAST

    meta = DiscoveryMeta(
        award_wins=award_wins,
        award_noms=award_noms,
        trending_rank=trending_rank,
        original_language=tmdb_data.get("original_language"),
        is_watchlisted=is_watchlisted,
        is_blockbuster=is_blockbuster and media_type not in ("tv", "series"),
    )

    # Build keyword name set once — reused for festival detection and the
    # new keyword-based signals (cult, true-story, metacritic).
    keyword_names: set[str] = (
        {(kw.get("name") or "").lower().strip() for kw in keywords}
        if keywords else set()
    )

    # --- Festival prize ---
    # The label is resolved here rather than read from the cache, because a
    # resolved label in the cache is a label that outlives its own correction.
    # *festival_keyword* is the cached half — which festival, not which prize —
    # and a fresh MDblist fetch supplies it from the keywords instead.  The
    # top-prize half comes from the TMDB id, so it lands even when neither does.
    meta.festival_label = resolve_festival_label(
        festival_keyword if festival_keyword is not None
        else match_festival_keyword(keyword_names),
        tmdb_id if tmdb_id is not None else tmdb_data.get("id"),
    )

    # --- Keyword-based discovery signals ---
    # Each signal uses an override (from cache) when available; otherwise
    # falls back to live keyword scanning on a fresh MDblist fetch.
    if is_cult_override is not None:
        meta.is_cult = is_cult_override
    elif keyword_names:
        meta.is_cult = bool({"cult-classic", "cult-film"} & keyword_names)

    if is_true_story_override is not None:
        meta.is_true_story = is_true_story_override
    elif keyword_names:
        meta.is_true_story = "based-on-true-story" in keyword_names

    if is_metacritic_override is not None:
        meta.is_metacritic_must_see = is_metacritic_override
    elif keyword_names:
        meta.is_metacritic_must_see = "metacritic-must-see" in keyword_names

    if is_digital_release_override is not None:
        meta.is_digital_release = is_digital_release_override

    if release_status_override is not None:
        meta.release_status = release_status_override
    meta.upcoming_release_date = upcoming_release_date
    meta.upcoming_release_window = upcoming_release_window

    if _is_recent(recent_digital_release_date):
        meta.is_just_added = True

    # --- Studios ---
    for company in tmdb_data.get("production_companies", []):
        name = company.get("name", "")
        if name in studios:
            meta.matched_studios.append(studios[name])

    # --- Credits ---
    credits = tmdb_data.get("credits", {})

    for crew_member in credits.get("crew", []):
        if crew_member.get("job") == "Director":
            name = crew_member.get("name", "")
            if name in directors:
                label = directors[name]
                if label not in meta.matched_directors:
                    meta.matched_directors.append(label)

    for cast_member in credits.get("cast", [])[:10]:
        name = cast_member.get("name", "")
        if name in cast_list:
            meta.matched_cast.append(cast_list[name])

    # --- Structural ---
    is_tv = media_type in ("tv", "series")

    if not is_tv:
        runtime = tmdb_data.get("runtime") or 0
        meta.is_short_film = 0 < runtime < 40
    else:
        num_seasons  = tmdb_data.get("number_of_seasons")  or 0
        num_episodes = tmdb_data.get("number_of_episodes") or 0

        meta.is_mini_series = (
            num_seasons == 1
            and 0 < num_episodes <= 8
        )

        if num_seasons >= 3 and num_episodes > 0:
            eps_per_season = num_episodes / num_seasons
            meta.is_binge_ready = 6 <= eps_per_season <= 20

    # --- Timely release / TV lifecycle signals ---
    tmdb_release_date = tmdb_data.get("tmdb_release_date") or release_date
    if is_tv and _is_recent(tmdb_release_date):
        meta.is_premiere = True

    if is_tv:
        next_ep = tmdb_data.get("next_episode") or None
        last_ep = tmdb_data.get("last_episode") or None
        seasons = tmdb_data.get("seasons") or []

        last_is_active = _is_recent_or_upcoming(_episode_date(last_ep))
        next_is_active = _is_recent_or_upcoming(_episode_date(next_ep))

        last_season = _episode_season(last_ep)
        next_season = _episode_season(next_ep)

        active_season = None
        active_ep = None
        if next_is_active and next_season and next_season > 1:
            active_season = next_season
            active_ep = next_ep
        elif last_is_active and last_season and last_season > 1:
            active_season = last_season
            active_ep = last_ep

        if active_season:
            # Prefer the season's own air_date when TMDB supplies it — this flags
            # a premiere correctly even for multi-episode drops, where the "next"
            # episode is no longer episode 1. When no season air_date is
            # available, fall back to the episode-number heuristic: episode 1 of a
            # season > 1 is a premiere; a later episode means it's returning.
            is_new_season = None
            for s in seasons:
                try:
                    s_num = int(s.get("season_number", 0))
                except (TypeError, ValueError):
                    continue
                if s_num == active_season:
                    air_date = s.get("air_date")
                    if air_date:
                        is_new_season = _is_recent_or_upcoming(air_date)
                    break

            if is_new_season is None:
                is_new_season = _episode_number(active_ep) == 1

            if is_new_season:
                meta.is_new_season = True
            else:
                meta.is_returning = True

        last_season = _episode_season(last_ep)
        last_number = _episode_number(last_ep)
        latest_season_count = _season_episode_count(seasons, last_season)
        status = tmdb_data.get("tmdb_status") or ""
        if (
            last_season
            and last_number
            and latest_season_count
            and last_number >= latest_season_count
            and _is_recent(_episode_date(last_ep), max_days=TV_RECENT_EPISODE_DAYS)
            and status in {"Ended", "Cancelled", "Canceled"}
        ):
            meta.is_season_finale = True

    if meta.is_premiere or meta.is_just_added or meta.is_digital_release or _is_recent(release_date):
        meta.is_new_release = True

    return meta


# ---------------------------------------------------------------------------
# Priority picker
# ---------------------------------------------------------------------------

def pick_sash(
    meta: DiscoveryMeta,
    priority: list[str],
) -> tuple[str, str] | None:
    """
    Walk *priority* (ordered list of slot names) and return the first match
    as ``(label_text, sash_type)``, or ``None`` if nothing matches.
    """
    for slot in priority:
        result = _evaluate_slot(slot, meta)
        if result is not None:
            sash_type = _SASH_TYPES.get(slot, "info")
            return result, sash_type
    return None


TRENDING_SLOTS = ("trending", "trending_broad")


def shown_trending_rank(meta: DiscoveryMeta, priority: list[str]) -> int | None:
    """The rank a trending_style other than "sash" draws: the title's rank when
    a trending slot in *priority* covers it, wherever in the list that slot is.
    The rank mark is separate from the sash, so nothing above it can hide it."""
    rank = meta.trending_rank
    if not rank:
        return None
    if "trending" in priority and rank <= _cfg.TRENDING_FETCH_COUNT:
        return rank
    if "trending_broad" in priority and _cfg.TRENDING_FETCH_COUNT < rank <= _cfg.TRENDING_BROAD_FETCH_COUNT:
        return rank
    return None


def _evaluate_slot(slot: str, meta: DiscoveryMeta) -> str | None:
    """Return a label string if this slot has a match, else None."""

    if slot == "watchlist":
        return "Watchlist" if meta.is_watchlisted else None

    if slot == "wins":
        # Oscar Best Picture wins and Emmy Outstanding wins only.
        # Golden Globe wins have their own slot (gg_wins) so they can be
        # prioritised independently. A title can win both Oscar and Emmy
        # (impossible in practice) but both share this slot since one is film,
        # one is TV — they never coexist on the same title.
        w = [v for v in meta.award_wins if v != "Globe Winner"]
        return w[0] if w else None

    if slot == "gg_wins":
        # Golden Globe wins — all top film and TV categories.
        return "Globe Winner" if "Globe Winner" in meta.award_wins else None

    if slot in ("pic_noms", "emmy_noms"):
        # Best Picture nominations (film) and Major Emmy nominations (TV) share
        # this slot — they never coexist on the same title, mirroring wins.
        # emmy_noms is kept as a legacy alias for backward-compat with old URLs.
        match = next((n for n in meta.award_noms if "Oscar Nominee" in n or "Emmy" in n), None)
        return match

    if slot == "gg_noms":
        # Golden Globe nominations — all top film and TV categories.
        return "Globe Nominee" if "Globe Nominee" in meta.award_noms else None

    if slot == "noms":
        # Legacy catch-all: any nomination (kept for backward-compat with
        # hand-crafted sash_priority query params)
        return " • ".join(meta.award_noms) if meta.award_noms else None

    if slot == "festival":
        return meta.festival_label if meta.festival_label else None

    if slot == "foreign":
        lang = meta.original_language
        if not lang or lang == "en":
            return None
        return LANGUAGE_LABELS.get(lang, "Foreign") # return LANGUAGE_LABELS.get(lang, "Foreign Language Film")

    if slot == "studio":
        # matched_studios already holds display labels (dict values)
        return f"{meta.matched_studios[0]}" if meta.matched_studios else None

    if slot == "director":
        # matched_directors already holds display labels (dict values)
        return f"{meta.matched_directors[0]}" if meta.matched_directors else None

    if slot == "cast":
        # matched_cast already holds display labels (dict values)
        return meta.matched_cast[0] if meta.matched_cast else None

    if slot == "trending":
        return f"#{meta.trending_rank} Today" if meta.trending_rank and meta.trending_rank <= _cfg.TRENDING_FETCH_COUNT else None

    if slot == "trending_broad":
        return f"#{meta.trending_rank} Today" if meta.trending_rank and _cfg.TRENDING_FETCH_COUNT < meta.trending_rank <= _cfg.TRENDING_BROAD_FETCH_COUNT else None

    if slot == "new_season":
        return "New Season" if meta.is_new_season else None

    if slot == "returning":
        return "Returning" if meta.is_returning else None

    if slot == "premiere":
        return "Premiere" if meta.is_premiere else None

    if slot == "just_added":
        return "Just Added" if meta.is_just_added else None

    if slot == "season_finale":
        return "Season Finale" if meta.is_season_finale else None

    if slot in ("new_release", "digital_release"):
        # Merged: fires on release-date recency OR r/movieleaks confirmation.
        # "digital_release" is kept as a legacy alias so old sash_priority params
        # still work — both slots check the same combined condition.
        if meta.is_new_release or meta.is_digital_release:
            return "New"
        return None

    if slot == "metacritic":
        return "Must-See" if meta.is_metacritic_must_see else None

    if slot == "blockbuster":
        return "Blockbuster" if meta.is_blockbuster else None

    if slot == "cult":
        return "Cult Classic" if meta.is_cult else None

    if slot == "true_story":
        return "True Story" if meta.is_true_story else None

    if slot == "structural":
        for key in _STRUCTURAL_CHECKS:
            if key == "short_film"  and meta.is_short_film:  return _STRUCTURAL_LABELS[key]
            if key == "mini_series" and meta.is_mini_series:  return _STRUCTURAL_LABELS[key]
            if key == "binge_ready" and meta.is_binge_ready:  return _STRUCTURAL_LABELS[key]
        return None

    if slot in ("short_film", "mini_series", "binge_ready"):
        if slot == "short_film" and meta.is_short_film: return _STRUCTURAL_LABELS[slot]
        if slot == "mini_series" and meta.is_mini_series: return _STRUCTURAL_LABELS[slot]
        if slot == "binge_ready" and meta.is_binge_ready: return _STRUCTURAL_LABELS[slot]
        return None

    if slot == "release_status":
        return _release_status_label(meta)

    if slot in RELEASE_STATUS_SLOTS:
        if meta.release_status and meta.release_status.lower() == slot:
            return _release_status_label(meta)
        return None

    return None


def _release_status_label(meta: DiscoveryMeta) -> str | None:
    """The status as a display string — or, when the title's next move has a
    published date, that date and what it opens: "Oct 16 Cinema" for a movie,
    "Dec 25 Premiere" / "Mar 4 Season 3" / "Jan 8 Returns" for a series.
    Checked here as well as upstream: a date beside a status that has already
    arrived (Streaming, Ended, ...) would read as a promise it isn't."""
    if meta.release_status in _DATED_STATUSES:
        label = release_date_label(meta.upcoming_release_date, meta.upcoming_release_window)
        if label:
            return label
    return meta.release_status


# The statuses that are waiting on a next date — the only ones that can wear one.
_DATED_STATUSES = frozenset({"Cinema", "Production", "Renewed", "Airing"})


# ---------------------------------------------------------------------------
# Default priority
# ---------------------------------------------------------------------------

ALL_PRIORITY_SLOTS: list[str] = [
    "watchlist",
    "wins",
    "gg_wins",
    "festival",
    "pic_noms",
    "gg_noms",
    "studio",
    "director",
    "cast",
    "trending",
    "new_season",
    "returning",
    "premiere",
    "just_added",
    "season_finale",
    "blockbuster",
    "cult",
    "foreign",
    "new_release",
    "metacritic",
    "true_story",
    "structural",
    "trending_broad",
    "emmy_noms",        # legacy alias for pic_noms — still accepted in sash_priority param
    "digital_release",  # legacy alias for new_release
    "noms",             # legacy alias for any nomination
    "release_status",   # opt-in: Blu-ray / Streaming / Cinema / Production — requires extra API call for movies
    "short_film",
    "mini_series",
    "binge_ready",
    "cinema",
    "streaming",
    "physical",
    "production",
    "ended",
    "cancelled",
    "airing",
    "renewed",
]


# ---------------------------------------------------------------------------
# Operator override loader
# ---------------------------------------------------------------------------

_OVERRIDE_PATH = _cfg.DISCOVERY_OVERRIDES_PATH

# The file's sections and the module lists they feed.
SECTIONS = ("studios", "directors", "cast")
BUILTIN_LISTS: dict[str, dict[str, str]] = {
    "studios":   dict(NOTABLE_STUDIOS),
    "directors": dict(NOTABLE_DIRECTORS),
    "cast":      dict(NOTABLE_CAST),
}
# The sash is narrow; the dashboard refuses labels longer than this and
# names no TMDB credit comes near.
MAX_LABEL_LENGTH = 40
MAX_NAME_LENGTH = 200
MAX_SECTION_ENTRIES = 2000

# (mtime_ns, size) of the file the lists were last built from, None when absent.
_loaded_stamp: tuple[int, int] | None = None
_checked_at = 0.0
_CHECK_INTERVAL = 3.0


def _file_stamp() -> tuple[int, int] | None:
    try:
        st = os.stat(_OVERRIDE_PATH)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _read_override_file() -> dict | None:
    """The file's JSON object, or None when absent or unusable (logged)."""
    try:
        with open(_OVERRIDE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None                   # normal — no overrides configured
    except Exception as exc:
        logger.warning(
            f"discovery_overrides.json: failed to parse ({exc}) — using built-in lists"
        )
        return None
    if not isinstance(data, dict):
        logger.warning("discovery_overrides.json: root must be a JSON object — ignoring")
        return None
    return data


def _clean_section(raw) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    return {str(k): str(v) for k, v in raw.items() if str(k).strip() and str(v).strip()}


def effective_lists(data: dict | None) -> dict[str, dict[str, str]]:
    """The three lists *data* (the file's contents) produces on top of the
    built-in ones.  "replace" (the default mode): a section present replaces
    its built-in list.  "merge": its entries are added to the built-in list."""
    lists = {name: dict(BUILTIN_LISTS[name]) for name in SECTIONS}
    if not data:
        return lists
    merge = data.get("mode", "replace") == "merge"
    for name in SECTIONS:
        section = _clean_section(data.get(name))
        if section is None:
            continue
        if merge:
            lists[name].update(section)
        else:
            lists[name] = section
    return lists


def _load_discovery_overrides() -> None:
    """
    (Re)build the director / studio / cast lists from the built-in ones and
    the operator's JSON file.  The lists are swapped in whole, never edited in
    place, so a render reading them mid-reload sees one version or the other.

    See the module docstring at the top of this file for the full format.
    """
    global NOTABLE_STUDIOS, NOTABLE_DIRECTORS, NOTABLE_CAST, _loaded_stamp
    _loaded_stamp = _file_stamp()
    data = _read_override_file()
    lists = effective_lists(data)
    NOTABLE_STUDIOS, NOTABLE_DIRECTORS, NOTABLE_CAST = (
        lists["studios"], lists["directors"], lists["cast"]
    )
    if data is not None:
        mode = "merge" if data.get("mode", "replace") == "merge" else "replace"
        counts = [
            f"{len(lists[name])} {name}" for name in SECTIONS
            if isinstance(data.get(name), dict)
        ]
        logger.info(f"discovery_overrides.json loaded ({mode}): {', '.join(counts) or 'no sections'}")


def refresh_overrides() -> bool:
    """Reload the lists if the file changed since they were built — the
    dashboard's editor writes it from whichever worker served the save.  A
    clock read on most calls, one stat every few seconds.  True when the
    lists were rebuilt."""
    global _checked_at
    now = time.monotonic()
    if now - _checked_at < _CHECK_INTERVAL:
        return False
    _checked_at = now
    if _file_stamp() == _loaded_stamp:
        return False
    _load_discovery_overrides()
    return True


def current_lists() -> dict:
    """What the dashboard's editor shows: each list as it applies now, and
    whether it is the built-in one or the operator's."""
    data = _read_override_file()
    lists = effective_lists(data)
    merge = bool(data) and data.get("mode", "replace") == "merge"
    out = {}
    for name in SECTIONS:
        custom = bool(data) and isinstance(data.get(name), dict)
        out[name] = {
            "entries": [{"name": k, "label": v} for k, v in lists[name].items()],
            "custom":  custom,
            "merged":  custom and merge,
            "builtin": [{"name": k, "label": v} for k, v in BUILTIN_LISTS[name].items()],
        }
    return out


def validate_entries(entries) -> dict[str, str]:
    """An editor-sent list ([{"name", "label"}, ...]) as a section dict.
    Raises ValueError on anything malformed."""
    if not isinstance(entries, list):
        raise ValueError("entries must be a list")
    if len(entries) > MAX_SECTION_ENTRIES:
        raise ValueError(f"more than {MAX_SECTION_ENTRIES} entries")
    out: dict[str, str] = {}
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("each entry must be an object")
        name = str(item.get("name") or "").strip()
        label = str(item.get("label") or "").strip() or name
        if not name:
            raise ValueError("an entry has no name")
        if len(name) > MAX_NAME_LENGTH:
            raise ValueError(f"name longer than {MAX_NAME_LENGTH} characters: {name[:40]}…")
        if len(label) > MAX_LABEL_LENGTH:
            raise ValueError(f"label for {name} is longer than {MAX_LABEL_LENGTH} characters")
        out[name] = label
    return out


def _sort_key(name: str) -> tuple[str, str]:
    """A–Z ignoring case and accents, so "Cuarón" sorts with "Cuaron"."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c))
    return (plain.casefold(), name)


def sorted_section(section: dict[str, str]) -> dict[str, str]:
    return {k: section[k] for k in sorted(section, key=_sort_key)}


def override_path() -> str:
    return _OVERRIDE_PATH


def override_writable() -> bool:
    if os.path.exists(_OVERRIDE_PATH):
        return os.access(_OVERRIDE_PATH, os.W_OK)
    return os.access(os.path.dirname(_OVERRIDE_PATH) or ".", os.W_OK)


def save_sections(changes: dict[str, dict[str, str] | None]) -> None:
    """Write the editor's lists to the file: a dict becomes that section's
    full list, None drops the section so the built-in list applies again.

    The file is rewritten in "replace" mode.  A hand-written "merge" file has
    its other sections expanded to the lists they produced, so nothing the
    operator sees changes except what they edited.  Written atomically, then
    reloaded here; other workers pick it up in refresh_overrides().
    """
    data = _read_override_file() or {}
    was_merge = data.get("mode", "replace") == "merge"
    applied = effective_lists(data)
    out: dict = {"mode": "replace"}
    for name in SECTIONS:
        if name in changes:
            if changes[name] is not None:
                out[name] = sorted_section(changes[name])
        elif isinstance(data.get(name), dict):
            out[name] = applied[name] if was_merge else _clean_section(data[name])
    directory = os.path.dirname(_OVERRIDE_PATH) or "."
    tmp = os.path.join(directory, f".{os.path.basename(_OVERRIDE_PATH)}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, _OVERRIDE_PATH)
    _load_discovery_overrides()


_load_discovery_overrides()
