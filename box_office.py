"""
box_office.py — the Blockbuster sash: a film among the top-grossing of its year.

"Blockbuster" has no official definition, so this one is ours: a film is a
blockbuster when TMDB's worldwide revenue puts it in the top
``BLOCKBUSTER_TOP_N`` (default 10) of its primary release year, and it grossed
at least ``BLOCKBUSTER_MIN_REVENUE`` (default $100M) in today's dollars.

Ranking within the year is what makes it work across eras: a flat figure
either shuts out Jaws and Star Wars or waves in a mid-table 2020s sequel.  The
floor is there for the years TMDB has little revenue data for, where "top 10"
would otherwise reach down to films that barely recorded a gross.  It is
deflated to each year's dollars with the US CPI (below): a nominal $100M let
only three 1975 films through, Jaws among them but not much else.

One year costs one /discover call (sorted by revenue) plus a details call for
each of its top N, since /discover doesn't return revenue itself.  A year's
list is stored in app_state and reused by every film from that year; past
years barely move, so they are kept for months, while the current year's
grosses are still climbing and are refreshed daily.
"""

import asyncio
import json
import logging
import time
from datetime import date

import httpx

import config as _cfg
from cache import get_app_state, set_app_state

logger = logging.getLogger(__name__)

_DISCOVER_URL = "https://api.themoviedb.org/3/discover/movie"
_DETAILS_URL  = "https://api.themoviedb.org/3/movie/{}"

# How long a year's list is trusted.  The year still in cinemas changes daily;
# last year's late grosses (and TMDB's edits to them) settle within weeks.
_TTL_CURRENT  = 86400
_TTL_LAST     = 7 * 86400
_TTL_SETTLED  = 90 * 86400

# After a failed build, don't ask again for this long (per worker), so a TMDB
# outage doesn't turn every movie render into another /discover attempt.
_FAILURE_BACKOFF = 600

# US CPI-U annual averages (BLS series CUUR0000SA0, 1982-84 = 100), for
# deflating the floor.  TMDB revenue is in nominal US dollars, so the US index
# is the right one even for a film earning mostly abroad.  Years past the
# table use its last entry (the floor is then simply today's), years before it
# its first.  2025 is the BLS annual figure; add a year here each January.
_CPI: dict[int, float] = {
    1913: 9.9, 1914: 10.0, 1915: 10.1, 1916: 10.9, 1917: 12.8, 1918: 15.1,
    1919: 17.3, 1920: 20.0, 1921: 17.9, 1922: 16.8, 1923: 17.1, 1924: 17.1,
    1925: 17.5, 1926: 17.7, 1927: 17.4, 1928: 17.1, 1929: 17.1, 1930: 16.7,
    1931: 15.2, 1932: 13.7, 1933: 13.0, 1934: 13.4, 1935: 13.7, 1936: 13.9,
    1937: 14.4, 1938: 14.1, 1939: 13.9, 1940: 14.0, 1941: 14.7, 1942: 16.3,
    1943: 17.3, 1944: 17.6, 1945: 18.0, 1946: 19.5, 1947: 22.3, 1948: 24.1,
    1949: 23.8, 1950: 24.1, 1951: 26.0, 1952: 26.5, 1953: 26.7, 1954: 26.9,
    1955: 26.8, 1956: 27.2, 1957: 28.1, 1958: 28.9, 1959: 29.1, 1960: 29.6,
    1961: 29.9, 1962: 30.2, 1963: 30.6, 1964: 31.0, 1965: 31.5, 1966: 32.4,
    1967: 33.4, 1968: 34.8, 1969: 36.7, 1970: 38.8, 1971: 40.5, 1972: 41.8,
    1973: 44.4, 1974: 49.3, 1975: 53.8, 1976: 56.9, 1977: 60.6, 1978: 65.2,
    1979: 72.6, 1980: 82.4, 1981: 90.9, 1982: 96.5, 1983: 99.6, 1984: 103.9,
    1985: 107.6, 1986: 109.6, 1987: 113.6, 1988: 118.3, 1989: 124.0,
    1990: 130.7, 1991: 136.2, 1992: 140.3, 1993: 144.5, 1994: 148.2,
    1995: 152.4, 1996: 156.9, 1997: 160.5, 1998: 163.0, 1999: 166.6,
    2000: 172.2, 2001: 177.1, 2002: 179.9, 2003: 184.0, 2004: 188.9,
    2005: 195.3, 2006: 201.6, 2007: 207.3, 2008: 215.3, 2009: 214.5,
    2010: 218.1, 2011: 224.9, 2012: 229.6, 2013: 233.0, 2014: 236.7,
    2015: 237.0, 2016: 240.0, 2017: 245.1, 2018: 251.1, 2019: 255.7,
    2020: 258.8, 2021: 271.0, 2022: 292.7, 2023: 304.7, 2024: 313.7,
    2025: 322.2,
}
_CPI_FIRST, _CPI_LAST = min(_CPI), max(_CPI)


def floor_for_year(year: int, floor_today: int) -> int:
    """*floor_today* (today's dollars) in *year*'s dollars."""
    cpi = _CPI[min(max(year, _CPI_FIRST), _CPI_LAST)]
    return round(floor_today * cpi / _CPI[_CPI_LAST])


_memo: dict[str, tuple[float, frozenset[str]]] = {}
_failed_at: dict[str, float] = {}
_locks: dict[str, asyncio.Lock] = {}


def _ttl(year: int) -> int:
    this_year = date.today().year
    if year >= this_year:
        return _TTL_CURRENT
    if year == this_year - 1:
        return _TTL_LAST
    return _TTL_SETTLED


def _state_key(year: int) -> str:
    # The settings, and the floor they make for this year, are part of the key,
    # so changing either (or a new CPI year) takes effect at once instead of
    # after the old list expires.
    floor = floor_for_year(year, _cfg.BLOCKBUSTER_MIN_REVENUE)
    return f"box_office_top:{year}:{_cfg.BLOCKBUSTER_TOP_N}:{floor}"


async def _fetch_year(client: httpx.AsyncClient, year: int, tmdb_key: str) -> list[str]:
    """The year's top-N TMDB movie ids by revenue, floor applied."""
    logger.info(f"External API Call: TMDB discover by revenue for {year}")
    resp = await client.get(_DISCOVER_URL, params={
        "api_key": tmdb_key,
        "primary_release_year": year,
        "sort_by": "revenue.desc",
        "include_adult": "false",
        "include_video": "false",
    })
    resp.raise_for_status()
    ids = [str(r["id"]) for r in (resp.json().get("results") or []) if r.get("id")]
    ids = ids[:_cfg.BLOCKBUSTER_TOP_N]

    async def revenue(tmdb_id: str) -> int:
        r = await client.get(_DETAILS_URL.format(tmdb_id), params={"api_key": tmdb_key})
        r.raise_for_status()
        return int(r.json().get("revenue") or 0)

    revenues = await asyncio.gather(*(revenue(i) for i in ids))
    # Revenue 0 is TMDB's "unknown", never a qualifying gross, floor or not.
    floor = max(1, floor_for_year(year, _cfg.BLOCKBUSTER_MIN_REVENUE))
    return [i for i, rev in zip(ids, revenues) if rev >= floor]


async def top_grossing_ids(
    client: httpx.AsyncClient, year: int, tmdb_key: str,
) -> frozenset[str] | None:
    """The TMDB ids that count as blockbusters for *year*, or None when the
    list couldn't be had (no key, TMDB failing) — a missing answer, not "none".
    A year still to come has none yet: that is an answer, so it is no call."""
    if year > date.today().year:
        return frozenset()
    if not tmdb_key:
        return None
    key = _state_key(year)
    now = time.time()

    hit = _memo.get(key)
    if hit and now - hit[0] < _ttl(year):
        return hit[1]

    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _memo.get(key)
        if hit and now - hit[0] < _ttl(year):
            return hit[1]

        raw = await asyncio.to_thread(get_app_state, key)
        if raw:
            try:
                stored = json.loads(raw)
                if now - float(stored["at"]) < _ttl(year):
                    ids = frozenset(stored["ids"])
                    _memo[key] = (float(stored["at"]), ids)
                    return ids
            except (ValueError, KeyError, TypeError):
                pass

        if now - _failed_at.get(key, 0) < _FAILURE_BACKOFF:
            return None
        try:
            ids = await _fetch_year(client, year, tmdb_key)
        except Exception as exc:
            logger.warning(f"Box-office top list for {year} failed: {exc}")
            _failed_at[key] = now
            return None

        await asyncio.to_thread(set_app_state, key, json.dumps({"at": now, "ids": ids}))
        _memo[key] = (now, frozenset(ids))
        return _memo[key][1]


def release_year(release_date: str | None) -> int | None:
    """The year of a TMDB ``YYYY-MM-DD`` date, or None."""
    try:
        return int((release_date or "")[:4])
    except ValueError:
        return None


async def is_blockbuster(
    client: httpx.AsyncClient, tmdb_id: str, release_date: str | None, tmdb_key: str,
) -> bool | None:
    """True when the movie is in the top-grossing list of its release year;
    None when that list couldn't be had, so the caller can keep the render
    provisional rather than cache it without the sash for its whole TTL."""
    year = release_year(release_date)
    if year is None:
        return False
    ids = await top_grossing_ids(client, year, tmdb_key)
    if ids is None:
        return None
    return str(tmdb_id) in ids
