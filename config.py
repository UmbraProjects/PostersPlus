#config.py
# If you're looking to change the highlighted directors, studios and cast:
#   - Source editors:  edit the lists in discovery.py directly.
#   - Docker operators (no source editing): place a JSON file at
#     /app/cache/discovery_overrides.json (inside the existing cache volume,
#     no extra mount needed).
#     See the docstring at the top of discovery.py for the full format,
#     or the project README for a ready-made sample.
import os


def effective_cpus() -> int:
    """Cores this process may actually use.

    os.cpu_count() reports the HOST's cores, and a Docker `--cpus=` / compose
    `cpus:` limit is enforced through the CFS quota rather than CPU affinity, so
    neither os.cpu_count() nor sched_getaffinity sees it.  A container limited to
    2 CPUs on a 4-core host reports 4 from both.

    That matters because ONNX thread scaling falls off a cliff past the real
    budget: measured on the detector's production input, a 2-CPU container runs
    135 ms at 2 threads, 153 ms at 4, 299 ms at 6 and 409 ms at 8.  Oversizing is
    far more expensive than undersizing, so take the *smallest* figure any source
    reports.
    """
    limits = []
    try:  # cgroup v2
        raw = open("/sys/fs/cgroup/cpu.max").read().split()
        if raw[0] != "max":
            limits.append(int(raw[0]) / int(raw[1]))
    except Exception:
        pass
    try:  # cgroup v1
        quota  = int(open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read())
        period = int(open("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read())
        if quota > 0 and period > 0:
            limits.append(quota / period)
    except Exception:
        pass
    try:
        limits.append(len(os.sched_getaffinity(0)))
    except Exception:
        pass
    limits.append(os.cpu_count() or 1)
    return max(1, int(min(limits)))


EFFECTIVE_CPUS = effective_cpus()

# Storage

DB_PATH               = "/app/cache/cache.db"
BADGE_DIR             = "/app/badges"
TMDB_POSTER_CACHE_DIR = "/app/cache/tmdb_posters" # base posters from TMDB
TMDB_LOGO_CACHE_DIR   = "/app/cache/tmdb_logos" # base logos from TMDB

# Environment

ACCESS_KEY            = os.environ.get("ACCESS_KEY")
AIOSTREAMS_URL        = os.environ.get("AIOSTREAMS_URL", "")
AIOSTREAMS_AUTH       = os.environ.get("AIOSTREAMS_AUTH", "")

# Quality source selection.
# QUALITY_SOURCE:   "aiostreams" (default), "scraper", or "qualicache".
# SCRAPER_URL:      Stremio addon manifest/base URL — only used when QUALITY_SOURCE=scraper.
#                   Example: https://torrentio.stremio.ru/{config}/manifest.json
# QUALICACHE_URL:   Base URL of a QualiCache instance — only used when
#                   QUALITY_SOURCE=qualicache. Example: http://qualicache:8000
# QUALICACHE_API_KEY: Optional; must match QualiCache's own ACCESS_KEY when set.
#
# Unlike aiostreams/scraper, QualiCache never scrapes on the request path: it
# crawls catalogues in the background and answers from its own SQLite cache, so
# a cold title returns "pending" instead of blocking on a slow addon. See
# quality.fetch_quality_from_qualicache for how pending is handled.
#
# Setting QUALITY_SOURCE to a non-aiostreams backend while AIOSTREAMS_URL/AUTH
# are also set is a misconfiguration — the AIOStreams settings are ignored and a
# warning is logged at startup.
QUALITY_SOURCE        = os.environ.get("QUALITY_SOURCE", "aiostreams").lower().strip()
SCRAPER_URL           = os.environ.get("SCRAPER_URL", "").strip()
QUALICACHE_URL        = os.environ.get("QUALICACHE_URL", "").strip()
QUALICACHE_API_KEY    = os.environ.get("QUALICACHE_API_KEY", "").strip()
SERVER_TMDB_KEY       = os.environ.get("TMDB_API_KEY", "").strip()
SERVER_MDBLIST_KEY    = os.environ.get("MDBLIST_API_KEY", "").strip()
SERVER_MDBLIST_KEY_2  = os.environ.get("MDBLIST_API_KEY_2", "").strip()

# TheTVDB v4 API key.  Optional — when empty, every TVDB code path is skipped
# and behaviour is identical to TMDB-only.  TVDB is used strictly as a fallback
# source of art (logos, backdrops, optionally textless posters) for titles where
# TMDB returns nothing usable, to reduce fallbacks to text titles / genre canvas.
# Unlike TMDB/MDBList (api key per request), TVDB v4 requires a one-month bearer
# token obtained from POST /login; the key is exchanged for a token internally.
SERVER_TVDB_KEY       = os.environ.get("TVDB_API_KEY", "").strip()
# Only required for user-supported ("subscriber") TVDB keys; blank for company keys.
TVDB_SUBSCRIBER_PIN   = os.environ.get("TVDB_SUBSCRIBER_PIN", "").strip()

def _tvdb_flag(key: str, default: bool) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if raw == "":
        return default
    return raw in ("1", "true", "yes")

# Per-asset feature toggles.  Logos/backdrops default on (low regression risk —
# pure fallback); posters default off because TVDB posters usually carry burned-in
# title text and must be vetted by text detection before use.
TVDB_USE_LOGOS        = _tvdb_flag("TVDB_USE_LOGOS",     True)
TVDB_USE_BACKDROPS    = _tvdb_flag("TVDB_USE_BACKDROPS", True)
TVDB_USE_POSTERS      = _tvdb_flag("TVDB_USE_POSTERS",   False)
# Where a TVDB clearlogo sits in the logo source chain:
#   1 = TVDB first      — beats both TMDB and the Metahub CDN
#   2 = TVDB mid        — after TMDB's own logos, but before Metahub
#   3 = TVDB last       — only when TMDB and Metahub both have nothing (default;
#                         zero change to existing output)
# TVDB clearlogos are often higher quality than TMDB/Metahub, so 1 or 2 generally
# improves results — at the cost of altering logos that currently come from those
# sources.  Ignored entirely when no TVDB key is set.
TVDB_LOGO_PRIORITY    = max(1, min(3, int(os.environ.get("TVDB_LOGO_PRIORITY", "3"))))
# Caps concurrent TVDB API calls so a burst of uncached misses can't stampede it.
TVDB_CONCURRENCY      = max(1, int(os.environ.get("TVDB_CONCURRENCY", "3")))

# Anime-native art sources (AniList / Kitsu).
# These engage only when a client passes anilist_id / kitsu_id — no id conversion
# is ever performed, so metadata providers that only speak imdb/tmdb/tvdb are
# completely unaffected.  Neither provider requires an API key.
ANIME_SOURCES_ENABLED = _tvdb_flag("ANIME_SOURCES_ENABLED", True)
# Composite a title logo over anime cover art. On by default: that art either
# carries no logotype or a small block of Japanese corner text most viewers
# can't read, so a proper logo is usually an improvement. Turn off to serve the
# provider's art untouched. Logos come from TMDB/Metahub/TVDB as usual — neither
# anime provider ships them — so this needs a tmdb_id or imdb_id on the request.
ANIME_COMPOSITE_LOGO  = _tvdb_flag("ANIME_COMPOSITE_LOGO", True)
# Capped per provider, because their limits differ by an order of magnitude.
# AniList advertises 90 req/min per IP but has served a degraded 30 for a long
# while (check the x-ratelimit-limit header), so it stays tight. Kitsu publishes
# no hard limit and answers in ~0.2s, so throttling it to the same degree just
# serialises a cold catalogue burst for no reason. Art and metadata are cached
# after first fetch, so either only bites while the cache is cold.
ANILIST_CONCURRENCY   = max(1, int(os.environ.get("ANILIST_CONCURRENCY", "3")))
KITSU_CONCURRENCY     = max(1, int(os.environ.get("KITSU_CONCURRENCY", "8")))
ANILIST_API_URL       = os.environ.get("ANILIST_API_URL", "https://graphql.anilist.co").strip()
KITSU_API_BASE        = os.environ.get("KITSU_API_BASE", "https://kitsu.io/api/edge").strip().rstrip("/")

# Ordered list of all configured server-side MDBList keys (primary first).
# Used by the key-rotation logic in main.py to fall back when a key is exhausted.
SERVER_MDBLIST_KEYS: list[str] = [k for k in [SERVER_MDBLIST_KEY, SERVER_MDBLIST_KEY_2] if k]

# Workers
# CDN cache TTL. When > 0, poster responses include a Cache-Control: public
# header, capped at the composite's remaining lifetime. "auto" advertises that
# remaining lifetime with no fixed ceiling. Set to 0 to send no Cache-Control.
_CDN_CACHE_TTL_RAW    = os.environ.get("CDN_CACHE_TTL", "0").strip().lower()
CDN_CACHE_TTL_AUTO    = _CDN_CACHE_TTL_RAW == "auto"
CDN_CACHE_TTL         = 0 if CDN_CACHE_TTL_AUTO else int(_CDN_CACHE_TTL_RAW or "0")
# Image format for composited posters (webp or jpeg). webp is recommended.
IMAGE_FORMAT          = os.environ.get("IMAGE_FORMAT", "webp").lower()
# Normalise the common "jpg" alias to the canonical "jpeg" that PIL's save()
# registry and the image/* media type both expect — "JPG" is not a valid PIL
# format string and would crash every render.
if IMAGE_FORMAT == "jpg":
    IMAGE_FORMAT = "jpeg"
if IMAGE_FORMAT not in ("webp", "jpeg"):
    IMAGE_FORMAT = "webp"
# JPEG output quality for composited posters (70-95). Higher = better quality, larger files.
JPEG_QUALITY          = max(70, min(95, int(os.environ.get("JPEG_QUALITY", "85"))))
# WebP output quality for composited posters (70-95).
WEBP_QUALITY          = max(70, min(95, int(os.environ.get("WEBP_QUALITY", "85"))))

# Feature Defaults 

SHOW_RATING_DISPLAY_MODE = 1
SHOW_AWARD_SASH          = True
BADGE_DISPLAY_MODE       = 4

# Poster Dimensions (500x750)

POSTER_WIDTH  = 500
POSTER_HEIGHT = 750

# Landscape Poster Dimensions (16:9)
#
# Twice the portrait width so a landscape card on a desktop client still gets a
# sharp image, and small enough that a WebP stays inside Stremio's 100kb poster
# guidance.  The source backdrop is fetched at w1280 and fitted down to this.

LANDSCAPE_WIDTH  = 1000
LANDSCAPE_HEIGHT = 563

# Rating & Genre Label Defaults

ACCENT_BAR_MODE_FONT_SIZE_RATIO    = 0.08   # font size in accent bar mode
NUMERIC_SCORE_MODE_FONT_SIZE_RATIO = 0.10   # font size in numeric mode
MINIMALIST_MODE_FONT_SIZE_RATIO    = 0.055  # font size in minimalist mode
ACCENT_BAR_MODE_FONT_Y_OFFSET      = 0.90   # vertical alignment in accent bar mode
NUMERIC_SCORE_MODE_FONT_Y_OFFSET   = 0.90   # vertical alignment in numeric score mode
MINIMALIST_MODE_FONT_X_OFFSET      = 0.05   # horizontal distance from right edge in minimalist mode
MINIMALIST_MODE_FONT_Y_OFFSET      = 0.92   # vertical position in minimalist mode (0=top, 1=bottom)

SCORE_GLOW_THRESHOLD = 85  # score threshold to activate glow
SCORE_GLOW_BLUR      = 1    # blur applied in glow mode
SCORE_GLOW_ALPHA     = 40   # alpha of the glow applied

# Logo Defaults

LOGO_MAX_W_RATIO  = 0.75   # target/max width of logo — the span every logo normalises to
LOGO_MAX_H_RATIO  = 0.25   # max height of logo (paired with LOGO_ABS_MAX_H px cap)
LOGO_BOTTOM_RATIO = 0.28   # distance of logo from the bottom
DEFAULT_LOGO_LANGUAGE = os.environ.get("DEFAULT_LOGO_LANGUAGE", os.environ.get("TMDB_LANGUAGE", "en"))

# Quality Badge Defaults

BADGE_HEIGHT = 20   # quality badge height in pixels
BADGE_GAP    = 8    # gap between horizontal stack badges in pixels

BADGE_ANCHOR_X_RATIO = 0.050   # x offset from left
BADGE_ANCHOR_Y_RATIO = 0.050   # y offset from top 

# TTL Settings

TMDB_POSTER_CACHE_DURATION   = 60
TMDB_LOGO_CACHE_DURATION     = 60
# +/- half this many days of deterministic per-key jitter applied to the
# poster/logo durations above, so a large batch cached at once (e.g. an
# initial pre-warm) doesn't all expire on the same day. 10 -> spread of
# 55-65 days for a 60-day base duration. Same cache_key always gets the
# same jitter.
TMDB_IMAGE_CACHE_JITTER_DAYS = int(os.environ.get("TMDB_IMAGE_CACHE_JITTER_DAYS", "10"))
TMDB_METADATA_CACHE_DURATION = 7    # re-check textless status / logos weekly
# TVDB artwork listings change slowly; cache the per-title artwork index and the
# resolved TVDB id for a fortnight.  Negative results (no TVDB match / no art) are
# cached for a shorter window so newly-added TVDB art is picked up reasonably soon.
TVDB_ARTWORK_CACHE_DURATION  = int(os.environ.get("TVDB_ARTWORK_CACHE_DURATION", "14"))   # days
TVDB_NEG_CACHE_DURATION      = int(os.environ.get("TVDB_NEG_CACHE_DURATION", "3"))         # days
# Artwork-type catalogue (/artwork/types) almost never changes — cache it long.
TVDB_TYPES_CACHE_DURATION    = int(os.environ.get("TVDB_TYPES_CACHE_DURATION", "30"))      # days
# Anime metadata changes slowly once a title has aired, but the community score
# does drift, so this is shorter than the TVDB artwork window.  Negative results
# (no such id on the provider) are cached briefly so a newly-added entry appears
# without waiting out the full window.
ANIME_METADATA_CACHE_DURATION = int(os.environ.get("ANIME_METADATA_CACHE_DURATION", "7"))  # days
ANIME_NEG_CACHE_DURATION      = int(os.environ.get("ANIME_NEG_CACHE_DURATION", "3"))       # days
DAYS_CONSIDERED_NEW          = 14
NEW_CACHE_DURATION           = 1
OLD_CACHE_DURATION           = 14
TRENDING_CACHE_DURATION      = 1
TRENDING_FETCH_TIME          = os.environ.get("TRENDING_FETCH_TIME", "").strip()
TRENDING_FETCH_TIMEZONE      = os.environ.get("TRENDING_FETCH_TIMEZONE", "UTC").strip()
TRENDING_FETCH_COUNT         = int(os.environ.get("TRENDING_FETCH_COUNT", "40"))
TRENDING_BROAD_FETCH_COUNT   = int(os.environ.get("TRENDING_BROAD_FETCH_COUNT", "100"))

# Where "trending" comes from.  Unset (the default) means TMDB's own global
# trending endpoint, which is US-weighted and not configurable.  Point these at a
# URL instead and that list becomes the trending set for its media type — both
# the sash's ranking and the titles the cache warmer pre-renders.
#
# Two payload shapes are accepted, which between them cover almost everything:
#
#   TMDB-shaped   {"results": [{"id": 1061474}, ...]}   ranked by array order.
#                 Any TMDB endpoint works, which is how you get a regional list
#                 TMDB's /trending cannot express:
#                   https://api.themoviedb.org/3/discover/movie
#                     ?api_key=KEY&region=FR&sort_by=popularity.desc
#
#   MDBList       a plain array of {"id": <tmdb id>, "rank": n, "mediatype": ...}
#                 ranked by "rank".  Paste the human list URL and it is converted
#                 for you — https://mdblist.com/lists/snoak/trending-movies
#                 becomes .../json automatically, and needs no MDBList API key.
#                 MDBList aggregates Trakt, Letterboxd, IMDb and others, so this
#                 is the practical way to seed trending from a service PostersPlus
#                 does not integrate with directly.
#
# The list's own order is the ranking; PostersPlus does not re-sort it. Nothing
# validates that the list is *actually* trending data — a list of your favourite
# westerns will be accepted and treated as the trending set.
#
# If a configured source fails (unreachable, malformed, or empty after parsing)
# the error is logged and NO trending data is served for that media type on that
# refresh, so the trending sash disappears rather than silently reverting to
# TMDB's list and looking like it worked.
TRENDING_SOURCE_MOVIE        = os.environ.get("TRENDING_SOURCE_MOVIE", "").strip()
TRENDING_SOURCE_TV           = os.environ.get("TRENDING_SOURCE_TV", "").strip()
# Cap on how many entries are taken from a custom source, so a 10k-item list
# cannot balloon the snapshot held in memory and in trending_cache.
TRENDING_SOURCE_MAX_ITEMS    = max(1, int(os.environ.get("TRENDING_SOURCE_MAX_ITEMS", "500")))
# Quality (AIOStreams) TTL — separate from rating TTL because stream availability
# for older titles is very stable.  New content keeps the 1-day window so fresh
# encodes are picked up quickly; old content is cached for much longer.
QUALITY_OLD_CACHE_DURATION   = int(os.environ.get("QUALITY_OLD_CACHE_DURATION", "90"))   # days
# Max concurrent background quality fetches.  Caps the burst when many uncached
# titles scroll into view simultaneously so AIOStreams isn't overwhelmed.
QUALITY_BG_CONCURRENCY       = int(os.environ.get("QUALITY_BG_CONCURRENCY", "5"))

# Seconds to wait for a quality fetch when wait_for_quality=true is requested.
# Should be generous enough to allow for slow scrapers (Torrentio, Comet) but
# not so long it stalls a poster-warm run indefinitely.
QUALITY_WAIT_TIMEOUT         = float(os.environ.get("QUALITY_WAIT_TIMEOUT", "30"))

# Max concurrent outbound MDBlist API calls.  MDBlist queues or drops requests
# when hit with too many simultaneous connections from the same key, causing
# ReadTimeouts even when the service is healthy.  3 is comfortably within their
# apparent per-key concurrency limit while still allowing good parallelism.
MDBLIST_CONCURRENCY          = int(os.environ.get("MDBLIST_CONCURRENCY", "3"))

# -----------------------------------------------------------------------
# IMDb local ratings dataset — an MDBList-free way to source the "imdb"
# weight, pulled straight from IMDb's own free, no-key, daily-refreshed
# non-commercial dataset (https://datasets.imdbws.com/title.ratings.tsv.gz).
#
# Off by default. When enabled, a background task downloads the dataset on
# IMDB_DATASET_REFRESH_HOURS and answers lookups from a local SQLite table —
# no per-title network call, no MDBList key required for that source. See
# imdb_dataset.py. Selected per-request/per-instance via the "imdb" weight's
# source setting (imdb_rating_source=dataset), independent of MDBList.
# -----------------------------------------------------------------------
IMDB_DATASET_ENABLED         = os.environ.get("IMDB_DATASET_ENABLED", "false").strip().lower() in ("1", "true", "yes")
IMDB_DATASET_PATH            = os.environ.get("IMDB_DATASET_PATH", "/app/cache/imdb_ratings.db").strip()
IMDB_DATASET_REFRESH_HOURS   = max(1, int(os.environ.get("IMDB_DATASET_REFRESH_HOURS", "24")))
# IMDb's own dataset already includes titles with a single vote; this filters
# those out for the same reason RATING_MIN_VOTES exists for MDBList sources.
IMDB_DATASET_MIN_VOTES       = max(0, int(os.environ.get("IMDB_DATASET_MIN_VOTES", "10")))

# Cache warming — proactively populate the TMDB metadata cache (logos, posters,
# credits) and the MDBList rating/award cache for currently-trending titles, so
# the first real requests for them are fast and don't all hit upstream APIs at
# once. Off by default — enable explicitly once the server keys' quotas are
# understood. Each budget is a ceiling on actual API calls (cache hits don't
# count), so steady-state runs after the first one are typically far cheaper
# than the configured budgets.
CACHE_WARM_ENABLED           = os.environ.get("CACHE_WARM_ENABLED", "false").strip().lower() == "true"
CACHE_WARM_TMDB_BUDGET       = int(os.environ.get("CACHE_WARM_TMDB_BUDGET", "2000"))
CACHE_WARM_MDBLIST_BUDGET    = int(os.environ.get("CACHE_WARM_MDBLIST_BUDGET", "500"))
CACHE_WARM_INTERVAL_HOURS    = float(os.environ.get("CACHE_WARM_INTERVAL_HOURS", "24"))

# Optionally align steady-state cache-warm cycles to a fixed local hour of day
# (e.g. "4" or "4:30" for 4:00am / 4:30am), instead of running exactly
# CACHE_WARM_INTERVAL_HOURS after the previous cycle finished. Useful for
# scheduling the (CPU-heavy, OCR-driven) warm cycle for off-peak hours.
# "Local" means the container's TZ — set TZ in your compose/.env if needed
# (defaults to UTC otherwise). Unset/empty = old behaviour (every
# CACHE_WARM_INTERVAL_HOURS). The very first cycle ever still runs after
# CACHE_WARM_STARTUP_GRACE_SECS regardless, so a fresh install pre-warms
# immediately.
CACHE_WARM_AT_HOUR: float | None = None
_cache_warm_at_raw = os.environ.get("CACHE_WARM_AT_HOUR", "").strip()
if _cache_warm_at_raw:
    try:
        if ":" in _cache_warm_at_raw:
            _hh, _mm = _cache_warm_at_raw.split(":", 1)
            CACHE_WARM_AT_HOUR = (int(_hh) + int(_mm) / 60.0) % 24
        else:
            CACHE_WARM_AT_HOUR = float(_cache_warm_at_raw) % 24
    except ValueError:
        CACHE_WARM_AT_HOUR = None

# Also pre-fetch quality badge data (resolution/source/HDR tokens) for each
# warmed title via the configured quality source (AIOStreams or scraper).
# Series default to S01E01. Off by default: this is a *per-title* request
# against your scraper/debrid-backed addon, separate from TMDB/MDBList, and
# at a budget of a couple thousand it can mean thousands of scrape requests
# in a short window. WARNING: if your quality source is a public Stremio
# addon (rather than your own self-hosted instance), this volume of traffic
# in a short period can get your server's IP rate-limited or blocked by that
# addon. Only enable this if you understand and accept that risk.
CACHE_WARM_QUALITY_ENABLED   = os.environ.get("CACHE_WARM_QUALITY_ENABLED", "false").strip().lower() == "true"

# Optionally pre-warm specific Stremio catalogs in addition to TMDB
# trending/popular — useful when a user has a particular addon catalog
# (e.g. a custom list) that they want fast on first load. Comma-separated
# list of addon manifest URLs (the same install links pasted into Stremio).
# Each catalog the manifest exposes is fetched (with pagination) and its
# items are resolved to TMDB ids and warmed first, ahead of trending/popular,
# within the same TMDB/MDBList budgets above.
CACHE_WARM_CATALOG_URLS = [
    u.strip() for u in os.environ.get("CACHE_WARM_CATALOG_URLS", "").split(",") if u.strip()
]
# Max items pre-warmed per catalog (across pagination), so a single large
# catalog can't consume the entire warm budget.
CACHE_WARM_CATALOG_MAX_ITEMS = int(os.environ.get("CACHE_WARM_CATALOG_MAX_ITEMS", "100"))

# Digital release (r/movieleaks) scraper settings
DIGITAL_RELEASE_MIN_AGE_DAYS = 1    # ignore posts younger than this (mods still cleaning up)
DIGITAL_RELEASE_MAX_AGE_DAYS = 30   # expire entries older than this from the cache

# Composite poster cache TTL (seconds).
# How long a fully composited poster is kept before being re-rendered.
# Each unique combination of title + rendering parameters gets its own entry,
# so changing settings immediately produces a fresh render on next request.
# Override with COMPOSITE_CACHE_TTL=X in your .env file.
COMPOSITE_CACHE_TTL        = int(os.environ.get("COMPOSITE_CACHE_TTL", "604800"))   # 7 days
# +/- half this many seconds of deterministic per-key jitter applied to
# COMPOSITE_CACHE_TTL, so a large batch of composites rendered around the
# same time don't all expire (and get re-rendered) at once. Default 2 days ->
# spread of 6-8 days for the default 7-day TTL. Same cache_key always gets
# the same jitter.
COMPOSITE_CACHE_TTL_JITTER = int(os.environ.get("COMPOSITE_CACHE_TTL_JITTER", str(2 * 86400)))
# Maximum number of composite cache entries. When exceeded the oldest entries are
# evicted on each insert to keep the table at this size. 0 = no cap (rely on TTL alone).
COMPOSITE_MAX_ENTRIES      = int(os.environ.get("COMPOSITE_MAX_ENTRIES", "0"))
# Number of fully-rendered composites kept in the in-memory LRU (L1) cache.
# These are served without any SQLite read, keeping the hot working set off the
# OS page cache.  Each entry is roughly 100-300 KB; 500 entries ≈ 50-150 MB.
# Set to 0 to disable L1 entirely (fall through to SQLite for every request).
COMPOSITE_MEM_ENTRIES      = int(os.environ.get("COMPOSITE_MEM_ENTRIES", "500"))
# Set to any truthy value (1, true, yes) to skip composite cache reads and writes
# entirely. Every request re-renders from scratch. Useful during development when
# iterating on rendering changes and you don't want stale renders served.
DISABLE_COMPOSITE_CACHE    = os.environ.get("DISABLE_COMPOSITE_CACHE", "").strip().lower() in ("1", "true", "yes")
# Movies with only a theatrical release date older than this many years are treated
# as "Streaming" rather than "Cinema" — guards against stale TMDB data where a
# physical/digital date was never added.  Set to 0 to disable the gate entirely.
CINEMA_MAX_AGE_YEARS       = max(0, int(os.environ.get("CINEMA_MAX_AGE_YEARS", "3")))

def _parse_bool_env(key: str, default: bool = False) -> bool:
    val = os.environ.get(key, "").strip().lower()
    if not val:
        return default
    return val not in ("0", "false", "no")

# Logo legibility: when a flat logo's average colour is too close to the poster
# background, recolour it (white / black / complementary accent) so it reads.
# Experimental and off by default while it's being tested — it can mis-handle
# some logos.  Set LOGO_CONTRAST_RESCUE=true to enable.
LOGO_CONTRAST_RESCUE       = _parse_bool_env("LOGO_CONTRAST_RESCUE", False)
# Emit per-logo sizing telemetry (source dims, aspect, final dims) at INFO level.
# Off by default — handy when tuning the logo size caps.
DEBUG_LOGO_SIZING          = _parse_bool_env("DEBUG_LOGO_SIZING", False)

# Prefer textless posters with enough votes to be meaningful, but never allow
# vote count alone to select art rated far below the best available option.
TMDB_POSTER_MIN_VOTES      = max(0, int(os.environ.get("TMDB_POSTER_MIN_VOTES", "3")))
TMDB_POSTER_MAX_SCORE_DROP = max(
    0.0, float(os.environ.get("TMDB_POSTER_MAX_SCORE_DROP", "1.0"))
)

# Logo fill-stretch: a slim logo whose clamped size leaves it looking lost may be
# enlarged toward its size cap by up to this factor (one axis only) so it has more
# presence.  1.0 = no enlargement.  Off by default — set LOGO_STRETCH_DISABLED=false
# to enable it; LOGO_STRETCH_FACTOR then sets how aggressive the enlargement is.
LOGO_STRETCH_DISABLED      = _parse_bool_env("LOGO_STRETCH_DISABLED", True)
LOGO_STRETCH_FACTOR        = max(1.0, float(os.environ.get("LOGO_STRETCH_FACTOR", "1.2")))

# Detect burned-in title text on posters TMDB mislabelled as "textless".  When
# detected, PostersPlus skips compositing its own logo/title so you don't get a
# double title.  Uses the PP-OCRv5 Mobile detector (one-time ~4.6MB model
# download). Foreground scans are vote-gated to protect burst latency; skipped
# assets are scanned later by the idle background queue.
#
# On by default; set TEXTLESS_TEXT_DETECTION=false to opt out.
#
# 3000 covers most titles while excluding the high-vote bulk of large libraries.
# Raise it for maximum foreground accuracy or lower it for faster stale-cache bursts.
# Changing it invalidates cached composites.
TEXTLESS_TEXT_DETECTION    = _parse_bool_env("TEXTLESS_TEXT_DETECTION", True)
TEXTLESS_DETECTION_MAX_VOTES = max(0, int(os.environ.get("TEXTLESS_DETECTION_MAX_VOTES", "3000")))
# Keep a small, deduplicated list of TMDB posters rejected by OCR so operators
# can review and correct upstream metadata manually.
TEXTLESS_FAKE_REPORT       = _parse_bool_env("TEXTLESS_FAKE_REPORT", True)
TEXTLESS_FAKE_REPORT_PATH  = os.environ.get(
    "TEXTLESS_FAKE_REPORT_PATH",
    "/app/cache/fake_textless_posters.txt",
).strip() or "/app/cache/fake_textless_posters.txt"
# Minimum PP-OCR box confidence. Higher is stricter (fewer false positives,
# lower recall). Wide title-shaped regions use the PPOCR_WIDE_* fallback.
PPOCR_BOX_THRESHOLD        = max(0.0, min(
    1.0, float(os.environ.get("PPOCR_BOX_THRESHOLD", "0.70"))
))
# Independent PP-OCR sessions used for parallel cold-cache scans, run in a
# dedicated executor.  Across worker processes, keep WORKERS x this value at or
# below EFFECTIVE_CPUS.
#
# Default 1, because raising it is a throughput-for-latency trade that usually
# loses.  Sessions SPLIT the ONNX thread budget rather than adding to it, so on
# 4 cores 1 session gets 4 intra-op threads and 2 sessions get 2 each.  Measured
# on real poster art: a single scan is ~88 ms at 1 session but ~139 ms at 2,
# while bulk throughput moves the other way, 8.9 -> 11.0 scans/s.  Neither
# setting measurably slows concurrent compositing (0.98x vs 1.09x render latency
# under saturated OCR), so contention is not the deciding factor.
#
# The queue decides it, and the queue is usually not busy: the background scan
# worker is a single task that drains one item at a time and waits for foreground
# idle, so it can never occupy a second session.  Extra sessions only earn their
# keep when many low-vote titles need FOREGROUND scans at once — a cold-cache
# sweep of a large new library.  Once text_detection_cache is populated, scans
# are occasional and latency-visible (someone is waiting on that poster), which
# is exactly where 1 session wins.
#
# Memory (measured, bundled mobile model): the first session is ~115 MB, mostly
# the onnxruntime instance itself, so that is the price of having detection on at
# all.  Each additional session adds ~50 MB — going 1 -> 2 cost ~86 MB more peak
# RSS under sustained scanning.
TEXTLESS_DETECTION_CONCURRENCY = max(1, min(
    EFFECTIVE_CPUS,
    int(os.environ.get("TEXTLESS_DETECTION_CONCURRENCY", "1")),
))

# Rating Score Weight Defaults

# Keep zero-weight providers here: they remain available as user-configurable options.

MOVIE_WEIGHTS = {   # set weight of movie ranking providers, must sum to 1
    "letterboxd":     0.8,
    "trakt":          0,
    "tomatoes":       0.2,
    "popcorn":        0, # popcorn is the api response MDblist uses for tomatoes audience
    "imdb":           0,
    "metacritic":     0,
    "metacriticuser": 0,
    "tmdb":           0,
    "rogerebert":     0,
    "myanimelist":    0,
    # Only ever present for titles requested by anilist_id / kitsu_id. A source
    # that isn't present contributes nothing and the remaining weights
    # renormalise (see calculate_weighted_score), so a non-zero weight here is
    # inert for every non-anime title.
    "anilist":        0,
    "kitsu":          0,
}

TV_WEIGHTS = {   # set weight of TV ranking providers, must sum to 1
    "trakt":          0.8,
    "tomatoes":       0.2,
    "popcorn":        0,
    "imdb":           0,
    "metacritic":     0,
    "metacriticuser": 0,
    "tmdb":           0,
    "myanimelist":    0,
    "anilist":        0,
    "kitsu":          0,
}

RATING_MIN_VOTES = max(0, int(os.environ.get("RATING_MIN_VOTES", "10")))

# Map badge file names to strings (no need to touch)

BADGE_FILES: dict[str, str] = {
    "4K":     "4K",
    "1080P":  "1080p",
    "REMUX":  "Remux",
    "WEBDL":  "Web",
    "DV":     "DV",
    "HDR10+": "HDR10+",
    "HDR10":  "HDR10",
}

# Maps TMDB categories to numerics (no need to touch in most cases)

GENRE_MAP = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy",
    80: "Crime", 99: "Documentary", 18: "Drama", 10751: "Family",
    14: "Fantasy", 36: "History", 27: "Horror", 10402: "Music",
    9648: "Mystery", 10749: "Romance", 878: "Sci-Fi", 53: "Thriller",
    10752: "War", 37: "Western",
    10759: "Action", 10762: "Kids", 10763: "News", 10764: "Reality",
    10765: "Sci-Fi", 10766: "Soap", 10767: "Talk", 10768: "War",
}

# Can re-order to change the priority that genres appear with (reference genre map above)
# Default Horror, Thriller, Mystery, Sci-Fi, Crime, Comedy, Fantasy, Adventure, Family, Action, History
# Music, War, Western, Documentary, Drama, Adventure, Reality, Kids, News, Soap, Talk
# Duplicate entries are not an accident, for certain genres TMDB uses two numbers, one for movies, one for shows.

GENRE_PRIORITY = [
    27, 53, 9648, 878, 10765, 80, 35, 10749, 14, 16, 10751,
    28, 10759, 36, 10402, 10752, 10768, 37, 99, 18, 12,
    10764, 10762, 10763, 10766, 10767,
]

# Separate ordering for titles requested by anime id, because the list above is
# tuned for a Western catalogue: there, Horror / Thriller / Mystery / Crime are
# strong discriminators and Action / Adventure / Drama are generic filler, so
# they sit near the end. Anime inverts that. Action, Adventure and Fantasy are
# the *primary* descriptors, while Mystery, Psychological and Supernatural are
# applied liberally as secondary tags — AniList tags Attack on Titan "Mystery"
# and Kitsu tags One Piece "Crime". Running anime through the Western order
# therefore surfaced the least representative label almost every time
# (One Piece -> Comedy, Evangelion -> Thriller, Hunter x Hunter -> Fantasy).
#
# This order was checked against the real genre lists of a sample of well-known
# titles from both providers. It is a presentation choice, not a correctness
# one — reorder freely if a different label reads better to you.
ANIME_GENRE_PRIORITY = [
    10749,            # Romance — if it's a romance, that's the hook
    27,               # Horror
    37,               # Western — vanishingly rare in anime, so highly telling
    99,               # Documentary — likewise
    878, 10765,       # Sci-Fi (also where Mecha lands)
    53,               # Thriller (also where Psychological lands)
    12,               # Adventure — the long-running shounen staple
    28, 10759,        # Action
    9648,             # Mystery — demoted below Action; over-applied in anime
    14,               # Fantasy (also where Supernatural lands)
    35,               # Comedy
    80,               # Crime
    10752, 10768,     # War (also where Military lands)
    36,               # History
    10402,            # Music
    18,               # Drama (also where Slice of Life lands)
    10762,            # Kids
    10751,            # Family
    16,               # Animation — guaranteed floor, always present
]

# Text based fallback, not important if everything is working properly

QUALITY_LABELS: dict[str, str] = {
    "4K":     "4K",
    "1080P":  "1080p",
    "REMUX":  "Remux",
    "WEBDL":  "Web",
    "DV":     "DV",
    "HDR10+": "HDR10+",
    "HDR10":  "HDR10",
    "ATMOS":  "Atmos",
    "DTSX":   "DTS:X",
}

# Normalizes all scores to be out of 100

SCORE_NORMALISERS = {
    "imdb":           lambda v: (v / 10)  * 100,
    "letterboxd":     lambda v: (v / 5)   * 100,
    "trakt":          lambda v: v,
    "tomatoes":       lambda v: v,
    "popcorn":        lambda v: v,
    "metacritic":     lambda v: v,
    "metacriticuser": lambda v: (v / 10)  * 100,
    "tmdb":           lambda v: v,
    "rogerebert":      lambda v: (v / 4)   * 100,
    "myanimelist":    lambda v: (v / 10)  * 100,
    # AniList averageScore and Kitsu averageRating are both already percentages.
    "anilist":        lambda v: v,
    "kitsu":          lambda v: v,
}

# Default Sash Priority

# Kept in sync with SASH_SLOTS in configurator.html — the configurator's
# default order and every bundled preset use this same sequence.
SASH_PRIORITY: list[str] = [
    # Prestige — rare and timeless, so they outrank everything else.
    "wins",
    "gg_wins",
    "festival",
    "pic_noms",
    "metacritic",
    "gg_noms",
    # Timely — narrow, time-boxed windows.  Above the curated lists below so a
    # notable-cast match can't bury "this is new right now".
    "trending",
    "trending_broad",
    "premiere",
    "new_release",
    "just_added",
    "new_season",
    "season_finale",
    # Curated taste — common matches, so they sit under the timely tier.
    "studio",
    "director",
    "cast",
    # Static flavour — always true, never urgent.
    "cult",
    "foreign",
    "true_story",
    "short_film",
    "mini_series",
    "binge_ready",
    # Broad lifecycle / release-status fallbacks — match almost everything, so
    # they sit last and only surface when nothing above did.
    "returning",
    "airing",
    "cancelled",
    "ended",
    "physical",
    "streaming",
    "cinema",
    "production",
]