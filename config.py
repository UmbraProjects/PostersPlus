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
from settings import env as _env, merge_order


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
# Images an operator pasted or uploaded in the dashboard's Artwork view.  Not a
# cache: nothing prunes it, and a file goes when no override uses it.
CUSTOM_ART_DIR        = "/app/cache/custom_art"
# Preview images for the operator's configurator presets (dashboard Presets).
PRESET_ART_DIR        = "/app/cache/preset_art"
# Label fonts the operator uploaded in the dashboard (Fonts), with their index.
CUSTOM_FONT_DIR       = "/app/cache/custom_fonts"
# The dashboard Logs view's copy of the log (log_store.py).
LOG_DIR               = "/app/cache/logs"

# Environment

PUBLIC_SHARE_ORIGIN   = _env('PUBLIC_SHARE_ORIGIN', "", group='Access & serving', kind='url', label='Public share origin', help="HTTPS origin (e.g. https://posters.example.com, no path/query) to mark this instance public for Share settings; blank keeps shares on https://postersplus.cc. Only for public keyless instances — does not alter access controls.", placeholder='https://posters.example.com').strip()
PUBLIC_URL            = _env('PUBLIC_URL', "", group='Access & serving', kind='url', label='Public URL', help="Public base URL for poster links in the trending catalogs addon (e.g. https://posters.example.com). Blank derives it from Host / X-Forwarded-* headers (spoofable behind shared caches).", placeholder='https://posters.example.com', advanced=True).strip().rstrip("/")
ACCESS_KEY            = _env('ACCESS_KEY', "", group='Access & serving', kind='secret', label='Access key', help='Shared secret every poster and configurator request must carry as access_key. Leave blank for open access.') or None
# For operators who protect the configurator with a separate login (Authelia,
# Pangolin, an SSO proxy...): posters are fetched by clients that can't sign in
# through a browser, so /poster keeps its access key, but the configurator no
# longer asks for one and hands the key to the page itself.  Anyone who can open
# the configurator can therefore read the key — it is only as safe as that login.
CONFIGURATOR_EXTERNAL_AUTH = _env('CONFIGURATOR_EXTERNAL_AUTH', "false", group='Access & serving', kind='bool', label='Configurator protected externally', help="Enable only if an external login (Authelia, Pangolin, SSO) protects all configurator paths (`/`, `/server-caps`, `/search`, `/resolve-imdb`, `/resolve-tmdb`, `/trending-top10`, `/report`, `/debug/fallback-gallery`). Opens without ?access_key= and injects the key via /server-caps; posters still require it. No effect without ACCESS_KEY.").strip().lower() in ("1", "true", "yes")
# Which other sites may show the configurator in a frame (frame-ancestors).
# Only this instance by default, so another page can't lay the configurator,
# which holds the user's API keys and the access key, under its own clicks.
CONFIGURATOR_FRAME_ANCESTORS = _env('CONFIGURATOR_FRAME_ANCESTORS', "", group='Access & serving', kind='list', label='Configurator frame origins', help="Origins allowed to embed the configurator in an iframe (e.g. https://home.example.com for Homarr/Organizr, or * for any). Blank allows only same-origin.", placeholder='https://home.example.com', advanced=True).strip()
# Off by default: on a public instance an Admin link in every visitor's header
# only invites people to try keys against the dashboard.  _flag isn't defined
# yet at this point, hence the inline parse.
SHOW_ADMIN_LINK       = _env('SHOW_ADMIN_LINK', "false", group='Access & serving', kind='bool', label='Admin link in configurator', help="Show an Admin link in the configurator header. Still requires ADMIN_KEY to sign in and stays hidden when the dashboard is disabled.").strip().lower() in ("1", "true", "yes")
# Poster reports (reports.py): a Report button on the configurator's preview
# that files into the dashboard's Reports view.  Limits are per address, so
# behind a reverse proxy FORWARDED_ALLOW_IPS has to be set; reports pause
# while it isn't (see reports.forwarding_state).
REPORTS_ENABLED         = _env('REPORTS_ENABLED', "false", group='Poster reports', kind='bool', label='Poster reports', help="Adds a Report button to configurator previews (wrong art/logo/details or text on textless); reports appear in the Reports view (requires ADMIN_KEY). Behind a proxy, set FORWARDED_ALLOW_IPS or per-IP limits stay paused.").strip().lower() in ("1", "true", "yes")
REPORTS_PER_IP          = int(_env('REPORTS_PER_IP', "10", group='Poster reports', kind='int', label='Reports per address per day', help='Max reports per IP in 24 hours. Resolved reports stop counting toward the limit; dismissed and deleted ones still count.', min=1, max=1000, show_if=('REPORTS_ENABLED', 'true')))
REPORTS_PURGE_THRESHOLD = int(_env('REPORTS_PURGE_THRESHOLD', "50", group='Poster reports', kind='int', label='Mass-report threshold', help="Exceeding this many reports in 7 days (including refused, excluding resolved) purges that IP's open reports and blocks it (manageable in Reports). 0 disables.", min=0, max=100000, show_if=('REPORTS_ENABLED', 'true')))
# Largest portrait width the resolution URL parameter may ask for.  Off (500,
# the default canvas) unless raised: a 2000 px render costs ~12x the CPU of a
# 500 and a large peak of memory, so a public instance must not let anyone
# request them.  Requests above the cap get the largest allowed size instead.
MAX_POSTER_RESOLUTION = int(_env('MAX_POSTER_RESOLUTION', "500", group='Access & serving', kind='choice', label='Maximum poster resolution', help="Max portrait width allowed via the resolution parameter (higher requests clamp here; 500 disables larger sizes). High sizes cost far more CPU/RAM (2000 px ≈ 12× 500 px).", choices=('500', '780', '1000', '1500', '2000')))
# Whether the configurator's live preview renders at the resolution picked
# there.  Off: the preview stays at 500 wide (it is displayed smaller than that
# anyway, and every fixed-pixel setting scales with the canvas, so it looks the
# same); on, every settings change while a large size is picked costs a full
# render at that size.
PREVIEW_AT_RESOLUTION = _env('PREVIEW_AT_RESOLUTION', "false", group='Access & serving', show_if=('MAX_POSTER_RESOLUTION', ('780', '1000', '1500', '2000')), kind='bool', label='Preview at chosen resolution', help="Render configurator live previews at the selected resolution instead of 500 px. Useful for judging sharpness, but heavier per change (2000 px ≈ 12× 500 px).").strip().lower() in ("1", "true", "yes")
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
SCRAPER_URL           = _env('SCRAPER_URL', "", group='Quality source', show_if=('QUALITY_SOURCE', 'scraper'), kind='url', label='Scraper URL', help='Base URL of a Stremio stream addon (used when quality source is scraper). Works best with standalone addons like Torrentio or Comet; use AIOStreams for Stremthru Torz.', placeholder='https://torrentio.strem.fun/').strip()
QUALICACHE_URL        = _env('QUALICACHE_URL', "", group='Quality source', show_if=('QUALITY_SOURCE', 'qualicache'), kind='url', label='QualiCache URL', help='Base URL of a QualiCache instance. Only used when the quality source is qualicache.', placeholder='http://qualicache:8000').strip()
QUALICACHE_API_KEY    = _env('QUALICACHE_API_KEY', "", group='Quality source', show_if=('QUALITY_SOURCE', 'qualicache'), kind='secret', label='QualiCache API key', help="Must match QualiCache's own ACCESS_KEY when it has one.").strip()
QUALICACHE_MIN_TRUST_VALUES = ("high", "medium", "low")
QUALICACHE_MIN_TRUST_RAW = _env('QUALICACHE_MIN_TRUST', "medium", group='Quality source', show_if=('QUALITY_SOURCE', 'qualicache'), kind='choice', label='QualiCache minimum trust', help='Lowest release-group tier to accept from QualiCache.', choices=('high', 'medium', 'low')).lower().strip()
QUALICACHE_MIN_TRUST = (
    QUALICACHE_MIN_TRUST_RAW
    if QUALICACHE_MIN_TRUST_RAW in QUALICACHE_MIN_TRUST_VALUES
    else "medium"
)
SERVER_TMDB_KEY       = _env('TMDB_API_KEY', "", group='API keys', kind='secret', label='TMDB API key', help='Fetches posters, logos and metadata (API key or Read Access Token). Without one (or per-client tmdb_key), titles fall back to Cinemeta and require an imdb_id.').strip()
SERVER_MDBLIST_KEY    = _env('MDBLIST_API_KEY', "", group='API keys', kind='secret', label='MDBList API key', help='Ratings, awards, keywords and age ratings. Without it the score reads N/A and the MDBList-only sashes are unavailable.').strip()
SERVER_MDBLIST_KEY_2  = _env('MDBLIST_API_KEY_2', "", group='API keys', kind='secret', label='MDBList API key (second)', help="Retried in the same request when the primary key is rate-limited; a key that has spent its daily quota stays parked until MDBList's reset.").strip()

# TheTVDB v4 API key.  Optional — when empty, every TVDB code path is skipped
# and behaviour is identical to TMDB-only.  TVDB is used strictly as a fallback
# source of art (logos, backdrops, optionally textless posters) for titles where
# TMDB returns nothing usable, to reduce fallbacks to text titles / genre canvas.
# Unlike TMDB/MDBList (api key per request), TVDB v4 requires a one-month bearer
# token obtained from POST /login; the key is exchanged for a token internally.
SERVER_TVDB_KEY       = _env('TVDB_API_KEY', "", group='API keys', kind='secret', label='TheTVDB API key', help='Optional TheTVDB v4 key for fallback logos, backdrops and posters when TMDB has no usable art. Blank disables TVDB.').strip()
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
TVDB_USE_POSTERS      = _flag(_env("TVDB_USE_POSTERS", "false", group='TVDB fallback art', kind='bool', label='Use TVDB posters', help='Last-resort TVDB posters: uses OCR-clean no-language (textless) posters to replace texted art, or user-language posters when no art exists.'), False)
# A user-selectable poster source, like FANART_POSTERS: poster_source=tvdb
# takes TVDB's best no-language poster (textless in practice), or under
# original art its best poster in the request's language order.
TVDB_POSTER_SOURCE    = _flag(_env("TVDB_POSTER_SOURCE", "false", group='TVDB fallback art', kind='bool', label='Offer TVDB posters', help='Offer TVDB as a poster/backdrop source (textless no-language, or user-language under Original Art; falls back to TMDB). Needs TVDB key; adds downloads, cache and OCR scans.'), False)

# Optional fanart.tv source.  With a project key AND FANART_POSTERS on, the
# configurator offers poster_source=fanart (most-liked fanart.tv poster, TMDB
# fallback).  Off by default: it adds poster downloads, disk cache and text
# scans alongside the TMDB art.  Random picks are RANDOM_POSTERS.
FANART_API_KEY        = _env('FANART_API_KEY', "", group='API keys', kind='secret', label='Fanart API key', help='Optional Fanart project key, needed for the Fanart poster source (see FANART_POSTERS).').strip()
FANART_POSTERS        = _flag(_env("FANART_POSTERS", "false", group='Fanart', kind='bool', label='Offer Fanart posters', help='Offer Fanart as a poster source (all titles or anime only; most-liked textless, or user-language under Original Art; TMDB fallback). Needs Fanart key (+ TVDB key for series).'), False)

# Artwork overrides shared between instances (art_overrides.py, "Sharing").
ART_OVERRIDES_SHARE      = _flag(_env("ART_OVERRIDES_SHARE", "false", group='Artwork sharing', kind='bool', label='Share artwork overrides', help='Expose your Artwork overrides read-only at /art-overrides/export.json (and uploaded/linked images at /custom-art/) for other instances to follow. Nothing else is shared.'), False)
ART_OVERRIDES_REMOTE_URL = _env('ART_OVERRIDES_REMOTE_URL', "", group='Artwork sharing', kind='url', label='Follow artwork from', help="Another Posters+ instance (with Share artwork overrides on) to sync Artwork overrides from hourly (e.g. https://posters.example.com). Local overrides win; uploaded images copy once. Blank disables.", placeholder='https://posters.example.com').strip()

# Where a TVDB clearlogo sits in the logo source chain:
#   1 = TVDB first      — beats both TMDB and the Metahub CDN
#   2 = TVDB mid        — after TMDB's own logos, but before Metahub
#   3 = TVDB last       — only when TMDB and Metahub both have nothing (default;
#                         zero change to existing output)
# TVDB clearlogos are often higher quality than TMDB/Metahub, so 1 or 2 generally
# improves results — at the cost of altering logos that currently come from those
# sources.  Ignored entirely when no TVDB key is set.
TVDB_LOGO_PRIORITY    = max(1, min(3, int(_env('TVDB_LOGO_PRIORITY', "3", group='TVDB fallback art', kind='choice', label='TVDB logo priority', help='TVDB clearlogo order: 1 before TMDB & Metahub, 2 between TMDB and Metahub, 3 last resort. Higher priority (1 or 2) can improve quality but changes existing logos.', choices=('1', '2', '3')))))
# Caps concurrent TVDB API calls so a burst of uncached misses can't stampede it.
TVDB_CONCURRENCY      = max(1, int(_env('TVDB_CONCURRENCY', "3", group='TVDB fallback art', kind='int', label='TVDB concurrency', help='Maximum concurrent outbound TVDB requests per worker.', min=1, max=32)))

# Cinemeta (Stremio's catalogue addon) as a key-less, IMDb-keyed art source.
# Engages only where the TMDB path can't: no TMDB key on the server or request,
# or TMDB has no record for the IMDb id. Also an extra no-art rescue tier
# (Metahub background, then poster) when TMDB knows a title but has no artwork.
# When a TMDB key is present and TMDB knows the title, nothing here runs.
CINEMETA_ENABLED = _flag(_env("CINEMETA_ENABLED", "true", group='Cinemeta fallback', kind='bool', label='Cinemeta fallback', help="Fallback to Stremio Cinemeta (no key, needs imdb_id / tt… stremio_id) when TMDB is unavailable or lacks a record, and try Metahub art before genre canvases."), True)
CINEMETA_API_BASE = _env('CINEMETA_API_BASE', "https://v3-cinemeta.strem.io", group='Cinemeta fallback', kind='url', label='Cinemeta API base', help='Override only if you proxy Cinemeta.', advanced=True).strip().rstrip("/")

# Anime-native art sources (AniList / Kitsu).
# These engage only when a client passes an anime id (anilist_id / kitsu_id, or
# one inside stremio_id), so metadata providers that only speak imdb/tmdb/tvdb
# are completely unaffected.  Nothing is ever converted TO an anime id.  Neither provider requires an API key.
# A MyAnimeList id (mal_id, or mal: in stremio_id) is rendered as the Kitsu or
# AniList id the mapping below gives it, so it needs ANIME_ID_MAP_ENABLED.
ANIME_SOURCES_ENABLED = _flag(_env("ANIME_SOURCES_ENABLED", "true", group='Anime sources', kind='bool', label='Anime sources', help='Serve art, titles, genres and scores from AniList/Kitsu (no key) when passed anilist_id, kitsu_id, or kitsu:/anilist: stremio_id. IMDb/TMDB clients unaffected.'), True)
# Composite a title logo over anime cover art. On by default: that art either
# carries no logotype or a small block of Japanese corner text most viewers
# can't read, so a proper logo is usually an improvement. Turn off to serve the
# provider's art untouched. Logos come from TMDB/Metahub/TVDB as usual — neither
# anime provider ships them — so this needs a tmdb_id or imdb_id on the request.
ANIME_COMPOSITE_LOGO  = _flag(_env("ANIME_COMPOSITE_LOGO", "true", group='Anime sources', kind='bool', label='Composite logo on anime art', help="Overlay a TMDB/Metahub/TVDB title logo onto anime cover art (needs tmdb_id, imdb_id, or anime ID mapping). Off serves the provider's cover art untouched."), True)
# Fill in the tmdb_id / imdb_id an anime request did not bring, from the
# community Kitsu/AniList -> TMDB/IMDb mapping (Fribb's anime-lists, the same
# data AIOMetadata resolves its placeholders from).  The art and metadata spine
# stay the anime provider's; the mapped ids only unlock what those providers
# cannot supply — TMDB's logos, the landscape backdrop, and the IMDb/TMDB-keyed
# enrichment.  This is what makes a client that can only send "{id}" for an
# anime title (Nuvio's own pattern resolver: "kitsu:7442" and nothing else)
# render the same poster as one that goes through AIOMetadata.
ANIME_ID_MAP_ENABLED = _flag(_env("ANIME_ID_MAP_ENABLED", "true", group='Anime sources', kind='bool', label='Anime id mapping', help="Resolve missing TMDB/IMDb IDs via the daily Kitsu/AniList mapping table so kitsu:/anilist: requests get TMDB logos, backdrops and IMDb ratings (provider art kept), and mal_id / mal: requests can render."), True)
ANIME_ID_MAP_URL     = _env('ANIME_ID_MAP_URL', "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json", group='Anime sources', show_if=('ANIME_ID_MAP_ENABLED', 'true'), kind='url', label='Anime id mapping source', help="Where the mapping list is downloaded from. Must be Fribb's anime-list-full.json format.", advanced=True).strip()
ANIME_ID_MAP_PATH    = "/app/cache/anime_ids.db"
ANIME_ID_MAP_REFRESH_HOURS = 24
# A later season's own art in landscape, rather than the show's one backdrop
# every season shares on TMDB (anime_season.py).
ANIME_SEASON_ART     = _flag(_env("ANIME_SEASON_ART", "true", group='Anime sources', show_if=('ANIME_ID_MAP_ENABLED', 'true'), kind='bool', label='Season art for later seasons', help="Use per-season art in landscape for later anime seasons/cours (clean Kitsu cover via ID mapping, else TMDB first-episode still) instead of the show backdrop. Leaves Season 1 and Artwork overrides unchanged."), True)
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
JIKAN_API_URL         = _env('JIKAN_API_URL', "https://jikan.slokker.cc/v4", group='Anime sources', kind='url', label='Jikan API URL', help="Jikan v4 URL (no key) for per-season MAL scores on later anime seasons (MDBList only has S1). Defaults to a community mirror (api.jikan.moe shut down Oct 2026); blank keeps S1's score.", advanced=True).strip().rstrip("/")

# Ordered list of all configured server-side MDBList keys (primary first).
# Used by the key-rotation logic in main.py to fall back when a key is exhausted.
SERVER_MDBLIST_KEYS: list[str] = [k for k in [SERVER_MDBLIST_KEY, SERVER_MDBLIST_KEY_2] if k]

LOG_VIEWER_MAX_MB     = max(0, int(_env('LOG_VIEWER_MAX_MB', "20", group='Caching', kind='int', label='Dashboard log size (MB)', help="Disk cap for the dashboard's searchable Logs view in the cache volume (~100k lines per 20 MB; drops oldest when full). 0 disables Logs (docker logs unaffected).", min=0, max=1000, advanced=True) or "0"))

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
_CDN_CACHE_TTL_RAW    = _env('CDN_CACHE_TTL', "auto", group='Access & serving', kind='text', label='CDN cache TTL', help="Sets Cache-Control: public, max-age=N on posters, never exceeding the composite's remaining life. auto advertises full remaining TTL (1 day for trending, composite TTL for settled); a number caps it; 0 omits the header.", placeholder='0, 3600 or auto').strip().lower()
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
IMAGE_FORMAT          = _env('IMAGE_FORMAT', "webp", group='Output', kind='choice', label='Image format', help='Output format for composited posters. WebP is smaller at the same quality; JPEG encodes about 10x faster (~2 ms vs ~21 ms per poster) for files about 40% larger at q85, which helps a render-bound instance. Changing it re-renders every cached poster.', choices=('webp', 'jpeg')).lower()
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
# libwebp effort (0-6). Lower encodes faster at the same quality for slightly
# larger files; on a 500x750 poster 2 takes ~21 ms, 0 ~13 ms (+16% bytes).
# Not part of the render cache key: cached posters stay valid when it changes.
WEBP_METHOD           = max(0, min(6, int(_env('WEBP_METHOD', "2", group='Output', kind='int', label='WebP encoder effort', help='libwebp effort (0-6), used when the image format is webp. Lower encodes faster for slightly larger files at the same quality: 0 is ~40% faster than the default 2 for ~16% larger posters. Above 2 is slower for little gain. Changing it keeps cached posters.', min=0, max=6))))

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
                             group='Output', kind='text', label='Default logo language', help='Default ISO language/locale for logos and posters. Regional locales (fr-fr, es-es, es-mx, pt-br) match that region only and fall back to English, not the bare language. Legacy env alias: TMDB_LANGUAGE.', placeholder='en')

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
DAYS_CONSIDERED_NEW          = 14
NEW_CACHE_DURATION           = 1
OLD_CACHE_DURATION           = 14
TRENDING_CACHE_DURATION      = 1
TRENDING_FETCH_TIME          = _env('TRENDING_FETCH_TIME', "", group='Trending', kind='text', label='Trending fetch time', help='Daily local time (e.g. 04:00) to refresh trending lists so ranked posters expire and update together. Blank refreshes every 24h from the last run.', placeholder='04:00').strip()
TRENDING_FETCH_TIMEZONE      = _env('TRENDING_FETCH_TIMEZONE', "UTC", group='Trending', kind='text', label='Trending fetch timezone', help='IANA timezone for the fetch time, e.g. America/New_York.', placeholder='UTC').strip()
TRENDING_FETCH_COUNT         = int(_env('TRENDING_FETCH_COUNT', "40", group='Trending', kind='int', label='Trending count', help='Ranks 1 to this number get the Trending sash.', min=1, max=500))
TRENDING_BROAD_FETCH_COUNT   = int(_env('TRENDING_BROAD_FETCH_COUNT', "100", group='Trending', kind='int', label='Broad trending count', help='Lower-ranked trending titles, from the trending count up to this rank, qualify for the lower-priority Trending (Broad) sash.', min=1, max=1000))

# Blockbuster sash: a film among the top-grossing of its release year, by
# TMDB's worldwide revenue (see box_office.py).  Ranking within the year rather
# than against a flat figure keeps Jaws in and a mid-table 2020s sequel out; the
# floor stops a thinly-recorded early year from crowning a $2M film.
BLOCKBUSTER_TOP_N            = int(_env('BLOCKBUSTER_TOP_N', "10", group='Sashes', kind='int', label='Blockbuster: top N of its year', help="A film gets the Blockbuster sash when it is among this many highest-grossing films of its release year (TMDB worldwide revenue).", min=1, max=20))
BLOCKBUSTER_MIN_REVENUE      = int(_env('BLOCKBUSTER_MIN_REVENUE', "100000000", group='Sashes', kind='int', label='Blockbuster: minimum gross (USD)', help="Minimum inflation-adjusted gross in today's USD (e.g. $100M ≈ $17M in 1975) so sparse early TMDB data doesn't award small films. 0 disables.", min=0, max=10000000000))

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
TRENDING_SOURCE_MOVIE        = _env('TRENDING_SOURCE_MOVIE', "", group='Trending', kind='url', label='Movie trending source', help="MDBList page or TMDB-shaped JSON endpoint replacing TMDB's movie trending list. Blank uses TMDB.", placeholder='https://mdblist.com/lists/snoak/trending-movies').strip()
TRENDING_SOURCE_TV           = _env('TRENDING_SOURCE_TV', "", group='Trending', kind='url', label='TV trending source', help="MDBList page or TMDB-shaped JSON endpoint replacing TMDB's TV trending list (for sashes and cache warming). Blank uses TMDB.", placeholder='https://mdblist.com/lists/snoak/trakt-s-trending-shows').strip()
# Anime ranks on their own lists while the trending catalogs addon is on (and
# leaves the movie and TV lists above): AniList's trending series and films
# unless one of these replaces it.  Same shapes as the sources above; their
# rows are TMDB ids, so a poster is ranked by the TMDB id it carries.
TRENDING_SOURCE_ANIME        = _env('TRENDING_SOURCE_ANIME', "", group='Trending', kind='url', label='Anime trending source', help="MDBList page or TMDB-shaped JSON endpoint replacing AniList's trending anime series (catalog and poster ranks). Blank uses AniList. Requires the trending catalogs addon.", placeholder='https://mdblist.com/lists/<user>/<anime-list>').strip()
TRENDING_SOURCE_ANIME_MOVIE  = _env('TRENDING_SOURCE_ANIME_MOVIE', "", group='Trending', kind='url', label='Anime film trending source', help="MDBList page or TMDB-shaped JSON endpoint replacing AniList's trending anime films (catalog and poster ranks). Blank uses AniList. Requires the trending catalogs addon.", placeholder='https://mdblist.com/lists/<user>/<anime-film-list>').strip()
# A small Stremio addon serving the trending lists behind the sashes as three
# catalogs (movies, series, anime), for metadata addons such as AIOMetadata to
# import.  Row order and the "#N Today" labels then come from the same snapshot,
# so the numbers line up with the row.  Also switches posters requested with an
# AniList id to the AniList trending rank, the ranking the anime catalog uses.
TRENDING_CATALOGS_ENABLED    = _env('TRENDING_CATALOGS_ENABLED', "true", group='Trending', kind='bool', label='Trending catalogs addon', help='Serve Trending Movies, Series, Anime and Anime Movies as a Stremio addon at /trending/manifest.json (or /trending/<access key>/manifest.json) so "#N Today" ranks match catalog order. Anime moves to its own AniList (or custom) lists across all ID types.').strip().lower() == "true"
# Leave out what can't be watched at home yet before ranks are numbered, so a
# trending row (and the ranks on its posters) holds only what can be played.
TRENDING_HIDE_UNRELEASED     = _env('TRENDING_HIDE_UNRELEASED', "false", group='Trending', kind='bool', label='Hide unreleased from trending', help='Exclude titles not yet out at home (unreleased/cinema-only films, unaired series, not-yet-airing AniList anime, anime films without home TMDB dates). Remaining titles re-rank without gaps at the next refresh.').strip().lower() == "true"
# The genre names TRENDING_HIDE_GENRES offers: GENRE_MAP's (below), once each.
_TRENDING_GENRE_CHOICES = (
    "Action", "Adventure", "Animation", "Comedy", "Crime", "Documentary", "Drama",
    "Family", "Fantasy", "History", "Horror", "Kids", "Music", "Mystery", "News",
    "Reality", "Rom-Com", "Romance", "Sci-Fi", "Soap", "Talk", "Thriller", "War", "Western",
)


def _trending_genres(raw: str) -> list[str]:
    """TRENDING_HIDE_GENRES's names, as typed in .env ("horror, Romance") or
    saved by the dashboard; unknown ones dropped."""
    lookup = {g.casefold(): g for g in _TRENDING_GENRE_CHOICES}
    return [lookup[g.strip().casefold()] for g in raw.split(",") if g.strip().casefold() in lookup]


# Genres left off the trending lists for the whole instance, before ranks are
# numbered, so every poster's rank and its catalog row still agree.  Names are
# GENRE_MAP's; TV's merged genres count as both halves (genre_names).
TRENDING_HIDE_GENRES = _trending_genres(_env('TRENDING_HIDE_GENRES', "", group='Trending', kind='multi', label='Hide genres from trending', help="Exclude selected genres from trending lists, sashes and catalogs (re-ranked without gaps at the next refresh). Merged TV genres match both halves (e.g. Sci-Fi & Fantasy counts as Sci-Fi and Fantasy).", choices=_TRENDING_GENRE_CHOICES))
TRENDING_HIDE_MIXED_GENRES = _env('TRENDING_HIDE_MIXED_GENRES', "true", group='Trending', kind='bool', label='Hide titles that are partly a hidden genre', help='On: hide titles with any hidden genre (hiding Romance hides Comedy + Romance). Off: hide only if all genres are hidden. Rom-Com matches Comedy + Romance.').strip().lower() == "true"
# Korean dramas left off the TV trending list for the whole instance: a title
# whose original language is Korean and that TMDB files as Drama or Soap.
TRENDING_HIDE_KDRAMAS = _env('TRENDING_HIDE_KDRAMAS', "false", group='Trending', kind='bool', label='Hide Korean dramas from trending', help="Exclude Korean Drama/Soap series from TV trending lists, sashes and catalogs (keeps Korean variety, reality and animation). Re-ranks without gaps at the next refresh.").strip().lower() == "true"
# Visitors choose their own trending lists (trending_list=, the configurator's
# the trending Configure dialog): each list's source from a fixed menu, and whether titles not
# out at home are left off.  The menu is TMDB's day and week lists, AniList for
# anime, the sources above and TRENDING_SOURCE_CHOICES; nothing else can be
# asked for, so the lists built stay bounded by the menu, not by visitors.
# Visitors who choose alike share one list, built when first asked for.
TRENDING_LIST_CHOICE         = _env('TRENDING_LIST_CHOICE', "true", group='Trending', kind='bool', label='Visitors choose trending lists', help="Let visitors choose their own trending catalogs in the configurator (from TMDB, AniList, public lists, and custom sources), toggle hiding unreleased titles, and pick one list per type for poster ranks. Identical picks share cached lists. Off: everyone uses server lists.").strip().lower() == "true"
# The most catalogs a visitor may pick for one list type.  Each pick is a list
# fetched, kept warm and rendered for, and Streaming Top 10 alone offers
# hundreds, so one install URL can't ask for all of them.
TRENDING_MAX_PICKS           = int(_env('TRENDING_MAX_PICKS', "8", group='Trending', show_if=('TRENDING_LIST_CHOICE', 'true'), kind='int', label='Catalogs per list type', help="Max catalogs a visitor can pick per type (Movies, Series, Anime, Anime Movies), capping background fetches and renders (especially with Streaming Top 10 charts). Excess picks in existing URLs are dropped from the end.", min=1, max=30))
# The public lists the trending Configure dialog offers besides TMDB's and AniList's own (see
# trending_lists): each one is fetched only once a visitor picks it.
TRENDING_LIST_SOURCES        = _env('TRENDING_LIST_SOURCES', "imdb,trakt,simkl,justwatch,rottentomatoes,tvstats,top10,shudder,flixpatrol", group='Trending', show_if=('TRENDING_LIST_CHOICE', 'true'), kind='multi', label='Public lists to offer', help="Extra public lists visitors can pick alongside TMDB and AniList (fetched only when picked). IMDb, Trakt, JustWatch, RT, TV Stats and US Top 10s use snoak's daily MDBList mirrors and SIMKL uses its hourly lists (no key); Shudder uses the server TMDB key; Streaming Top 10 adds FlixPatrol charts across 119 countries (see TRENDING_TOP10_URL).", choices=("imdb", "trakt", "simkl", "justwatch", "rottentomatoes", "tvstats", "top10", "shudder", "flixpatrol"), labels={"imdb": "IMDb", "trakt": "Trakt", "simkl": "SIMKL", "justwatch": "JustWatch", "rottentomatoes": "Rotten Tomatoes", "tvstats": "Television Stats", "top10": "Streaming Top 10s (US)", "shudder": "Shudder (US)", "flixpatrol": "Streaming Top 10 (by country)"})
TRENDING_SOURCE_CHOICES      = _env('TRENDING_SOURCE_CHOICES', "", group='Trending', show_if=('TRENDING_LIST_CHOICE', 'true'), kind='list', label='Extra trending sources', help='Extra selectable lists as type|Name|URL (type: movie, tv, anime, anime_movie; MDBList page or TMDB-shaped JSON), e.g. movie|Trakt Trending|https://mdblist.com/lists/<user>/<list>.', advanced=True)
# Where Streaming Top 10's charts are read: fp-crawler's daily copies of
# FlixPatrol's, the ones AIOMetadata's Top 10 catalogs read.  A mirror of the
# same files (availability.json and <country>.json) can stand in for it.
TRENDING_TOP10_URL           = _env('TRENDING_TOP10_URL', "https://raw.githubusercontent.com/0xConstant1/fp-crawler/main/catalogs", group='Trending', show_if=('TRENDING_LIST_CHOICE', 'true'), label='Streaming Top 10 source', help="Where Streaming Top 10's charts are read: fp-crawler's daily copies of FlixPatrol's, or a mirror of the same files (availability.json and <country>.json).", advanced=True).strip()
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
#   mdblist   the watchlist of the account behind MDBLIST_WATCHLIST_API_KEY,
#             or MDBLIST_API_KEY when that is blank.  MDBList
#             mirrors a linked Trakt watchlist, so this is also the free route
#             for Trakt users (Trakt's own API needs a VIP-gated app key).
#   simkl     the "Plan to Watch" list of a SIMKL account.  Needs a free SIMKL
#             app (SIMKL_CLIENT_ID); the account is linked once through the
#             device/PIN flow, whose link is printed in the log on first run.
#   trakt     TRAKT_USERNAME's public watchlist, read with TRAKT_CLIENT_ID.
#   pmdb      the PublicMetaDB watchlist of the account behind PMDB_API_KEY
#             (or the list PMDB_LIST_ID names).
#   <URL>     any MDBList list page — a shared "to watch" list, for example.
# Unset (the default) disables the feature entirely: no fetch, no sash.
# -----------------------------------------------------------------------
APP_VERSION                  = "1.2.0"
WATCHLIST_SOURCE             = _env('WATCHLIST_SOURCE', "", group='Watchlist', kind='text', label='Watchlist source', help="Self-hosted only: marks watchlist titles with a sash. Accepts mdblist (uses MDBLIST_WATCHLIST_API_KEY or MDBLIST_API_KEY; also mirrors Trakt for free), simkl, trakt, pmdb, or any MDBList list URL. Blank disables.", placeholder='mdblist, simkl, trakt, pmdb or a list URL').strip()
# How often the source is re-checked.  Every cycle is one cheap call (MDBList:
# one page per 500 items; SIMKL: /sync/activities, the list itself only when
# it changed; Trakt: two list calls; PMDB: the list lookup plus one page per
# 500 items), so this is safe well below the default.
WATCHLIST_REFRESH_MINUTES    = max(1, int(_env('WATCHLIST_REFRESH_MINUTES', "30", group='Watchlist', show_if=('WATCHLIST_SOURCE', '*'), kind='int', label='Refresh interval (minutes)', help="Minutes between watchlist checks (lightweight: 1 page/500 titles on MDBList/PMDB, timestamp check on SIMKL, 2 calls on Trakt).", min=1, max=1440)))
# SIMKL: which of the account's lists count as "the watchlist".  Any of
# plantowatch, watching, hold (the last two exist for TV/anime only).
WATCHLIST_SIMKL_STATUSES     = [
    s.strip().lower()
    for s in _env('WATCHLIST_SIMKL_STATUSES', "plantowatch", group='Watchlist', show_if=('WATCHLIST_SOURCE', 'simkl'), kind='list', label='SIMKL statuses', help='SIMKL lists counted as watchlist: plantowatch, watching, hold.', advanced=True, placeholder='plantowatch').split(",")
    if s.strip()
]
# A key used only for WATCHLIST_SOURCE=mdblist, so the watchlist can come from
# a personal MDBList account while the server key (ratings, warming) belongs
# to another.  Blank falls back to MDBLIST_API_KEY.
MDBLIST_WATCHLIST_KEY        = _env('MDBLIST_WATCHLIST_API_KEY', "", group='Watchlist', show_if=('WATCHLIST_SOURCE', 'mdblist'), kind='secret', label='MDBList watchlist key', help="Optional separate MDBList key for reading a personal watchlist without spending its quota on ratings/warming. Blank uses MDBLIST_API_KEY.").strip()
SIMKL_CLIENT_ID              = _env('SIMKL_CLIENT_ID', "", group='Watchlist', show_if=('WATCHLIST_SOURCE', 'simkl'), kind='secret', label='SIMKL client id', help='From simkl.com/settings/developer registered as "TV, devices & command line" (device/PIN flow; needs no secret or redirect URL). Link the account here once saved.').strip()
# Only for a SIMKL app registered as "Server apps & services"; the other two
# app types mint no secret and need none.
SIMKL_CLIENT_SECRET          = _env('SIMKL_CLIENT_SECRET', "", group='Watchlist', show_if=('WATCHLIST_SOURCE', 'simkl'), kind='secret', label='SIMKL client secret', help='Only needed if the SIMKL app uses "Server apps & services" instead of the recommended "TV, devices & command line" type.', advanced=True).strip()
# Skips the device flow entirely when set — for a token obtained elsewhere.
# Never refreshed, so a V2 token here goes stale after 7 days.
SIMKL_ACCESS_TOKEN           = _env('SIMKL_ACCESS_TOKEN', "", group='Watchlist', show_if=('WATCHLIST_SOURCE', 'simkl'), kind='secret', label='SIMKL access token', help='Skips the device flow and uses this token as-is; never refreshed.', advanced=True).strip()
TRAKT_CLIENT_ID              = _env('TRAKT_CLIENT_ID', "", group='Watchlist', show_if=('WATCHLIST_SOURCE', 'trakt'), kind='secret', label='Trakt client id', help='From an existing Trakt API app (creating one needs Trakt VIP).').strip()
TRAKT_USERNAME               = _env('TRAKT_USERNAME', "", group='Watchlist', show_if=('WATCHLIST_SOURCE', 'trakt'), kind='text', label='Trakt username', help='The public profile whose watchlist is read.').strip()
# Optional: reads /sync/watchlist as the token's owner instead of the public
# profile, which is what a private profile needs.
TRAKT_ACCESS_TOKEN           = _env('TRAKT_ACCESS_TOKEN', "", group='Watchlist', show_if=('WATCHLIST_SOURCE', 'trakt'), kind='secret', label='Trakt access token', help="Reads /sync/watchlist as the token's owner instead of the public profile; needed for a private profile.", advanced=True).strip()
PMDB_API_KEY                 = _env('PMDB_API_KEY', "", group='Watchlist', show_if=('WATCHLIST_SOURCE', 'pmdb'), kind='secret', label='PMDB API key', help='A PublicMetaDB API key (pm-...), created under Settings → API on publicmetadb.com. Only read access is used.').strip()
# Blank reads the account's watchlist; an id (lst_...) reads that list instead,
# which can be any list the key can see, including someone else's public one.
PMDB_LIST_ID                 = _env('PMDB_LIST_ID', "", group='Watchlist', show_if=('WATCHLIST_SOURCE', 'pmdb'), kind='text', label='PMDB list id', help="Read this PMDB list (lst_...) instead of the account's watchlist. Any list the key can see, including a public one.", advanced=True, placeholder='lst_...').strip()
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
MDBLIST_MIN_INTERVAL         = max(0.0, float(_env('MDBLIST_MIN_INTERVAL', "0.2", group='Performance', kind='float', label='MDBList request spacing (s)', help='Min seconds between outbound MDBList calls (live + warming) to avoid per-IP burst limits (0.2 = 5/s). 0 disables pacing.', min=0, max=5)))

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
POSTER_RENDER_CONCURRENCY    = max(1, int(_env('POSTER_RENDER_CONCURRENCY', "8", group='Performance', kind='int', label='Render concurrency', help='Max concurrent uncached renders per worker (cache hits bypass this); excess bursts queue instead of exhausting upstream connections.', min=1, max=64)))
# How long a fresh render may wait for one of those slots before it is turned
# away with a 503.  Unbounded, a burst bigger than the host can render queued
# for good: a public instance sat 12k renders deep, rendering posters for
# clients that had given up minutes before while every new one joined the back.
POSTER_RENDER_QUEUE_TIMEOUT  = max(0.0, float(_env('POSTER_RENDER_QUEUE_TIMEOUT', "60", group='Performance', kind='float', label='Render queue timeout (s)', help='Seconds an uncached render waits for a free render slot before the request gets a 503 (Retry-After) instead. 0 waits indefinitely.', min=0, max=600)))

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
IMDB_DATASET_ENABLED         = _env('IMDB_DATASET_ENABLED', "false", group='Ratings', kind='bool', label='IMDb dataset', help="Download IMDb's free daily ratings dataset locally (no key) to serve IMDb scores without MDBList when imdb_rating_source=dataset.").strip().lower() in ("1", "true", "yes")
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
CACHE_WARM_ENABLED           = _env('CACHE_WARM_ENABLED', "false", group='Cache warming', kind='bool', label='Cache warming', help="Pre-warm TMDB and MDBList caches in the background for trending, popular and catalog titles. Enable once API quotas are understood.").strip().lower() == "true"
CACHE_WARM_TMDB_BUDGET       = int(_env('CACHE_WARM_TMDB_BUDGET', "2000", group='Cache warming', show_if=('CACHE_WARM_ENABLED', 'true'), kind='int', label='TMDB budget per cycle', help='Max uncached TMDB API calls per cycle; also caps how many trending/popular/top-rated titles are walked for MDBList bulk rating.', min=0, max=100000))
CACHE_WARM_MDBLIST_BUDGET    = int(_env('CACHE_WARM_MDBLIST_BUDGET', "500", group='Cache warming', show_if=('CACHE_WARM_ENABLED', 'true'), kind='int', label='MDBList budget per cycle', help='Max MDBList requests per cycle (stale ratings batch up to 200 titles per call when bulk is available).', min=0, max=100000))
# MDBList's limit is a per-key daily quota (1000/day free) shared with live
# poster requests, and every response reports what's left. The warmer stops
# spending a key once its remaining daily requests fall to this floor, so a
# cycle can't leave the rest of the day rendering without ratings. 0 disables
# the floor (budget only).
CACHE_WARM_MDBLIST_RESERVE   = max(0, int(_env('CACHE_WARM_MDBLIST_RESERVE', "300", group='Cache warming', show_if=('CACHE_WARM_ENABLED', 'true'), kind='int', label='MDBList daily reserve', help="Stop warming a key once its remaining MDBList daily quota (1,000/day free) drops to this reserve, preserving quota for live renders. 0 disables.", min=0, max=100000)))
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
_cache_warm_at_raw = _env('CACHE_WARM_AT_HOUR', "", group='Cache warming', show_if=('CACHE_WARM_ENABLED', 'true'), kind='text', label='Warm at local hour', help="Optional local time (e.g. 4 or 4:30, container TZ/UTC) to run steady-state warm cycles off-peak instead of on an interval. First cycle still runs after boot.", advanced=True, placeholder='4:30').strip()
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
CACHE_WARM_QUALITY_ENABLED   = _env('CACHE_WARM_QUALITY_ENABLED', "false", group='Cache warming', show_if=('CACHE_WARM_ENABLED', 'true'), kind='bool', label='Warm quality badges', help="Pre-fetch stream quality badges (resolution, source, HDR) during warming. Only enable against your own AIOStreams/scraper; public scrapers may rate-limit or block your IP.", advanced=True).strip().lower() == "true"

# Optionally pre-warm specific Stremio catalogs in addition to TMDB
# trending/popular — useful when a user has a particular addon catalog
# (e.g. a custom list) that they want fast on first load. Comma-separated
# list of addon manifest URLs (the same install links pasted into Stremio).
# Each catalog the manifest exposes is fetched (with pagination) and its
# items are resolved to TMDB ids and warmed first, ahead of trending/popular,
# within the same TMDB/MDBList budgets above.
CACHE_WARM_CATALOG_URLS = [
    u.strip() for u in _env('CACHE_WARM_CATALOG_URLS', "", group='Cache warming', show_if=('CACHE_WARM_ENABLED', 'true'), kind='list', label='Catalog manifest URLs', help='Stremio addon manifest URLs whose catalogs are warmed first (ahead of trending/popular) within the budgets above.', advanced=True).split(",") if u.strip()
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
COMPOSITE_CACHE_TTL        = int(_env('COMPOSITE_CACHE_TTL', "604800", group='Caching', kind='int', label='Composite cache TTL (s)', help='Seconds to keep a rendered poster before re-rendering (604800 = 7 days).', min=60, max=31536000))   # 7 days
# +/- half this many seconds of deterministic per-key jitter applied to
# COMPOSITE_CACHE_TTL, so a large batch of composites rendered around the
# same time don't all expire (and get re-rendered) at once. Default 2 days ->
# spread of 6-8 days for the default 7-day TTL. Same cache_key always gets
# the same jitter.
COMPOSITE_CACHE_TTL_JITTER = int(_env('COMPOSITE_CACHE_TTL_JITTER', "172800", group='Caching', kind='int', label='Composite TTL jitter (s)', help='Plus or minus half this many seconds of per-key jitter on the composite TTL, so a batch rendered together does not all expire at once.', min=0, max=31536000, advanced=True))
# How long a provisional render (one missing a piece: quality still being
# fetched, an OCR scan queued, a rating source cooling down) is kept.  Not
# keeping them at all turned a long upstream outage into a full render on
# every view, just when the instance was already degraded; a short life still
# lets the finished poster replace it soon.  Never sent with an ETag.
PROVISIONAL_CACHE_TTL      = int(_env('PROVISIONAL_CACHE_TTL', "300", group='Caching', kind='int', label='Provisional render TTL (s)', help='Seconds to cache incomplete renders (pending quality, missing rating, queued OCR) locally and on clients. 0 re-renders until complete.', min=0, max=86400, advanced=True))
# Maximum number of composite cache entries. When exceeded the oldest entries are
# evicted on each insert to keep the table at this size. 0 = no cap (rely on TTL
# alone), which let any client that can request posters fill the disk with
# distinct render settings.
COMPOSITE_MAX_ENTRIES      = int(_env('COMPOSITE_MAX_ENTRIES', "500000", group='Caching', kind='int', label='Composite cache max entries', help='Max cached composites before evicting oldest (~50–150 KB each; 500000 ≈ 50 GB). 0 relies on TTL only (unbounded growth).', min=0, max=10000000))
# The same cap by size.  A count can't bound the disk on its own: a composite
# is ~65 KB at 500 px, ~100 KB landscape and ~570 KB at 2000 px.  Past it the
# oldest composites go until the cache is back under 95% of it.  0 = no size
# cap, only the count above.
COMPOSITE_MAX_GB           = float(_env('COMPOSITE_MAX_GB', "0", group='Caching', kind='float', label='Composite cache max size (GB)', help='Composite cache size cap in GB; evicts oldest down to 95% when exceeded (works alongside the entry cap). 0 disables.', min=0, max=100000))
# The same cap as a share of the cache volume's total size, so an instance on
# a small disk isn't sized by COMPOSITE_MAX_ENTRIES' ~50 GB (a public instance
# filled its volume at ~530k composites that way).  The lower of this and
# COMPOSITE_MAX_GB applies.  0 = no share cap.
COMPOSITE_MAX_DISK_PCT     = float(_env('COMPOSITE_MAX_DISK_PCT', "40", group='Caching', kind='float', label='Composite cache max share of disk (%)', help="Composite cache size cap as a percentage of the cache volume's total size; the lower of this and Composite cache max size applies. 0 disables.", min=0, max=100))
# Below this much free space on the cache volume, source images are no longer
# stored, each new composite takes the space of older ones (so the database
# stops growing), and the oldest composites are evicted and their space handed
# back until there's twice the floor free — so a full disk can't take the
# SQLite cache (and every other write on the volume) down with it.
CACHE_MIN_FREE_GB          = float(_env('CACHE_MIN_FREE_GB', "1", group='Caching', kind='float', label='Minimum free disk (GB)', help='Below this much free space on the cache volume, source images are not stored and the oldest rendered posters are evicted until twice this is free again. 0 disables.', min=0, max=100000))
# Number of fully-rendered composites kept in each worker's in-memory LRU (L1).
# Off by default: an L1 hit only saves a SQLite point read (~0.7 ms cold,
# ~0.04 ms once the OS page cache has it, against ~4 ms for the whole hit),
# while every worker holds its own copy — ~400 KB per entry measured on a live
# instance, so 500 entries cost ~200 MB per worker.  Configurator previews are
# a new composite per change, so they churn it rather than hit it.
COMPOSITE_MEM_ENTRIES      = int(_env('COMPOSITE_MEM_ENTRIES', "0", group='Caching', kind='int', label='In-memory composites', help="Per-worker in-memory LRU for rendered posters (~100–600 KB each). Off (0) by default since SQLite reads take <1 ms.", min=0, max=100000, advanced=True))
# Set to any truthy value (1, true, yes) to skip composite cache reads and writes
# entirely. Every request re-renders from scratch. Useful during development when
# iterating on rendering changes and you don't want stale renders served.
DISABLE_COMPOSITE_CACHE    = _env('DISABLE_COMPOSITE_CACHE', "false", group='Caching', kind='bool', label='Disable composite cache', help='Skip composite cache reads and writes entirely; every request re-renders. For development only.', advanced=True).strip().lower() in ("1", "true", "yes")
# How long a movie whose only past release is theatrical may stay "Cinema"
# before it is assumed to be streaming.  TMDB is often slow to add a digital
# date, or never does, and a film should not wear "Cinema" for years because of
# it.  Studio windows have settled at 17-45 days for most films and ~60 for the
# largest, so most films get CINEMA_ASSUMED_DIGITAL_DAYS.  A film with at least
# CINEMA_POPULAR_VOTES TMDB votes is big enough that TMDB will be kept current,
# and big enough for a long run (Oppenheimer played ~120 days), so it gets
# CINEMA_POPULAR_DIGITAL_DAYS instead.  Neither is consulted while a future
# digital date is published: that date is the answer.  CINEMA_MAX_AGE_YEARS is
# the outer backstop for when the day windows are switched off.
CINEMA_ASSUMED_DIGITAL_DAYS = max(0, int(_env('CINEMA_ASSUMED_DIGITAL_DAYS', "60", group='Rendering', kind='int', label='Assumed digital window (days)', help="Days after a theatrical release with no digital date before assuming Streaming (typical: 17–60; films ≥ CINEMA_POPULAR_VOTES use CINEMA_POPULAR_DIGITAL_DAYS). 0 disables (stays Cinema up to CINEMA_MAX_AGE_YEARS).", min=0, max=365, advanced=True)))
CINEMA_POPULAR_VOTES        = max(0, int(_env('CINEMA_POPULAR_VOTES', "1000", group='Rendering', kind='int', label='Popular film votes', help="TMDB votes needed to use the longer CINEMA_POPULAR_DIGITAL_DAYS window instead of CINEMA_ASSUMED_DIGITAL_DAYS (unknown vote counts use the shorter). 0 disables.", min=0, max=1000000, advanced=True)))
CINEMA_POPULAR_DIGITAL_DAYS = max(0, int(_env('CINEMA_POPULAR_DIGITAL_DAYS', "180", group='Rendering', kind='int', label='Popular film cinema window (days)', help="Assumed Streaming window (days) for popular films (≥ CINEMA_POPULAR_VOTES) with no digital date (covers ~120-day runs). 0 stays Cinema up to CINEMA_MAX_AGE_YEARS.", min=0, max=3650, advanced=True)))
# Outer backstop: a theatrical-only movie older than this many years is
# "Streaming" whatever the windows above say, even with a (stale) future
# digital date published.  Set to 0 to disable the gate entirely.
CINEMA_MAX_AGE_YEARS       = max(0, int(_env('CINEMA_MAX_AGE_YEARS', "3", group='Rendering', kind='int', label='Cinema max age (years)', help='Hard backstop against stale TMDB data: theatrical-only releases older than this many years always count as Streaming. 0 disables.', min=0, max=50, advanced=True)))

def _parse_bool(val: str, default: bool = False) -> bool:
    val = val.strip().lower()
    if not val:
        return default
    return val not in ("0", "false", "no")

# Logo legibility: when a flat logo's average colour is too close to the poster
# background, recolour it (white / black / complementary accent) so it reads.
# Experimental and off by default while it's being tested — it can mis-handle
# some logos.  Set LOGO_CONTRAST_RESCUE=true to enable.
# Lets users pick poster_pick=random: one of the top five posters from their
# source (TMDB, fanart.tv or TVDB), re-rolled whenever the poster re-renders.  Off by
# default: each title can end up with five posters in the disk cache.
RANDOM_POSTERS             = _parse_bool(_env("RANDOM_POSTERS", "false", group='Rendering', kind='bool', label='Allow random posters', help='Let users pick randomly from the top 5 posters (TMDB/Fanart/TVDB), changing on re-render. Stores up to 5 posters per title in disk cache.'), False)
LOGO_CONTRAST_RESCUE       = _parse_bool(_env("LOGO_CONTRAST_RESCUE", "false", group='Rendering', kind='bool', label='Logo contrast rescue', help='Experimental: recolour flat logos (white/black/accent) that blend into the background. Multi-colour and outline logos are untouched.', advanced=True), False)
# Emit per-logo sizing telemetry (source dims, aspect, final dims) at INFO level.
# Off by default — handy when tuning the logo size caps.
DEBUG_LOGO_SIZING          = _parse_bool(_env("DEBUG_LOGO_SIZING", "false", group='Rendering', kind='bool', label='Log logo sizing', help='Emit per-logo sizing telemetry at INFO level.', advanced=True), False)

# Paths other modules used to read from the environment themselves; declared
# here so the dashboard lists them and a saved value applies.
YUNET_MODEL_PATH           = _env("YUNET_MODEL_PATH", "", group='Rendering', kind='text', label='Face model path',
    help='Path to the YuNet face-detection model (blank uses bundled copy). Missing model soft-disables to saliency crop.',
    placeholder='auto', advanced=True).strip()
DISCOVERY_OVERRIDES_PATH   = _env("DISCOVERY_OVERRIDES_PATH", "/app/cache/discovery_overrides.json", group='Rendering', kind='text',
    label='Discovery overrides path', help='JSON file for Studio, Director and Cast sash lists (edited via Sash lists, or manually per discovery_overrides.example.json).',
    advanced=True).strip() or "/app/cache/discovery_overrides.json"
SASH_LABELS_PATH           = _env("SASH_LABELS_PATH", "/app/cache/sash_labels.json", group='Rendering', kind='text',
    label='Sash labels path', help='JSON file for custom per-language sash label overrides ("New Season", "#{rank} Today"), managed via Sash labels.',
    advanced=True).strip() or "/app/cache/sash_labels.json"

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
TEXTLESS_DETECTION_MAX_VOTES = max(0, int(_env('TEXTLESS_DETECTION_MAX_VOTES', "3000", group='Text detection', show_if=('TEXTLESS_TEXT_DETECTION', 'true'), kind='int', label='Foreground scan vote gate', help='Foreground OCR vote ceiling: titles above this render immediately (uncached) and queue for background OCR. Raise for accuracy, lower for faster bursts; changes invalidate cache.', min=0, max=1000000)))
# Instead of serving a detected fake textless poster as-is, swap in a portrait
# crop of the title's language-neutral backdrop with our logo on top.  Only
# when a logo resolves (or the request wants no overlay at all) and the crop
# itself scans clean.  Costs about half a second on that title's first render
# (backdrop download, text-aware crop, one more scan); later renders reuse the
# cached crop and scan result.  On by default; turning it off invalidates
# cached composites.
TEXTLESS_BACKDROP_FALLBACK = _parse_bool(_env("TEXTLESS_BACKDROP_FALLBACK", "true", group='Text detection', show_if=('TEXTLESS_TEXT_DETECTION', 'true'), kind='bool', label='Backdrop for fake textless posters', help='When a "textless" poster has burned-in text, use a runner-up textless poster (if 6+ exist) or a cropped backdrop + logo (~0.5s on first render). Changes invalidate cache.'), True)
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
# dedicated executor.  Capped automatically at EFFECTIVE_CPUS // WORKERS per
# worker so multi-worker deployments don't oversubscribe ONNX threads.
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
    label='Scan top margin', help='Top fraction of poster height skipped during OCR to ignore edge studio/network logos. 0 scans full height.', min=0, max=0.9, advanced=True) or "0.08")))
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
    max(1, EFFECTIVE_CPUS // WORKERS),
    int(_env('TEXTLESS_DETECTION_CONCURRENCY', "1", group='Text detection', show_if=('TEXTLESS_TEXT_DETECTION', 'true'), kind='int', label='OCR sessions', help="Parallel PP-OCR sessions (~50 MB each, capped at per-worker CPU budget). Splits ONNX threads (slower per scan; mainly helps cold-cache sweeps).", min=1, max=16, advanced=True)),
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

# Not a TMDB genre: a title TMDB (or AniList/Kitsu) gives both Comedy and
# Romance also carries this one (with_derived_genres), so the priority order can rank "Rom-Com" above,
# between or below the two.  Of the 300 most-voted films TMDB tags both, about
# 7 in 10 open their Wikipedia article as a romantic comedy; most of the rest
# are teen and sex comedies built round a romance (American Pie, Juno).
ROMCOM_GENRE_ID = 1074935
GENRE_MAP[ROMCOM_GENRE_ID] = "Rom-Com"


def with_derived_genres(genre_ids: "list[int]") -> list[int]:
    """*genre_ids* plus the genres derived from them (Rom-Com)."""
    ids = list(genre_ids or ())
    if 35 in ids and 10749 in ids and ROMCOM_GENRE_ID not in ids:
        ids.append(ROMCOM_GENRE_ID)
    return ids


# TV's merged genres stand for both their halves when a title is matched by
# genre name (the trending catalogs' genre filter).
_GENRE_NAME_EXTRAS = {10759: ("Adventure",), 10765: ("Fantasy",)}


def genre_names(genre_ids: "list[int]") -> set[str]:
    """The genre names *genre_ids* carry, movie and TV ids alike, Rom-Com
    included (derived here when not already there)."""
    names: set[str] = set()
    for gid in with_derived_genres(genre_ids):
        if GENRE_MAP.get(gid):
            names.add(GENRE_MAP[gid])
        names.update(_GENRE_NAME_EXTRAS.get(gid, ()))
    return names


def genre_hidden(genre_ids: "list[int]", hidden: "set[str]", mixed: bool) -> bool:
    """Whether a title with *genre_ids* is one of the *hidden* genres (names):
    any of them when *mixed*, else only when every genre it has is hidden.
    Rom-Com is derived from Comedy + Romance, so it neither keeps a title nor,
    hidden, stands apart from them: hiding it covers both."""
    names = genre_names(genre_ids)
    if mixed:
        return bool(names & hidden)
    base = names - {"Rom-Com"}
    if "Rom-Com" in names and "Rom-Com" in hidden:
        base -= {"Comedy", "Romance"}
    return bool(names) and base <= hidden


def genre_label(genre_ids: "list[int]", priority: "list[int]") -> str:
    """The name of the first genre in *priority* the title carries, or
    "Unknown".  Rom-Com counts only if *genre_ids* already carry it: it is
    derived where the genres come from TMDB or an anime provider (main.py),
    not from IMDb or TVDB, which give Friends and Big Bang Theory Romance."""
    ids = set(genre_ids or ())
    for gid in priority:
        if gid in ids and GENRE_MAP.get(gid):
            return GENRE_MAP[gid]
    return "Unknown"

# The order genres are tried in when a title has several: the first one the
# title carries is its label, tint, fallback background and title font.  Set
# from the admin dashboard (a drag list) or as comma-separated ids; an id left
# out keeps its default place at the end.  TV ids are listed apart from the film
# ids they print as, because TMDB numbers them separately.
_GENRE_LABELS: dict[str, str] = {
    **{str(gid): name for gid, name in GENRE_MAP.items()},
    "10759": "Action & Adventure (TV)",
    "10762": "Kids (TV)",
    "10763": "News (TV)",
    "10764": "Reality (TV)",
    "10765": "Sci-Fi & Fantasy (TV, not split)",
    "10766": "Soap (TV)",
    "10767": "Talk (TV)",
    "10768": "War & Politics (TV)",
    str(ROMCOM_GENRE_ID): "Rom-Com (Comedy + Romance)",
}

# Checked against the genres of TMDB's ~750 most-voted films and ~800 shows.
# Sci-Fi and Fantasy rank above Mystery, which TMDB puts on much of its TV
# (Stranger Things, Dark and The Expanse all printed Mystery).  Fantasy sits
# beside Sci-Fi so a show split out of TV's merged genre keeps a label that
# strong (Buffy, Good Omens printed Comedy).  War beats Action and History
# (Dunkirk, Saving Private Ryan).  Animation is near last: the art already
# shows a title is animated, so Family / Action / Drama say more.
_DEFAULT_GENRE_PRIORITY = (
    27, 53, 878, 10765, 14, 9648, 80, ROMCOM_GENRE_ID, 35, 10749, 10751,
    10752, 10768, 28, 10759, 36, 10402, 37, 99, 18, 12, 16,
    10764, 10762, 10763, 10766, 10767,
)

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
_DEFAULT_ANIME_GENRE_PRIORITY = (
    ROMCOM_GENRE_ID,  # Rom-Com — Romance and Comedy both
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
)


def _genre_order(key: str, default: tuple[int, ...], label: str, help: str) -> list[int]:
    choices = tuple(str(gid) for gid in default)
    raw = _env(key, ",".join(choices), group='Genres', kind='order', label=label,
               help=help, choices=choices, labels=_GENRE_LABELS, advanced=True)
    # Same rules the dashboard applies on save, for a value set in the
    # environment: unknown ids and repeats are dropped, missing ones added.
    seen: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if part in choices and part not in seen:
            seen.append(part)
    return [int(gid) for gid in merge_order(seen, choices)]


GENRE_PRIORITY = _genre_order(
    'GENRE_PRIORITY', _DEFAULT_GENRE_PRIORITY, 'Genre priority',
    "First matching genre sets the poster label, fallback background and title font. "
    "TV merged genres (Action & Adventure, War & Politics) rank separately from film ones; "
    "Sci-Fi & Fantasy splits via keywords (fallback prints Sci-Fi). TV Horror comes from "
    "MDBList/keywords. Rank Rom-Com below Comedy and Romance to disable it.",
)
ANIME_GENRE_PRIORITY = _genre_order(
    'ANIME_GENRE_PRIORITY', _DEFAULT_ANIME_GENRE_PRIORITY, 'Anime genre priority',
    "Same priority order for AniList/Kitsu requests, favouring Action, Adventure and "
    "Fantasy over common Mystery/Supernatural tags, with Animation last as fallback.",
)

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
    "new_release",
    "just_added",
    "new_season",
    "season_finale",
    # Curated taste — common matches, so they sit under the timely tier.
    "studio",
    "director",
    "cast",
    # Static flavour — always true, never urgent.
    "blockbuster",
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
    # Under Airing, which dates a new series' next episode ("Airing Oct 15");
    # Premiere ("Premiered Oct 2") shows when nothing is airing, as when a
    # whole season came out at once.
    "premiere",
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