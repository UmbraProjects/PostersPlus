#config.py
# If you're looking to change the highlighted directors, studios and cast:
#   - Source editors:  edit the lists in discovery.py directly.
#   - Docker operators (no source editing): place a JSON file at
#     /app/cache/discovery_overrides.json (inside the existing cache volume,
#     no extra mount needed).
#     See the docstring at the top of discovery.py for the full format,
#     or discovery_overrides.example.json for a ready-made sample.
import os

# Every operator-facing setting is declared through settings.env(): one call
# records the field the admin dashboard shows (group, kind, help, bounds) and
# returns the raw string to parse, honouring the saved settings file over the
# environment over the default.  See settings.py for the precedence rules.
from settings import env as _env


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

PUBLIC_URL            = _env('PUBLIC_URL', "", group='Access & serving', kind='url', label='Public URL', help="The address clients reach this instance on, e.g. https://posters.example.com. Used for the poster links the trending catalogs addon hands out. Blank derives it from each request's Host / X-Forwarded-Host / X-Forwarded-Proto headers, which works behind most proxies but lets a forged header change the links in a response a shared cache might keep.", placeholder='https://posters.example.com', advanced=True).strip().rstrip("/")
ACCESS_KEY            = _env('ACCESS_KEY', "", group='Access & serving', kind='secret', label='Access key', help='Shared secret every poster and configurator request must carry as access_key. Leave blank for open access.') or None
QUALITY_SOURCE        = _env('QUALITY_SOURCE', "aiostreams", group='Quality source', kind='choice', label='Quality source', help='Where stream-quality badges come from. QualiCache never scrapes on the request path; a cold title returns pending instead of blocking.', choices=('aiostreams', 'scraper', 'qualicache')).lower().strip()
AIOSTREAMS_URL        = _env('AIOSTREAMS_URL', "", group='Quality source', show_if=('QUALITY_SOURCE', 'aiostreams'), kind='url', label='AIOStreams URL', help='Base URL of your AIOStreams instance. Used when the quality source is aiostreams.', placeholder='https://aiostreams.example.com')
AIOSTREAMS_AUTH       = _env('AIOSTREAMS_AUTH', "", group='Quality source', show_if=('QUALITY_SOURCE', 'aiostreams'), kind='secret', label='AIOStreams auth', help='AIOStreams credentials as Base64 user:password.')

# Quality source selection.
# QUALITY_SOURCE:   "aiostreams" (default), "scraper", or "qualicache".
# SCRAPER_URL:      Stremio addon manifest/base URL — only used when QUALITY_SOURCE=scraper.
#                   Example: https://torrentio.stremio.ru/{config}/manifest.json
# QUALICACHE_URL:   Base URL of a QualiCache instance — only used when
#                   QUALITY_SOURCE=qualicache. Example: http://qualicache:8000
# QUALICACHE_API_KEY: Optional; must match QualiCache's own ACCESS_KEY when set.
# QUALICACHE_MIN_TRUST: Lowest release-group tier to accept: high, medium
#                      (default), or low.
#
# Unlike aiostreams/scraper, QualiCache never scrapes on the request path: it
# crawls catalogues in the background and answers from its own SQLite cache, so
# a cold title returns "pending" instead of blocking on a slow addon. See
# quality.fetch_quality_from_qualicache for how pending is handled.
#
# Setting QUALITY_SOURCE to a non-aiostreams backend while AIOSTREAMS_URL/AUTH
# are also set is a misconfiguration — the AIOStreams settings are ignored and a
# warning is logged at startup.
SCRAPER_URL           = _env('SCRAPER_URL', "", group='Quality source', show_if=('QUALITY_SOURCE', 'scraper'), kind='url', label='Scraper URL', help='Base URL of a Stremio stream addon, e.g. https://torrentio.strem.fun/. Only used when the quality source is scraper. Standalone addons like Torrentio and Comet work best; Stremthru Torz requires auth and should be used through AIOStreams instead.', placeholder='https://torrentio.strem.fun/').strip()
QUALICACHE_URL        = _env('QUALICACHE_URL', "", group='Quality source', show_if=('QUALITY_SOURCE', 'qualicache'), kind='url', label='QualiCache URL', help='Base URL of a QualiCache instance. Only used when the quality source is qualicache.', placeholder='http://qualicache:8000').strip()
QUALICACHE_API_KEY    = _env('QUALICACHE_API_KEY', "", group='Quality source', show_if=('QUALITY_SOURCE', 'qualicache'), kind='secret', label='QualiCache API key', help="Must match QualiCache's own ACCESS_KEY when it has one.").strip()
QUALICACHE_MIN_TRUST_VALUES = ("high", "medium", "low")
QUALICACHE_MIN_TRUST_RAW = _env('QUALICACHE_MIN_TRUST', "medium", group='Quality source', show_if=('QUALITY_SOURCE', 'qualicache'), kind='choice', label='QualiCache minimum trust', help='Lowest release-group tier to accept from QualiCache.', choices=('high', 'medium', 'low')).lower().strip()
QUALICACHE_MIN_TRUST = (
    QUALICACHE_MIN_TRUST_RAW
    if QUALICACHE_MIN_TRUST_RAW in QUALICACHE_MIN_TRUST_VALUES
    else "medium"
)
SERVER_TMDB_KEY       = _env('TMDB_API_KEY', "", group='API keys', kind='secret', label='TMDB API key', help='Fetches posters, logos and metadata. Strongly recommended; without one (and no per-client tmdb_key) titles render from Cinemeta and need an imdb_id on the request.').strip()
SERVER_MDBLIST_KEY    = _env('MDBLIST_API_KEY', "", group='API keys', kind='secret', label='MDBList API key', help='Ratings, awards, keywords and age ratings. Without it the score reads N/A and the MDBList-only sashes are unavailable.').strip()
SERVER_MDBLIST_KEY_2  = _env('MDBLIST_API_KEY_2', "", group='API keys', kind='secret', label='MDBList API key (second)', help="Retried in the same request when the primary key is rate-limited; a key that has spent its daily quota stays parked until MDBList's reset.").strip()

# TheTVDB v4 API key.  Optional — when empty, every TVDB code path is skipped
# and behaviour is identical to TMDB-only.  TVDB is used strictly as a fallback
# source of art (logos, backdrops, optionally textless posters) for titles where
# TMDB returns nothing usable, to reduce fallbacks to text titles / genre canvas.
# Unlike TMDB/MDBList (api key per request), TVDB v4 requires a one-month bearer
# token obtained from POST /login; the key is exchanged for a token internally.
SERVER_TVDB_KEY       = _env('TVDB_API_KEY', "", group='API keys', kind='secret', label='TheTVDB API key', help='Optional TheTVDB v4 key. When set, TVDB is a fallback art source (logos, backdrops, optionally posters) for titles where TMDB returns nothing usable, reducing fallbacks to text titles and genre canvases. Blank disables it entirely.').strip()
# Only required for user-supported ("subscriber") TVDB keys; blank for company keys.
TVDB_SUBSCRIBER_PIN   = _env('TVDB_SUBSCRIBER_PIN', "", group='API keys', kind='secret', label='TheTVDB subscriber PIN', help='Only for user-supported (subscriber) TVDB keys; leave blank for company keys.').strip()

def _flag(raw: str, default: bool) -> bool:
    raw = raw.strip().lower()
    if raw == "":
        return default
    return raw in ("1", "true", "yes")

# Per-asset feature toggles.  Logos/backdrops default on (low regression risk —
# pure fallback); posters default off because TVDB posters usually carry burned-in
# title text and must be vetted by text detection before use.
TVDB_USE_LOGOS        = _flag(_env("TVDB_USE_LOGOS", "true", group='TVDB fallback art', kind='bool', label='Use TVDB logos', help='Use TVDB clearlogos when TMDB and Metahub have none.'), True)
TVDB_USE_BACKDROPS    = _flag(_env("TVDB_USE_BACKDROPS", "true", group='TVDB fallback art', kind='bool', label='Use TVDB backdrops', help='Use TVDB backgrounds when no textless TMDB poster or backdrop exists.'), True)
TVDB_USE_POSTERS      = _flag(_env("TVDB_USE_POSTERS", "false", group='TVDB fallback art', kind='bool', label='Use TVDB posters', help='Use TVDB posters as a last resort. Off by default because they often carry burned-in title text; only used when text detection confirms a clean image.'), False)
# Where a TVDB clearlogo sits in the logo source chain:
#   1 = TVDB first      — beats both TMDB and the Metahub CDN
#   2 = TVDB mid        — after TMDB's own logos, but before Metahub
#   3 = TVDB last       — only when TMDB and Metahub both have nothing (default;
#                         zero change to existing output)
# TVDB clearlogos are often higher quality than TMDB/Metahub, so 1 or 2 generally
# improves results — at the cost of altering logos that currently come from those
# sources.  Ignored entirely when no TVDB key is set.
TVDB_LOGO_PRIORITY    = max(1, min(3, int(_env('TVDB_LOGO_PRIORITY', "3", group='TVDB fallback art', kind='choice', label='TVDB logo priority', help='Where a TVDB clearlogo sits in the logo chain: 1 before TMDB and Metahub, 2 after TMDB but before Metahub, 3 last resort (only when both have nothing). TVDB logos are often higher quality, so 1 or 2 improve results but change logos currently sourced from TMDB or Metahub.', choices=('1', '2', '3')))))
# Caps concurrent TVDB API calls so a burst of uncached misses can't stampede it.
TVDB_CONCURRENCY      = max(1, int(_env('TVDB_CONCURRENCY', "3", group='TVDB fallback art', kind='int', label='TVDB concurrency', help='Maximum concurrent outbound TVDB requests per worker.', min=1, max=32)))

# Cinemeta (Stremio's catalogue addon) as a key-less, IMDb-keyed art source.
# Engages only where the TMDB path can't: no TMDB key on the server or request,
# or TMDB has no record for the IMDb id. Also an extra no-art rescue tier
# (Metahub background, then poster) when TMDB knows a title but has no artwork.
# When a TMDB key is present and TMDB knows the title, nothing here runs.
CINEMETA_ENABLED = _flag(_env("CINEMETA_ENABLED", "true", group='Cinemeta fallback', kind='bool', label='Cinemeta fallback', help="Render from Stremio's Cinemeta catalogue (IMDb-keyed, no API key) when no TMDB key is available or TMDB has no record for the IMDb id, and try its Metahub art before the genre canvas when TMDB has no artwork. Needs an imdb_id (or a tt... stremio_id) on the request."), True)
CINEMETA_API_BASE = _env('CINEMETA_API_BASE', "https://v3-cinemeta.strem.io", group='Cinemeta fallback', kind='url', label='Cinemeta API base', help='Override only if you proxy Cinemeta.', advanced=True).strip().rstrip("/")

# Anime-native art sources (AniList / Kitsu).
# These engage only when a client passes an anime id (anilist_id / kitsu_id, or
# one inside stremio_id), so metadata providers that only speak imdb/tmdb/tvdb
# are completely unaffected.  Nothing is ever converted TO an anime id.  Neither provider requires an API key.
ANIME_SOURCES_ENABLED = _flag(_env("ANIME_SOURCES_ENABLED", "true", group='Anime sources', kind='bool', label='Anime sources', help='Serve art, titles, genres and a community score from AniList and Kitsu when a client passes an anilist_id or kitsu_id (or a kitsu:/anilist: stremio_id). Clients that only speak imdb/tmdb are unaffected. Neither provider needs an API key.'), True)
# Composite a title logo over anime cover art. On by default: that art either
# carries no logotype or a small block of Japanese corner text most viewers
# can't read, so a proper logo is usually an improvement. Turn off to serve the
# provider's art untouched. Logos come from TMDB/Metahub/TVDB as usual — neither
# anime provider ships them — so this needs a tmdb_id or imdb_id on the request.
ANIME_COMPOSITE_LOGO  = _flag(_env("ANIME_COMPOSITE_LOGO", "true", group='Anime sources', kind='bool', label='Composite logo on anime art', help="Composite a title logo over anime cover art. That art rarely carries a logotype (or only a small block of Japanese corner text), so a proper logo is usually an improvement; off serves the provider's art untouched. Logos come from TMDB, Metahub or TVDB, so the request needs a tmdb_id or imdb_id, or anime id mapping to supply one."), True)
# Fill in the tmdb_id / imdb_id an anime request did not bring, from the
# community Kitsu/AniList -> TMDB/IMDb mapping (Fribb's anime-lists, the same
# data AIOMetadata resolves its placeholders from).  The art and metadata spine
# stay the anime provider's; the mapped ids only unlock what those providers
# cannot supply — TMDB's logos, the landscape backdrop, and the IMDb/TMDB-keyed
# enrichment.  This is what makes a client that can only send "{id}" for an
# anime title (Nuvio's own pattern resolver: "kitsu:7442" and nothing else)
# render the same poster as one that goes through AIOMetadata.
ANIME_ID_MAP_ENABLED = _flag(_env("ANIME_ID_MAP_ENABLED", "true", group='Anime sources', kind='bool', label='Anime id mapping', help="Fill in the TMDB and IMDb ids an anime request didn't send, from the community Kitsu/AniList mapping list (downloaded daily into a local table). Lets a client that only sends a kitsu: or anilist: id get TMDB logos, landscape backdrops and IMDb-keyed ratings; art still comes from the anime provider."), True)
ANIME_ID_MAP_URL     = _env('ANIME_ID_MAP_URL', "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json", group='Anime sources', show_if=('ANIME_ID_MAP_ENABLED', 'true'), kind='url', label='Anime id mapping source', help="Where the mapping list is downloaded from. Must be Fribb's anime-list-full.json format.", advanced=True).strip()
ANIME_ID_MAP_PATH    = "/app/cache/anime_ids.db"
ANIME_ID_MAP_REFRESH_HOURS = 24
# Capped per provider, because their limits differ by an order of magnitude.
# AniList advertises 90 req/min per IP but has served a degraded 30 for a long
# while (check the x-ratelimit-limit header), so it stays tight. Kitsu publishes
# no hard limit and answers in ~0.2s, so throttling it to the same degree just
# serialises a cold catalogue burst for no reason. Art and metadata are cached
# after first fetch, so either only bites while the cache is cold.
ANILIST_CONCURRENCY   = max(1, int(_env('ANILIST_CONCURRENCY', "3", group='Anime sources', kind='int', label='AniList concurrency', help="Maximum concurrent AniList requests. AniList's effective limit is low, so keep this tight.", min=1, max=32, advanced=True)))
KITSU_CONCURRENCY     = max(1, int(_env('KITSU_CONCURRENCY', "8", group='Anime sources', kind='int', label='Kitsu concurrency', help='Maximum concurrent Kitsu requests.', min=1, max=64, advanced=True)))
ANILIST_API_URL       = _env('ANILIST_API_URL', "https://graphql.anilist.co", group='Anime sources', kind='url', label='AniList API URL', help='Override only if you proxy AniList.', advanced=True).strip()
KITSU_API_BASE        = _env('KITSU_API_BASE', "https://kitsu.io/api/edge", group='Anime sources', kind='url', label='Kitsu API base', help='Override only if you proxy Kitsu.', advanced=True).strip().rstrip("/")

# Ordered list of all configured server-side MDBList keys (primary first).
# Used by the key-rotation logic in main.py to fall back when a key is exhausted.
SERVER_MDBLIST_KEYS: list[str] = [k for k in [SERVER_MDBLIST_KEY, SERVER_MDBLIST_KEY_2] if k]

# Uvicorn worker processes.  Read by entrypoint.sh (from the settings file,
# then the environment) before Python starts; declared here so the dashboard
# offers it and validates it.
WORKERS               = max(1, int(_env("WORKERS", "1", group='Performance', kind="int", label="Worker processes",
                                        help="Uvicorn worker processes. One worker avoids duplicate uncached renders, scans and API work across processes.",
                                        min=1, max=32) or "1"))
# CDN cache TTL. "auto" (the default) advertises the composite's remaining
# lifetime as Cache-Control: public, max-age, so a caching client or CDN holds
# a trending poster for a day and a settled title for the full composite TTL.
# A number caps max-age at that many seconds; 0 sends no Cache-Control.
_CDN_CACHE_TTL_RAW    = _env('CDN_CACHE_TTL', "auto", group='Access & serving', kind='text', label='CDN cache TTL', help="Cache-Control: public, max-age=N on poster responses, capped at the composite's remaining life so a cached copy never outlives the trending rank or release status baked into it. auto (the default) advertises that remaining life with no fixed ceiling: a trending poster expires in a day, a settled title lasts the full composite TTL. A number caps it; 0 sends no Cache-Control.", placeholder='0, 3600 or auto').strip().lower()
CDN_CACHE_TTL_AUTO    = _CDN_CACHE_TTL_RAW == "auto"
try:
    CDN_CACHE_TTL     = 0 if CDN_CACHE_TTL_AUTO else int(_CDN_CACHE_TTL_RAW or "0")
    CDN_CACHE_TTL_VALID = True
except ValueError:
    # A word is a legal value here now, so a typo is a live possibility rather
    # than a theoretical one. Refusing to boot over a caching hint is a worse
    # failure than ignoring the hint and saying so.
    CDN_CACHE_TTL     = 0
    CDN_CACHE_TTL_VALID = False
# Image format for composited posters (webp or jpeg). webp is recommended.
IMAGE_FORMAT          = _env('IMAGE_FORMAT', "webp", group='Output', kind='choice', label='Image format', help='Output format for composited posters. WebP is smaller at the same quality.', choices=('webp', 'jpeg')).lower()
# Normalise the common "jpg" alias to the canonical "jpeg" that PIL's save()
# registry and the image/* media type both expect — "JPG" is not a valid PIL
# format string and would crash every render.
if IMAGE_FORMAT == "jpg":
    IMAGE_FORMAT = "jpeg"
if IMAGE_FORMAT not in ("webp", "jpeg"):
    IMAGE_FORMAT = "webp"
# JPEG output quality for composited posters (70-95). Higher = better quality, larger files.
JPEG_QUALITY          = max(70, min(95, int(_env('JPEG_QUALITY', "85", group='Output', kind='int', label='JPEG quality', help='JPEG output quality (70-95), used when the image format is jpeg. Raise to 92 for higher fidelity; lower to reduce file size.', min=70, max=95))))
# WebP output quality for composited posters (70-95).
WEBP_QUALITY          = max(70, min(95, int(_env('WEBP_QUALITY', "85", group='Output', kind='int', label='WebP quality', help='WebP output quality (70-95), used when the image format is webp (the default). Higher is better quality and larger files.', min=70, max=95))))

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
DEFAULT_LOGO_LANGUAGE = _env("DEFAULT_LOGO_LANGUAGE", os.environ.get("TMDB_LANGUAGE", "en"),  # TMDB_LANGUAGE: legacy alias
                             group='Output', kind='text', label='Default logo language', help='ISO language or locale code for title logos and poster language preference when a request names none. Region-qualified locales (fr-fr, es-es, es-mx, pt-br) select artwork tagged for that region only, falling back to English rather than to the bare language. TMDB_LANGUAGE is accepted as a legacy alias in the environment.', placeholder='en')

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
TMDB_IMAGE_CACHE_JITTER_DAYS = int(_env('TMDB_IMAGE_CACHE_JITTER_DAYS', "10", group='Caching', kind='int', label='TMDB image cache jitter (days)', help='Plus or minus half this many days of per-title jitter on TMDB poster and logo cache durations, so a batch cached together does not all expire the same day.', min=0, max=60, advanced=True))
TMDB_METADATA_CACHE_DURATION = 7    # re-check textless status / logos weekly
# TVDB artwork listings change slowly; cache the per-title artwork index and the
# resolved TVDB id for a fortnight.  Negative results (no TVDB match / no art) are
# cached for a shorter window so newly-added TVDB art is picked up reasonably soon.
TVDB_ARTWORK_CACHE_DURATION  = int(_env('TVDB_ARTWORK_CACHE_DURATION', "14", group='TVDB fallback art', kind='int', label='TVDB artwork cache (days)', help="Days to cache a title's resolved TVDB id and artwork listing.", min=1, max=365, advanced=True))   # days
TVDB_NEG_CACHE_DURATION      = int(_env('TVDB_NEG_CACHE_DURATION', "3", group='TVDB fallback art', kind='int', label='TVDB negative cache (days)', help='Days to cache a no-match / no-art result, so newly added TVDB art is picked up sooner.', min=1, max=365, advanced=True))         # days
# Artwork-type catalogue (/artwork/types) almost never changes — cache it long.
TVDB_TYPES_CACHE_DURATION    = int(_env('TVDB_TYPES_CACHE_DURATION', "30", group='TVDB fallback art', kind='int', label='TVDB artwork-type cache (days)', help='Days to cache the artwork-type catalogue, which rarely changes.', min=1, max=365, advanced=True))      # days
# Anime metadata changes slowly once a title has aired, but the community score
# does drift, so this is shorter than the TVDB artwork window.  Negative results
# (no such id on the provider) are cached briefly so a newly-added entry appears
# without waiting out the full window.
ANIME_METADATA_CACHE_DURATION = int(_env('ANIME_METADATA_CACHE_DURATION', "7", group='Anime sources', kind='int', label='Anime metadata cache (days)', help="Days to cache an anime title's provider metadata and score.", min=1, max=365, advanced=True))  # days
ANIME_NEG_CACHE_DURATION      = int(_env('ANIME_NEG_CACHE_DURATION', "3", group='Anime sources', kind='int', label='Anime negative cache (days)', help='Days to cache a no-such-id result from the provider.', min=1, max=365, advanced=True))       # days
CINEMETA_METADATA_CACHE_DURATION = int(_env('CINEMETA_METADATA_CACHE_DURATION', "7", group='Cinemeta fallback', kind='int', label='Cinemeta metadata cache (days)', help="Days to cache a title's Cinemeta document, including the IMDb-to-TMDB id it carries.", min=1, max=365, advanced=True))  # days
CINEMETA_NEG_CACHE_DURATION      = int(_env('CINEMETA_NEG_CACHE_DURATION', "3", group='Cinemeta fallback', kind='int', label='Cinemeta negative cache (days)', help='Days to cache a no-such-id result from Cinemeta.', min=1, max=365, advanced=True))       # days
# Assumed theatrical-to-digital window, for the one case nothing knows a
# movie's digital date: a Cinemeta-spined render (no TMDB key) with no
# MDBList record to hand (no key, or MDBList has no date yet).  Studio windows
# have settled at 17-45 days for most films and ~60 for the largest, so a
# theatrical date older than this is far more likely to be streaming than in
# cinemas.  Never consulted when a digital, physical or TMDB date is known.
CINEMA_ASSUMED_DIGITAL_DAYS = max(0, int(_env('CINEMA_ASSUMED_DIGITAL_DAYS', "60", group='Cinemeta fallback', kind='int', label='Assumed digital window (days)', help="Without a TMDB key, and when MDBList has no digital date for a movie, treat a theatrical release older than this many days as Streaming rather than Cinema. Typical studio windows are 17-45 days, the largest releases about 60. 0 disables the assumption (Cinema until a date is known).", min=0, max=365, advanced=True)))
DAYS_CONSIDERED_NEW          = 14
NEW_CACHE_DURATION           = 1
OLD_CACHE_DURATION           = 14
TRENDING_CACHE_DURATION      = 1
TRENDING_FETCH_TIME          = _env('TRENDING_FETCH_TIME', "", group='Trending', kind='text', label='Trending fetch time', help='Local time of day (e.g. 04:00) to refresh the trending list used by the Trending sashes. Every poster showing a rank is cached until then, so the ranks all change at once. Blank refreshes 24 hours after the previous refresh instead.', placeholder='04:00').strip()
TRENDING_FETCH_TIMEZONE      = _env('TRENDING_FETCH_TIMEZONE', "UTC", group='Trending', kind='text', label='Trending fetch timezone', help='IANA timezone for the fetch time, e.g. America/New_York.', placeholder='UTC').strip()
TRENDING_FETCH_COUNT         = int(_env('TRENDING_FETCH_COUNT', "40", group='Trending', kind='int', label='Trending count', help='Ranks 1 to this number get the Trending sash.', min=1, max=500))
TRENDING_BROAD_FETCH_COUNT   = int(_env('TRENDING_BROAD_FETCH_COUNT', "100", group='Trending', kind='int', label='Broad trending count', help='Lower-ranked trending titles, from the trending count up to this rank, qualify for the lower-priority Trending (Broad) sash.', min=1, max=1000))

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
TRENDING_SOURCE_MOVIE        = _env('TRENDING_SOURCE_MOVIE', "", group='Trending', kind='url', label='Movie trending source', help="An MDBList list page or any TMDB-shaped JSON endpoint whose order replaces TMDB's global movie trending list. Blank keeps TMDB's list.", placeholder='https://mdblist.com/lists/snoak/trending-movies').strip()
TRENDING_SOURCE_TV           = _env('TRENDING_SOURCE_TV', "", group='Trending', kind='url', label='TV trending source', help="An MDBList list page or any TMDB-shaped JSON endpoint whose order replaces TMDB's global TV trending list for both sashes and cache warming. Blank keeps TMDB's list.", placeholder='https://mdblist.com/lists/snoak/trakt-s-trending-shows').strip()
# A small Stremio addon serving the trending lists behind the sashes as three
# catalogs (movies, series, anime), for metadata addons such as AIOMetadata to
# import.  Row order and the "#N Today" labels then come from the same snapshot,
# so the numbers line up with the row.  Also switches posters requested with an
# AniList id to the AniList trending rank, the ranking the anime catalog uses.
TRENDING_CATALOGS_ENABLED    = _env('TRENDING_CATALOGS_ENABLED', "true", group='Trending', kind='bool', label='Trending catalogs addon', help='Serve the trending lists behind the Trending sashes as a Stremio addon with Trending Movies, Series and Anime catalogs, at /trending/manifest.json (/trending/<access key>/manifest.json when an access key is set). Import it into your metadata addon and the "#N Today" labels match the row order. Also gives posters requested with an AniList id the AniList trending rank used by the anime catalog. On by default; turn off to serve no addon.').strip().lower() == "true"
# Cap on how many entries are taken from a custom source, so a 10k-item list
# cannot balloon the snapshot held in memory and in trending_cache.
TRENDING_SOURCE_MAX_ITEMS    = max(1, int(_env('TRENDING_SOURCE_MAX_ITEMS', "500", group='Trending', kind='int', label='Custom source cap', help='Maximum entries taken from a custom trending source.', min=1, max=10000, advanced=True)))

# -----------------------------------------------------------------------
# Watchlist marker — a "Watchlist" sash on every title in ONE user's
# watchlist.  Self-hosted only, by design: the composite cache is shared by
# everyone who hits an instance, so a per-request watchlist would fragment
# it per user and multiply upstream quota.  One instance, one watchlist.
#
# WATCHLIST_SOURCE selects where the list comes from:
#   mdblist   the watchlist of the account behind MDBLIST_API_KEY.  MDBList
#             mirrors a linked Trakt watchlist, so this is also the free route
#             for Trakt users (Trakt's own API needs a VIP-gated app key).
#   simkl     the "Plan to Watch" list of a SIMKL account.  Needs a free SIMKL
#             app (SIMKL_CLIENT_ID); the account is linked once through the
#             device/PIN flow, whose link is printed in the log on first run.
#   trakt     TRAKT_USERNAME's public watchlist, read with TRAKT_CLIENT_ID.
#   <URL>     any MDBList list page — a shared "to watch" list, for example.
# Unset (the default) disables the feature entirely: no fetch, no sash.
# -----------------------------------------------------------------------
APP_VERSION                  = "1.2.0"
WATCHLIST_SOURCE             = _env('WATCHLIST_SOURCE', "", group='Watchlist', kind='text', label='Watchlist source', help="Self-hosted only: marks every title in one user's watchlist with a Watchlist sash. mdblist (the watchlist of the MDBList key's account, also the free route for Trakt, which MDBList mirrors), simkl, trakt, or any MDBList list page URL. Blank disables the feature.", placeholder='mdblist, simkl, trakt or a list URL').strip()
# How often the source is re-checked.  Every cycle is one cheap call (MDBList:
# one page per 500 items; SIMKL: /sync/activities, the list itself only when
# it changed; Trakt: two list calls), so this is safe well below the default.
WATCHLIST_REFRESH_MINUTES    = max(1, int(_env('WATCHLIST_REFRESH_MINUTES', "30", group='Watchlist', show_if=('WATCHLIST_SOURCE', '*'), kind='int', label='Refresh interval (minutes)', help="How often the watchlist source is re-checked. Each check is one cheap call (one MDBList page per 500 titles; SIMKL's activities timestamp, with the list only re-read when it changed; two Trakt calls).", min=1, max=1440)))
# SIMKL: which of the account's lists count as "the watchlist".  Any of
# plantowatch, watching, hold (the last two exist for TV/anime only).
WATCHLIST_SIMKL_STATUSES     = [
    s.strip().lower()
    for s in _env('WATCHLIST_SIMKL_STATUSES', "plantowatch", group='Watchlist', show_if=('WATCHLIST_SOURCE', 'simkl'), kind='list', label='SIMKL statuses', help='Which SIMKL lists count as the watchlist: plantowatch, watching, hold (comma-separated).', advanced=True, placeholder='plantowatch').split(",")
    if s.strip()
]
SIMKL_CLIENT_ID              = _env('SIMKL_CLIENT_ID', "", group='Watchlist', show_if=('WATCHLIST_SOURCE', 'simkl'), kind='secret', label='SIMKL client id', help='From a free app at simkl.com/settings/developer. Register it as "TV, devices & command line": PostersPlus links by code (the device/PIN flow), so that type needs no secret and no redirect URL. The account is then linked once from the admin dashboard\'s Watchlist group.').strip()
# Only for a SIMKL app registered as "Server apps & services"; the other two
# app types mint no secret and need none.
SIMKL_CLIENT_SECRET          = _env('SIMKL_CLIENT_SECRET', "", group='Watchlist', show_if=('WATCHLIST_SOURCE', 'simkl'), kind='secret', label='SIMKL client secret', help='Only if the SIMKL app was registered as "Server apps & services", the one type that mints a secret and then requires it. Not needed for the recommended "TV, devices & command line" type.', advanced=True).strip()
# Skips the device flow entirely when set — for a token obtained elsewhere.
# Never refreshed, so a V2 token here goes stale after 7 days.
SIMKL_ACCESS_TOKEN           = _env('SIMKL_ACCESS_TOKEN', "", group='Watchlist', show_if=('WATCHLIST_SOURCE', 'simkl'), kind='secret', label='SIMKL access token', help='Skips the device flow and uses this token as-is; never refreshed.', advanced=True).strip()
TRAKT_CLIENT_ID              = _env('TRAKT_CLIENT_ID', "", group='Watchlist', show_if=('WATCHLIST_SOURCE', 'trakt'), kind='secret', label='Trakt client id', help='From an existing Trakt API app (creating one needs Trakt VIP).').strip()
TRAKT_USERNAME               = _env('TRAKT_USERNAME', "", group='Watchlist', show_if=('WATCHLIST_SOURCE', 'trakt'), kind='text', label='Trakt username', help='The public profile whose watchlist is read.').strip()
# Optional: reads /sync/watchlist as the token's owner instead of the public
# profile, which is what a private profile needs.
TRAKT_ACCESS_TOKEN           = _env('TRAKT_ACCESS_TOKEN', "", group='Watchlist', show_if=('WATCHLIST_SOURCE', 'trakt'), kind='secret', label='Trakt access token', help="Reads /sync/watchlist as the token's owner instead of the public profile; needed for a private profile.", advanced=True).strip()
# Quality (AIOStreams) TTL — separate from rating TTL because stream availability
# for older titles is very stable.  New content keeps the 1-day window so fresh
# encodes are picked up quickly; old content is cached for much longer.
QUALITY_OLD_CACHE_DURATION   = int(_env('QUALITY_OLD_CACHE_DURATION', "90", group='Caching', kind='int', label='Quality cache for old titles (days)', help='Stream quality for older titles is stable, so it is cached this long; new titles keep a 1-day window.', min=1, max=365))   # days
# Max concurrent background quality fetches.  Caps the burst when many uncached
# titles scroll into view simultaneously so AIOStreams isn't overwhelmed.
QUALITY_BG_CONCURRENCY       = int(_env('QUALITY_BG_CONCURRENCY', "5", group='Performance', kind='int', label='Background quality fetches', help='Caps concurrent background quality fetches when many uncached titles appear at once.', min=1, max=64))

# Seconds to wait for a quality fetch when wait_for_quality=true is requested.
# Should be generous enough to allow for slow scrapers (Torrentio, Comet) but
# not so long it stalls a poster-warm run indefinitely.
QUALITY_WAIT_TIMEOUT         = float(_env('QUALITY_WAIT_TIMEOUT', "30", group='Performance', kind='float', label='Quality wait timeout (s)', help='How long a request with wait_for_quality=true waits for the scraper.', min=1, max=300))

# Max concurrent outbound MDBlist API calls.  MDBlist queues or drops requests
# when hit with too many simultaneous connections from the same key, causing
# ReadTimeouts even when the service is healthy.  3 is comfortably within their
# apparent per-key concurrency limit while still allowing good parallelism.
MDBLIST_CONCURRENCY          = int(_env('MDBLIST_CONCURRENCY', "3", group='Performance', kind='int', label='MDBList concurrency', help='Maximum concurrent outbound MDBList requests per worker. MDBList drops requests past roughly 3 per key.', min=1, max=16))

# Minimum spacing between MDBList request *starts*, shared by live renders and
# the cache warmer.  Besides the daily quota, MDBList has a short per-IP burst
# limit: too many calls in a few seconds returns 503s, then 429 with
# Retry-After: 10 for every key on the address.  MDBLIST_CONCURRENCY alone let
# a cold catalog warm reach ~10 calls/s (3 in flight at ~300 ms each), which
# tripped it; 0.2 s holds the process to 5/s.  0 disables the pacing.
MDBLIST_MIN_INTERVAL         = max(0.0, float(_env('MDBLIST_MIN_INTERVAL', "0.2", group='Performance', kind='float', label='MDBList request spacing (s)', help='Minimum seconds between the start of one outbound MDBList request and the next, across live renders and cache warming. MDBList has a short per-IP burst limit on top of the daily quota, and 3 unpaced concurrent calls can reach it during a cold catalog warm; 0.2 holds the server to 5 calls per second. 0 turns the pacing off.', min=0, max=5)))

# Max uncached poster renders in flight per worker.  Composite cache hits and
# requests coalesced onto an in-flight render are never held back — this only
# gates the pipeline that talks to TMDB / MDBList / TVDB and composites.
#
# A catalog grid opening cold can fire 50+ /poster requests in one second, and
# each render fans out to ~4 upstream calls at its peak (art, logo, rating,
# trending) before the release-status lookups.  Uncapped, that burst asks the
# shared httpx pool for several times its connection budget at once, and
# everything past the budget fails with PoolTimeout rather than waiting its
# turn.  The per-source caps above (MDBLIST_CONCURRENCY etc.) limit one
# upstream each; nothing limited the number of renders competing for the pool.
# 8 renders x ~4 calls fits inside the pool with headroom for background work.
POSTER_RENDER_CONCURRENCY    = max(1, int(_env('POSTER_RENDER_CONCURRENCY', "8", group='Performance', kind='int', label='Render concurrency', help='Maximum uncached poster renders in flight per worker. Cache hits are never held back; a burst of fresh renders (a cold catalog grid) queues past this rather than exhausting the upstream connection pool. Raise on a machine with headroom, lower on a small VPS.', min=1, max=64)))

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
IMDB_DATASET_ENABLED         = _env('IMDB_DATASET_ENABLED', "false", group='Ratings', kind='bool', label='IMDb dataset', help="Download IMDb's free, no-key, daily-refreshed ratings dataset into a local table so the imdb rating weight can be served without MDBList. Selected per request or instance with imdb_rating_source=dataset.").strip().lower() in ("1", "true", "yes")
IMDB_DATASET_PATH            = _env('IMDB_DATASET_PATH', "/app/cache/imdb_ratings.db", group='Ratings', show_if=('IMDB_DATASET_ENABLED', 'true'), kind='text', label='IMDb dataset path', help="Where the dataset's SQLite table is kept.").strip()
IMDB_DATASET_REFRESH_HOURS   = max(1, int(_env('IMDB_DATASET_REFRESH_HOURS', "24", group='Ratings', show_if=('IMDB_DATASET_ENABLED', 'true'), kind='int', label='IMDb dataset refresh (hours)', help='How often the dataset is re-downloaded.', min=1, max=720)))
# IMDb's own dataset already includes titles with a single vote; this filters
# those out for the same reason RATING_MIN_VOTES exists for MDBList sources.
IMDB_DATASET_MIN_VOTES       = max(0, int(_env('IMDB_DATASET_MIN_VOTES', "10", group='Ratings', show_if=('IMDB_DATASET_ENABLED', 'true'), kind='int', label='IMDb dataset minimum votes', help='Titles with fewer IMDb votes than this are ignored, as the rating minimum votes does for MDBList sources.', min=0, max=100000)))

# Cache warming — proactively populate the TMDB metadata cache (logos, posters,
# credits) and the MDBList rating/award cache for currently-trending titles, so
# the first real requests for them are fast and don't all hit upstream APIs at
# once. Off by default — enable explicitly once the server keys' quotas are
# understood. Each budget is a ceiling on actual API calls (cache hits don't
# count), so steady-state runs after the first one are typically far cheaper
# than the configured budgets.
CACHE_WARM_ENABLED           = _env('CACHE_WARM_ENABLED', "false", group='Cache warming', kind='bool', label='Cache warming', help="Pre-populate the TMDB and MDBList caches for trending, popular and catalog titles in the background. Off by default; enable once the server keys' quotas are understood.").strip().lower() == "true"
CACHE_WARM_TMDB_BUDGET       = int(_env('CACHE_WARM_TMDB_BUDGET', "2000", group='Cache warming', show_if=('CACHE_WARM_ENABLED', 'true'), kind='int', label='TMDB budget per cycle', help='Ceiling on actual TMDB API calls per warm cycle; cache hits do not count.', min=0, max=100000))
CACHE_WARM_MDBLIST_BUDGET    = int(_env('CACHE_WARM_MDBLIST_BUDGET', "500", group='Cache warming', show_if=('CACHE_WARM_ENABLED', 'true'), kind='int', label='MDBList budget per cycle', help='Ceiling on actual MDBList calls per warm cycle.', min=0, max=100000))
# MDBList's limit is a per-key daily quota (1000/day free) shared with live
# poster requests, and every response reports what's left. The warmer stops
# spending a key once its remaining daily requests fall to this floor, so a
# cycle can't leave the rest of the day rendering without ratings. 0 disables
# the floor (budget only).
CACHE_WARM_MDBLIST_RESERVE   = max(0, int(_env('CACHE_WARM_MDBLIST_RESERVE', "300", group='Cache warming', show_if=('CACHE_WARM_ENABLED', 'true'), kind='int', label='MDBList daily reserve', help="MDBList's limit is a per-key daily quota (1,000/day on a free key) shared with live poster requests. The warmer stops spending a key once its remaining daily requests, reported by MDBList on every response, fall to this floor, so a cycle cannot leave the rest of the day without ratings. 0 disables the floor.", min=0, max=100000)))
CACHE_WARM_INTERVAL_HOURS    = float(_env('CACHE_WARM_INTERVAL_HOURS', "24", group='Cache warming', show_if=('CACHE_WARM_ENABLED', 'true'), kind='float', label='Warm interval (hours)', help='Hours between the end of one warm cycle and the start of the next. Ignored after the first cycle once a warm hour is set.', min=1, max=720))

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
_cache_warm_at_raw = _env('CACHE_WARM_AT_HOUR', "", group='Cache warming', show_if=('CACHE_WARM_ENABLED', 'true'), kind='text', label='Warm at local hour', help="Optional fixed local hour (e.g. 4 or 4:30) to align steady-state cycles to, instead of the interval. Useful for scheduling the OCR-heavy cycle off-peak. Uses the container's TZ (UTC if unset); the first cycle after startup always runs shortly after boot.", advanced=True, placeholder='4:30').strip()
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
CACHE_WARM_QUALITY_ENABLED   = _env('CACHE_WARM_QUALITY_ENABLED', "false", group='Cache warming', show_if=('CACHE_WARM_ENABLED', 'true'), kind='bool', label='Warm quality badges', help="Also pre-fetch quality-badge data (resolution, source, HDR tokens) for every warmed title via the configured quality source. Against a public Stremio scraper addon this volume of traffic can get your server's IP rate-limited or blocked; only enable against your own AIOStreams or scraper instance.", advanced=True).strip().lower() == "true"

# Optionally pre-warm specific Stremio catalogs in addition to TMDB
# trending/popular — useful when a user has a particular addon catalog
# (e.g. a custom list) that they want fast on first load. Comma-separated
# list of addon manifest URLs (the same install links pasted into Stremio).
# Each catalog the manifest exposes is fetched (with pagination) and its
# items are resolved to TMDB ids and warmed first, ahead of trending/popular,
# within the same TMDB/MDBList budgets above.
CACHE_WARM_CATALOG_URLS = [
    u.strip() for u in _env('CACHE_WARM_CATALOG_URLS', "", group='Cache warming', show_if=('CACHE_WARM_ENABLED', 'true'), kind='list', label='Catalog manifest URLs', help='Comma-separated Stremio addon manifest URLs (the install links you would paste into Stremio). Each catalog the manifest exposes is fetched and warmed first, ahead of trending and popular, within the budgets above.', advanced=True).split(",") if u.strip()
]
# Max items pre-warmed per catalog (across pagination), so a single large
# catalog can't consume the entire warm budget.
CACHE_WARM_CATALOG_MAX_ITEMS = int(_env('CACHE_WARM_CATALOG_MAX_ITEMS', "100", group='Cache warming', show_if=('CACHE_WARM_ENABLED', 'true'), kind='int', label='Items per catalog', help="Maximum items pre-warmed per catalog across pagination, so one large catalog cannot consume the whole cycle's budget.", min=1, max=10000, advanced=True))

# Digital release (r/movieleaks) scraper settings
DIGITAL_RELEASE_MIN_AGE_DAYS = 1    # ignore posts younger than this (mods still cleaning up)
DIGITAL_RELEASE_MAX_AGE_DAYS = 30   # expire entries older than this from the cache

# Composite poster cache TTL (seconds).
# How long a fully composited poster is kept before being re-rendered.
# Each unique combination of title + rendering parameters gets its own entry,
# so changing settings immediately produces a fresh render on next request.
# Override with COMPOSITE_CACHE_TTL=X in your .env file.
COMPOSITE_CACHE_TTL        = int(_env('COMPOSITE_CACHE_TTL', "604800", group='Caching', kind='int', label='Composite cache TTL (s)', help='How long a fully rendered poster is kept before it is re-rendered. Default 604800 (7 days).', min=60, max=31536000))   # 7 days
# +/- half this many seconds of deterministic per-key jitter applied to
# COMPOSITE_CACHE_TTL, so a large batch of composites rendered around the
# same time don't all expire (and get re-rendered) at once. Default 2 days ->
# spread of 6-8 days for the default 7-day TTL. Same cache_key always gets
# the same jitter.
COMPOSITE_CACHE_TTL_JITTER = int(_env('COMPOSITE_CACHE_TTL_JITTER', "172800", group='Caching', kind='int', label='Composite TTL jitter (s)', help='Plus or minus half this many seconds of per-key jitter on the composite TTL, so a batch rendered together does not all expire at once.', min=0, max=31536000, advanced=True))
# Maximum number of composite cache entries. When exceeded the oldest entries are
# evicted on each insert to keep the table at this size. 0 = no cap (rely on TTL alone).
COMPOSITE_MAX_ENTRIES      = int(_env('COMPOSITE_MAX_ENTRIES', "0", group='Caching', kind='int', label='Composite cache max entries', help='Oldest entries are evicted past this many. 0 relies on the TTL alone.', min=0, max=10000000))
# Number of fully-rendered composites kept in the in-memory LRU (L1) cache.
# These are served without any SQLite read, keeping the hot working set off the
# OS page cache.  Each entry is roughly 100-300 KB; 500 entries ≈ 50-150 MB.
# Set to 0 to disable L1 entirely (fall through to SQLite for every request).
COMPOSITE_MEM_ENTRIES      = int(_env('COMPOSITE_MEM_ENTRIES', "500", group='Caching', kind='int', label='In-memory composites', help='Rendered posters kept in the in-memory LRU, served without a SQLite read. Each is roughly 100-300 KB; 500 is about 50-150 MB. 0 disables it.', min=0, max=100000, advanced=True))
# Set to any truthy value (1, true, yes) to skip composite cache reads and writes
# entirely. Every request re-renders from scratch. Useful during development when
# iterating on rendering changes and you don't want stale renders served.
DISABLE_COMPOSITE_CACHE    = _env('DISABLE_COMPOSITE_CACHE', "false", group='Caching', kind='bool', label='Disable composite cache', help='Skip composite cache reads and writes entirely; every request re-renders. For development only.', advanced=True).strip().lower() in ("1", "true", "yes")
# Movies with only a theatrical release date older than this many years are treated
# as "Streaming" rather than "Cinema" — guards against stale TMDB data where a
# physical/digital date was never added.  Set to 0 to disable the gate entirely.
CINEMA_MAX_AGE_YEARS       = max(0, int(_env('CINEMA_MAX_AGE_YEARS', "3", group='Rendering', kind='int', label='Cinema max age (years)', help='Movies whose only known release is a theatrical date older than this are treated as Streaming rather than Cinema, guarding against stale TMDB data missing a physical or digital date. 0 disables the gate.', min=0, max=50, advanced=True)))

def _parse_bool(val: str, default: bool = False) -> bool:
    val = val.strip().lower()
    if not val:
        return default
    return val not in ("0", "false", "no")

# Logo legibility: when a flat logo's average colour is too close to the poster
# background, recolour it (white / black / complementary accent) so it reads.
# Experimental and off by default while it's being tested — it can mis-handle
# some logos.  Set LOGO_CONTRAST_RESCUE=true to enable.
LOGO_CONTRAST_RESCUE       = _parse_bool(_env("LOGO_CONTRAST_RESCUE", "false", group='Rendering', kind='bool', label='Logo contrast rescue', help='Recolour a flat logo (white, black or accent) when it blends into the poster background; multi-colour and outline logos are never touched. Experimental and off by default while tested.', advanced=True), False)
# Emit per-logo sizing telemetry (source dims, aspect, final dims) at INFO level.
# Off by default — handy when tuning the logo size caps.
DEBUG_LOGO_SIZING          = _parse_bool(_env("DEBUG_LOGO_SIZING", "false", group='Rendering', kind='bool', label='Log logo sizing', help='Emit per-logo sizing telemetry at INFO level.', advanced=True), False)

# Paths other modules used to read from the environment themselves; declared
# here so the dashboard lists them and a saved value applies.
YUNET_MODEL_PATH           = _env("YUNET_MODEL_PATH", "", group='Rendering', kind='text', label='Face model path',
    help='Where the YuNet face-detection model is read from; blank uses the bundled copy. Face detection soft-disables if it is missing, falling back to the saliency crop.',
    placeholder='auto', advanced=True).strip()
DISCOVERY_OVERRIDES_PATH   = _env("DISCOVERY_OVERRIDES_PATH", "/app/cache/discovery_overrides.json", group='Rendering', kind='text',
    label='Discovery overrides path', help='JSON file overriding the notable studio, director and cast lists behind those sashes. See discovery_overrides.example.json.',
    advanced=True).strip() or "/app/cache/discovery_overrides.json"

# Prefer textless posters with enough votes to be meaningful, but never allow
# vote count alone to select art rated far below the best available option.
TMDB_POSTER_MIN_VOTES      = max(0, int(_env('TMDB_POSTER_MIN_VOTES', "3", group='Rendering', kind='int', label='Poster minimum votes', help='Prefer textless posters with at least this many TMDB votes.', min=0, max=100000, advanced=True)))
TMDB_POSTER_MAX_SCORE_DROP = max(
    0.0, float(_env('TMDB_POSTER_MAX_SCORE_DROP', "1.0", group='Rendering', kind='float', label='Poster max score drop', help='Never let vote count alone select art rated more than this far below the best available option.', min=0, max=10, advanced=True))
)

# Logo fill-stretch: a slim logo whose clamped size leaves it looking lost may be
# enlarged toward its size cap by up to this factor (one axis only) so it has more
# presence.  1.0 = no enlargement.  Off by default — set LOGO_STRETCH_DISABLED=false
# to enable it; LOGO_STRETCH_FACTOR then sets how aggressive the enlargement is.
LOGO_STRETCH_DISABLED      = _parse_bool(_env("LOGO_STRETCH_DISABLED", "true", group='Rendering', kind='bool', label='Disable logo stretch', help='Set to false to let a slim logo be enlarged toward its size cap so it has more presence.', advanced=True), True)
LOGO_STRETCH_FACTOR        = max(1.0, float(_env('LOGO_STRETCH_FACTOR', "1.2", group='Rendering', kind='float', label='Logo stretch factor', help='When stretching is enabled, a slim logo is enlarged toward its size cap by up to this factor (one axis only). 1.0 is no enlargement.', min=1, max=3, advanced=True)))

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
TEXTLESS_TEXT_DETECTION    = _parse_bool(_env("TEXTLESS_TEXT_DETECTION", "true", group='Text detection', kind='bool', label='Burned-in text detection', help='Detect title text on posters TMDB mislabelled as textless and skip compositing a logo over them. Uses the PP-OCRv5 Mobile detector.'), True)
TEXTLESS_DETECTION_MAX_VOTES = max(0, int(_env('TEXTLESS_DETECTION_MAX_VOTES', "3000", group='Text detection', show_if=('TEXTLESS_TEXT_DETECTION', 'true'), kind='int', label='Foreground scan vote gate', help='Foreground OCR vote limit. Titles with more TMDB votes render without waiting, skip composite caching, and enter the idle background scan queue. Raise for foreground accuracy; lower for faster stale-cache bursts. Changing it invalidates cached composites.', min=0, max=1000000)))
# Keep a small, deduplicated list of TMDB posters rejected by OCR so operators
# can review and correct upstream metadata manually.
TEXTLESS_FAKE_REPORT       = _parse_bool(_env("TEXTLESS_FAKE_REPORT", "true", group='Text detection', show_if=('TEXTLESS_TEXT_DETECTION', 'true'), kind='bool', label='Report fake textless posters', help='Keep a deduplicated list of TMDB posters rejected by OCR, for correcting upstream metadata.', advanced=True), True)
TEXTLESS_FAKE_REPORT_PATH  = _env("TEXTLESS_FAKE_REPORT_PATH", "/app/cache/fake_textless_posters.txt",
                                  group='Text detection', show_if=('TEXTLESS_FAKE_REPORT', 'true'), kind='text', label='Fake textless report path', help='Where that list is written.', advanced=True).strip() or "/app/cache/fake_textless_posters.txt"
# Minimum PP-OCR box confidence. Higher is stricter (fewer false positives,
# lower recall). Wide title-shaped regions use the PPOCR_WIDE_* fallback.
PPOCR_BOX_THRESHOLD        = max(0.0, min(
    1.0, float(_env('PPOCR_BOX_THRESHOLD', "0.70", group='Text detection', show_if=('TEXTLESS_TEXT_DETECTION', 'true'), kind='float', label='OCR box threshold', help='Minimum PP-OCR box confidence, 0-1. Higher is stricter: fewer false positives, lower recall.', min=0, max=1, advanced=True))
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
# The wide-box fallback and scan window, consumed by text_detect.py.  Declared
# here (not read from the environment there) so the dashboard's fields apply.
PPOCR_WIDE_BOX_THRESHOLD   = max(0.0, min(1.0, float(_env("PPOCR_WIDE_BOX_THRESHOLD", "0.30", group='Text detection', show_if=('TEXTLESS_TEXT_DETECTION', 'true'), kind='float',
    label='OCR wide-box threshold', help='Lower-confidence fallback for wide, title-shaped regions that PP-OCR scores below the box threshold. Never above the box threshold.',
    min=0, max=1, advanced=True) or "0.30")))
PPOCR_WIDE_MIN_ASPECT      = max(1.0, float(_env("PPOCR_WIDE_MIN_ASPECT", "3.0", group='Text detection', show_if=('TEXTLESS_TEXT_DETECTION', 'true'), kind='float',
    label='Wide-box minimum aspect', help='Minimum width-to-height ratio for the wide-box fallback.', min=1, max=20, advanced=True) or "3.0"))
PPOCR_WIDE_MIN_AREA        = max(0.0, min(1.0, float(_env("PPOCR_WIDE_MIN_AREA", "0.01", group='Text detection', show_if=('TEXTLESS_TEXT_DETECTION', 'true'), kind='float',
    label='Wide-box minimum area', help="Minimum share of the poster's area a box must cover for the wide-box fallback.", min=0, max=1, advanced=True) or "0.01")))
PPOCR_WIDE_MIN_Y           = max(0.0, min(1.0, float(_env("PPOCR_WIDE_MIN_Y", "0.55", group='Text detection', show_if=('TEXTLESS_TEXT_DETECTION', 'true'), kind='float',
    label='Wide-box minimum centre', help='Minimum vertical centre (0 top, 1 bottom) for the poster-only geometric fallback used when OCR cannot read a centred, title-shaped box.', min=0, max=1, advanced=True) or "0.55")))
TEXTLESS_SCAN_TOP          = max(0.0, min(0.9, float(_env("TEXTLESS_SCAN_TOP", "0.08", group='Text detection', show_if=('TEXTLESS_TEXT_DETECTION', 'true'), kind='float',
    label='Scan top margin', help='Fraction of poster height skipped from the top before counting title text, so studio and network bugs at the very edge are ignored. 0 scans the entire poster.', min=0, max=0.9, advanced=True) or "0.08")))
PPOCR_MODEL_URL            = _env("PPOCR_MODEL_URL", "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.8.0/onnx/PP-OCRv5/det/ch_PP-OCRv5_det_mobile.onnx",
    group='Text detection', show_if=('TEXTLESS_TEXT_DETECTION', 'true'), kind='url', label='OCR model URL', help='Where the PP-OCRv5 detection model is downloaded from when it is not already present. Change together with the checksum.', advanced=True).strip()
PPOCR_MODEL_SHA256         = _env("PPOCR_MODEL_SHA256", "4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae",
    group='Text detection', show_if=('TEXTLESS_TEXT_DETECTION', 'true'), kind='text', label='OCR model SHA-256', help='Expected checksum of that download. A mismatch fails the load rather than running an unverified model.', advanced=True).strip()
PPOCR_MODEL_PATH           = _env("PPOCR_MODEL_PATH", "", group='Text detection', show_if=('TEXTLESS_TEXT_DETECTION', 'true'), kind='text', label='OCR model path',
    help='Where the detection model is read from. Blank uses the copy baked into the image, or /app/cache/ when the image was built without one.',
    placeholder='auto', advanced=True).strip()
PPOCR_SKIP_MODEL_HASH      = _parse_bool(_env("PPOCR_SKIP_MODEL_HASH", "false", group='Text detection', show_if=('TEXTLESS_TEXT_DETECTION', 'true'), kind='bool', label='Skip OCR model checksum',
    help='Skip checksum verification of the detection model. Only sensible when deliberately supplying your own model.', advanced=True), False)

TEXTLESS_DETECTION_CONCURRENCY = max(1, min(
    EFFECTIVE_CPUS,
    int(_env('TEXTLESS_DETECTION_CONCURRENCY', "1", group='Text detection', show_if=('TEXTLESS_TEXT_DETECTION', 'true'), kind='int', label='OCR sessions', help="Independent PP-OCR sessions in a dedicated executor. Sessions split the ONNX thread budget rather than adding to it, so raising this makes each scan slower and only pays off during a cold-cache sweep; each extra session costs roughly 50 MB. Capped at the container's real CPU budget.", min=1, max=16, advanced=True)),
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

# Anime weights
#
# A title counts as anime when it carries a rating from any of these sources —
# nothing else about it is consulted, no genre or keyword heuristics. MDBList
# returns a MyAnimeList score for anime it knows by IMDb id, so this catches
# anime requested the ordinary way by TMDB/IMDb id; AniList and Kitsu only ever
# appear on the anime-native path, which is anime by definition.
ANIME_RATING_SOURCES = ("myanimelist", "anilist", "kitsu")

# Sources a request may name in anime_movie_weights / anime_tv_weights.
#
# Deliberately no server-side default alongside these: a request that sends
# neither parameter scores its anime with MOVIE_WEIGHTS / TV_WEIGHTS (or the
# request's own movie_weights / tv_weights), exactly as before the anime
# parameters existed, so existing URLs render the same score.
#
# The lists are what MDBList actually returned for anime, sampled over ~30
# titles in Sep 2026. Anime movies carried every movie source. Anime shows
# never carried a Roger Ebert review, and a Metacritic critic score appeared
# once with 5 votes — under RATING_MIN_VOTES — so both are left out; Letterboxd,
# which TV_WEIGHTS omits, was present on half the shows sampled and is kept.
ANIME_MOVIE_SOURCES = (
    "myanimelist", "anilist", "kitsu",
    "letterboxd", "trakt", "tomatoes", "popcorn", "imdb",
    "metacritic", "metacriticuser", "tmdb", "rogerebert",
)

ANIME_TV_SOURCES = (
    "myanimelist", "anilist", "kitsu",
    "trakt", "tomatoes", "popcorn", "imdb", "metacriticuser", "tmdb", "letterboxd",
)

RATING_MIN_VOTES = max(0, int(_env('RATING_MIN_VOTES', "10", group='Ratings', kind='int', label='Rating minimum votes', help='A rating source with fewer votes than this is ignored for the weighted score.', min=0, max=100000)))

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
    # Personal — the user put it there, so it outranks even the prestige tier.
    # Inert unless WATCHLIST_SOURCE is configured on the instance.
    "watchlist",
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
    # Last so the diff-encoded priorities the configurator writes ("slot@N")
    # keep every position they were saved with.
    "renewed",
]