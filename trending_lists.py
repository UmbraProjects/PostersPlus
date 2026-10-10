"""Which trending lists a request ranks against (trending_list=).

The parameter names, for each of the four lists (movie, tv, anime and
anime_movie), the sources picked for it, and whether titles not out at home
yet are left off: "m:imdb.tmdb_week,h:1".  Each source picked is a catalog
of its own in the trending addon; the first is the list posters rank on
("m:none" picks no catalog, and ranks on the operator's).  A part left out
is the operator's choice, so a URL carries only where it differs, and a URL
that differs in nothing is the operator's lists, the ones every other poster
ranks on.

Only what the menu offers can be asked for: TMDB's day and week lists,
AniList for anime, the public lists below (TRENDING_LIST_SOURCES), the
operator's TRENDING_SOURCE_* and the extra TRENDING_SOURCE_CHOICES.  An
unknown part falls back to the operator's choice.  So the number of lists an
instance builds is bounded by its menu, not by its visitors, and visitors
who choose alike share one list.

Each list is stored under its own key (see TrendingLists.key): the endpoint
alone for the operator's list, so those rows are the ones stored before
visitors could choose, and "<endpoint>@<parts>" otherwise, holding only the
parts that list is built from.
"""
from __future__ import annotations

import dataclasses
import functools
import re
from dataclasses import dataclass

import config

ENDPOINTS = ("movie", "tv", "anime", "anime_movie")
ANIME_ENDPOINTS = ("anime", "anime_movie")
PARAM = "trending_list"

_LETTER = {"movie": "m", "tv": "t", "anime": "a", "anime_movie": "f"}
_BY_LETTER = {v: k for k, v in _LETTER.items()}

# The operator's TRENDING_SOURCE_<type>, for the list it is set for.
SERVER = "server"
TMDB = "tmdb"
TMDB_WEEK = "tmdb_week"
ANILIST = "anilist"

# Public lists that need no key.  IMDb answers scripts with a bot challenge
# and Trakt wants an API key, so their charts come from the MDBList user
# snoak's mirrors, refreshed daily (as are the JustWatch, Rotten Tomatoes,
# Television Stats and streaming Top 10 ones).  SIMKL publishes its trending
# lists on its own CDN, refreshed hourly.
_SNOAK = "https://mdblist.com/lists/snoak/"
_SIMKL = "https://data.simkl.in/discover/trending/{}/{}_500.json"


def _top10(kind: str) -> tuple[tuple[str, str, str, str], ...]:
    services = (("netflix", "Netflix", "netflix"), ("prime", "Prime Video", "amazon-prime"),
                ("apple_tv", "Apple TV+", "apple-tv"), ("disney", "Disney+", "disney"),
                ("paramount", "Paramount+", "paramount"), ("hbo_max", "HBO Max", "hbo-max"))
    return tuple((sid, f"{name} Top 10 (US)", "top10", f"{_SNOAK}top-10-{slug}-{kind}-in-the-us")
                 for sid, name, slug in services)


# (id, name, family, url) of each list's built-in sources; "" is TMDB's or
# AniList's own API.  The family is what TRENDING_LIST_SOURCES offers.
_BUILTIN: dict[str, tuple[tuple[str, str, str, str], ...]] = {
    "movie": (
        (TMDB, "TMDB Today", "tmdb", ""),
        (TMDB_WEEK, "TMDB This Week", "tmdb", ""),
        ("imdb", "IMDb Most Popular", "imdb", _SNOAK + "top-10-movies-of-the-day"),
        ("trakt", "Trakt Trending", "trakt", _SNOAK + "trending-movies"),
        ("trakt_digital", "Trakt Trending (Digital)", "trakt", _SNOAK + "trakts-trending-movies-digital"),
        ("simkl", "SIMKL Today", "simkl", _SIMKL.format("movies", "today")),
        ("simkl_week", "SIMKL This Week", "simkl", _SIMKL.format("movies", "week")),
        ("justwatch", "JustWatch Popular", "justwatch", _SNOAK + "todays-most-popular-movies-on-justwatch"),
        ("rotten_tomatoes", "Rotten Tomatoes Popular", "rottentomatoes",
         _SNOAK + "most-popular-movies-on-rotten-tomatoes"),
        ("tv_stats", "Television Stats Today", "tvstats",
         _SNOAK + "todays-most-popular-movies-on-television-stats"),
        *_top10("movies"),
    ),
    "tv": (
        (TMDB, "TMDB Today", "tmdb", ""),
        (TMDB_WEEK, "TMDB This Week", "tmdb", ""),
        ("imdb", "IMDb Most Popular", "imdb", _SNOAK + "top-10-shows-of-the-day"),
        ("trakt", "Trakt Trending", "trakt", _SNOAK + "trakt-s-trending-shows"),
        ("simkl", "SIMKL Today", "simkl", _SIMKL.format("tv", "today")),
        ("simkl_week", "SIMKL This Week", "simkl", _SIMKL.format("tv", "week")),
        ("justwatch", "JustWatch Popular", "justwatch", _SNOAK + "todays-most-popular-shows-on-justwatch"),
        ("rotten_tomatoes", "Rotten Tomatoes Popular", "rottentomatoes",
         _SNOAK + "most-popular-shows-on-rotten-tomatoes"),
        ("tv_stats", "Television Stats Today", "tvstats",
         _SNOAK + "todays-most-popular-shows-on-television-stats"),
        *_top10("shows"),
    ),
    "anime": (
        (ANILIST, "AniList", "anilist", ""),
        ("simkl", "SIMKL Today", "simkl", _SIMKL.format("anime", "today")),
        ("simkl_week", "SIMKL This Week", "simkl", _SIMKL.format("anime", "week")),
        ("trakt", "Trakt Trending", "trakt", _SNOAK + "trending-anime-shows"),
    ),
    "anime_movie": (
        (ANILIST, "AniList", "anilist", ""),
        ("trakt", "Trakt Trending", "trakt", _SNOAK + "trending-anime-movies"),
    ),
}
# The families TRENDING_LIST_SOURCES may turn off.  TMDB's and AniList's
# own lists are always offered: they are what the operator's lists are.
FAMILIES = {
    "imdb": "IMDb", "trakt": "Trakt", "simkl": "SIMKL", "justwatch": "JustWatch",
    "rottentomatoes": "Rotten Tomatoes", "tvstats": "Television Stats",
    "top10": "Streaming Top 10s (US)",
}
# Catalogs one list type may be picked for.
MAX_PICKS = 8
_SERVER_LABEL = "This Instance's List"

_TYPE_ALIASES = {"movie": "movie", "movies": "movie", "tv": "tv", "series": "tv", "show": "tv",
                 "shows": "tv", "anime": "anime", "anime_movie": "anime_movie",
                 "anime_movies": "anime_movie", "anime_film": "anime_movie",
                 "anime_films": "anime_movie"}
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def endpoint_of(media_type: str) -> str:
    """The list a media type ranks on: "tv" for series, the anime lists as
    they are, "movie" otherwise."""
    if media_type in ANIME_ENDPOINTS:
        return media_type
    return "tv" if media_type in ("tv", "series") else "movie"


def anime_split() -> bool:
    """Whether anime ranks on its own lists (see tmdb.anime_split)."""
    return bool(config.TRENDING_CATALOGS_ENABLED)


def operator_url(endpoint: str) -> str:
    """The operator's TRENDING_SOURCE_<type> for *endpoint*, "" when unset."""
    return {
        "movie": config.TRENDING_SOURCE_MOVIE,
        "tv": config.TRENDING_SOURCE_TV,
        "anime": config.TRENDING_SOURCE_ANIME,
        "anime_movie": config.TRENDING_SOURCE_ANIME_MOVIE,
    }[endpoint]


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.casefold()).strip("-")[:32].strip("-")


@functools.lru_cache(maxsize=1)
def _extras_for(raw: str) -> dict[str, tuple[tuple[str, str, str], ...]]:
    """TRENDING_SOURCE_CHOICES's "type|Name|URL" entries as (id, name, url)
    per list.  An id is the name's slug, so a list keeps its id when its URL
    is changed; one that would clash with another on its list is numbered."""
    out: dict[str, list[tuple[str, str, str]]] = {e: [] for e in ENDPOINTS}
    for entry in raw.split(","):
        bits = [b.strip() for b in entry.split("|", 2)]
        if len(bits) != 3 or not all(bits):
            continue
        endpoint = _TYPE_ALIASES.get(bits[0].casefold())
        if endpoint is None or not bits[2].lower().startswith(("http://", "https://")):
            continue
        base = _slug(bits[1]) or "list"
        taken = {b[0] for b in _BUILTIN[endpoint]} | {SERVER, "none"} | {i for i, _n, _u in out[endpoint]}
        source_id, n = base, 2
        while source_id in taken:
            source_id = f"{base[:28]}-{n}"
            n += 1
        out[endpoint].append((source_id, bits[1], bits[2]))
    return {e: tuple(v) for e, v in out.items()}


def _extras(endpoint: str) -> tuple[tuple[str, str, str], ...]:
    return _extras_for(config.TRENDING_SOURCE_CHOICES or "")[endpoint]


def default_source(endpoint: str) -> str:
    return SERVER if operator_url(endpoint) else _BUILTIN[endpoint][0][0]


def _builtin(endpoint: str) -> list[tuple[str, str, str, str]]:
    allowed = {f.strip().casefold() for f in str(config.TRENDING_LIST_SOURCES or "").split(",")}
    return [b for b in _BUILTIN[endpoint] if b[2] in ("tmdb", "anilist") or b[2] in allowed]


def menu(endpoint: str) -> list[tuple[str, str]]:
    """(id, name) of every source *endpoint*'s list may be read from, the
    operator's choice first."""
    out = [(SERVER, _SERVER_LABEL)] if operator_url(endpoint) else []
    out += [(b[0], b[1]) for b in _builtin(endpoint)]
    out += [(source_id, name) for source_id, name, _url in _extras(endpoint)]
    return out


def pickable(endpoint: str) -> "set[str]":
    """The source ids a visitor may ask *endpoint*'s list from: the menu, or
    only the operator's own while visitors may not choose."""
    if not config.TRENDING_LIST_CHOICE:
        return {default_source(endpoint)}
    return {i for i, _n in menu(endpoint)}


def source_name(endpoint: str, source: str) -> str:
    return dict(menu(endpoint)).get(source, source)


def source_url(endpoint: str, source: str) -> str:
    """The URL *source* is read from, or "" for TMDB's and AniList's own."""
    if source == SERVER:
        return operator_url(endpoint)
    for b in _BUILTIN[endpoint]:
        if b[0] == source:
            return b[3]
    for source_id, _name, url in _extras(endpoint):
        if source_id == source:
            return url
    return ""


@dataclass(frozen=True)
class TrendingLists:
    """One choice of the four lists' sources, and of leaving off what isn't
    out at home (*home*).  The fields are the lists posters rank on; *shown*
    is, per list (ENDPOINTS' order), the sources picked as catalogs, None
    while that is each list's ranking source alone."""
    movie: str
    tv: str
    anime: str
    anime_movie: str
    home: bool
    shown: "tuple[tuple[str, ...], ...] | None" = None

    def source(self, endpoint: str) -> str:
        return getattr(self, endpoint_of(endpoint))

    def url(self, endpoint: str) -> str:
        endpoint = endpoint_of(endpoint)
        return source_url(endpoint, self.source(endpoint))

    def week(self, endpoint: str) -> bool:
        return self.source(endpoint) == TMDB_WEEK

    def catalogs(self, endpoint: str) -> "tuple[str, ...]":
        """The sources picked as *endpoint*'s catalogs, its ranking one first."""
        endpoint = endpoint_of(endpoint)
        if self.shown is None:
            return (self.source(endpoint),)
        return self.shown[ENDPOINTS.index(endpoint)]

    def ranking(self) -> "TrendingLists":
        """Just the lists posters rank on, without the other catalogs."""
        return dataclasses.replace(self, shown=None)

    def for_catalog(self, endpoint: str, source: str) -> "TrendingLists":
        """The lists *endpoint*'s catalog from *source* is cut from, and its
        posters rank on."""
        return dataclasses.replace(self, shown=None, **{endpoint_of(endpoint): source})

    def _parts(self, endpoint: str) -> tuple[str, ...]:
        # What this one list is built from: its own source and the home
        # filter, and while anime ranks on its own lists, which anime lists
        # the movie and TV ones leave their titles to.
        parts = (self.source(endpoint), "h1" if self.home else "h0")
        if endpoint not in ANIME_ENDPOINTS and anime_split():
            parts += (self.anime, self.anime_movie)
        return parts

    def key(self, endpoint: str) -> str:
        """Where *endpoint*'s list is stored: the endpoint alone when it is
        the operator's list."""
        endpoint = endpoint_of(endpoint)
        parts = self._parts(endpoint)
        if parts == default()._parts(endpoint):
            return endpoint
        return f"{endpoint}@{'.'.join(parts)}"

    def token(self) -> str:
        """The trending_list= value for this choice: only what differs from
        the operator's, "" when nothing does."""
        base = default()
        out = []
        for e in ENDPOINTS:
            picks = self.catalogs(e)
            if picks != (base.source(e),):
                out.append(f"{_LETTER[e]}:{'.'.join(picks) or 'none'}")
        if self.home != base.home:
            out.append(f"h:{int(self.home)}")
        return ",".join(out)

    def rank_token(self) -> str:
        """token() of the lists posters rank on: what a render depends on."""
        return self.ranking().token()

    @property
    def is_default(self) -> bool:
        return self == default()


def default() -> TrendingLists:
    """The operator's lists."""
    return TrendingLists(*(default_source(e) for e in ENDPOINTS),
                         home=bool(config.TRENDING_HIDE_UNRELEASED))


def parse(raw: "str | None") -> TrendingLists:
    """The lists a trending_list= value asks for; anything the menu doesn't
    offer is the operator's choice, as is everything while visitors may not
    choose (TRENDING_LIST_CHOICE)."""
    base = default()
    if not raw or not config.TRENDING_LIST_CHOICE:
        return base
    return _parse(raw.strip().lower()[:200], base)


@functools.lru_cache(maxsize=256)
def _parse(raw: str, base: TrendingLists) -> TrendingLists:
    shown = {e: (base.source(e),) for e in ENDPOINTS}
    home = base.home
    for part in raw.split(","):
        letter, _, value = part.strip().partition(":")
        value = value.strip()
        if letter == "h":
            if value in ("0", "1"):
                home = value == "1"
            continue
        endpoint = _BY_LETTER.get(letter)
        if not endpoint:
            continue
        if value == "none":
            shown[endpoint] = ()
            continue
        offered = pickable(endpoint)
        picks: list[str] = []
        for source in value.split("."):
            source = source.strip()
            if _ID_RE.match(source) and source in offered and source not in picks:
                picks.append(source)
        if picks:
            shown[endpoint] = tuple(picks[:MAX_PICKS])
    # No catalog picked ranks on the operator's list.
    ranking = {e: (shown[e][0] if shown[e] else base.source(e)) for e in ENDPOINTS}
    plain = all(shown[e] == (ranking[e],) for e in ENDPOINTS)
    return TrendingLists(*(ranking[e] for e in ENDPOINTS), home=home,
                         shown=None if plain else tuple(shown[e] for e in ENDPOINTS))


def from_key(key: str) -> "tuple[str, TrendingLists]":
    """(endpoint, lists) a stored list's key names; the parts a key leaves
    out are the operator's."""
    endpoint, _, rest = key.partition("@")
    base = default()
    if not rest:
        return endpoint, base
    parts = rest.split(".")
    picked = {e: base.source(e) for e in ENDPOINTS}
    home = base.home
    if parts:
        picked[endpoint_of(endpoint)] = parts[0]
    if len(parts) > 1:
        home = parts[1] == "h1"
    if len(parts) > 3:
        picked["anime"], picked["anime_movie"] = parts[2], parts[3]
    return endpoint, TrendingLists(*(picked[e] for e in ENDPOINTS), home=home)


def caps() -> dict:
    """The menu as /server-caps hands it to the configurator."""
    base = default()
    return {
        "enabled": bool(config.TRENDING_LIST_CHOICE),
        "home_default": base.home,
        "max_picks": MAX_PICKS,
        "lists": {
            e: {"default": base.source(e),
                "choices": [{"id": i, "name": n} for i, n in menu(e)]}
            for e in ENDPOINTS
        },
    }
