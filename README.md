# PostersPlus

A self-hosted poster generation service that composites extensive metadata onto movie and TV artwork - ratings, award sashes, quality badges, and title logos - served as ready-to-use WebP or JPEG images. PostersPlus is compatible with AIOMetadata, Bingecat, Plex, Jellyfin, and really any application that can pass IMDb, TMDB, AniList, or Kitsu IDs and type.

Those not self-hosting can [visit the public instance.](https://postersplus.elfhosted.com)

---

## Showcase
<p align="center">
  <img src="Showcase/frosted_notch.jpg?raw=true" width="23%"/>
  <img src="Showcase/clean_sash.png?raw=true" width="23%"/>
  <img src="Showcase/rating_bar.jpg?raw=true" width="23%"/>
  <img src="Showcase/mini_sash.jpg?raw=true" width="23%"/>
  <img src="Showcase/bar_rating_notch.jpg?raw=true" width="23%"/>
  <img src="Showcase/clean_notch.jpg?raw=true" width="23%"/>
  <img src="Showcase/rating_bar_notch.jpg?raw=true" width="23%"/>
  <img src="Showcase/mini_original_art.jpg?raw=true" width="23%"/>
  <img src="Showcase/bar_rating_notch_silver.jpg?raw=true" width="23%"/>
  <img src="Showcase/bar_rating_notch_gold.jpg?raw=true" width="23%"/>
  <img src="Showcase/bar_rating_sash_original_art.jpg?raw=true" width="23%"/>
  <img src="Showcase/mini_lowlogo_cinema.jpg?raw=true" width="23%"/>
</p>
<p align="center">
  Client featured is a slightly modified Stremio Kai by allecsc
</p>
<p align="center">
  <img src="Showcase/frosted_kai_large.png?raw=true" width="92%"/>
</p>

---

## Features

- **Ratings overlay** - weighted composite score from Letterboxd, Trakt, Rotten Tomatoes, IMDb, Metacritic, TMDb, MyAnimeList, AniList, Kitsu, and more. Four display modes (Score Bar, Clean, Minimalist, Bar) with many sub-modes. Minimalist mode includes Year, Rating, Both, and Split layouts with optional centring and independently styled field/rating separators. Three built-in colour palettes plus custom score-to-hex palettes, poster-aware overlays, configurable text and glow colours, and optional glow on high scores.

- **Award sashes** - Oscar Best Picture, Golden Globe (film and TV, five major categories), Emmy Outstanding Series (Drama, Comedy, Limited), festival prizes, notable studios/directors/cast, trending titles, TV lifecycle signals (new season, returning, season finale), premieres, just-added digital movies, cult classics, true stories, and Metacritic Must-See. Priority order is fully configurable and any sash can be disabled. Sashes can also render as a modern filled or frosted notch with independent size, inset, padding, text colour, and artwork-aware tint controls.

- **Quality badges** - seven display modes: Quality Bookmark (a tier-coloured top-left corner fold), Quality Notch (vertical tier-coloured accent pill), Quality + Age Rating (age numeral tinted by 4K/Remux/HDR tier), Badge Row (PNG icons for 4K, 1080p, Remux, Web, DV, HDR10+, HDR10), Combined Text Badge, Age Rating Only, or hidden. A minimum quality threshold (`badge_min_score`) can suppress the badge when stream quality falls below a configurable bar. Quality can come from AIOStreams, a standalone Stremio addon such as Torrentio or Comet, QualiCache's background crawler, or the actual files in Plex and Jellyfin.

- **Title logos** - TMDB, Metahub, or optional TheTVDB logos composited over the artwork with configurable size and position. Language priority can combine the requested locale, original language, English, language-neutral art, and a secondary preferred language. A standalone `/logo` endpoint exposes cached TMDB/Metahub logo selection as a PNG.

- **Anime-native requests** - AniList and Kitsu IDs can supply cover art, title, genres, air dates, lifecycle status, and community score without conversion to IMDb or TMDB. Any accompanying IMDb/TMDB IDs still enrich the poster with logos, MDBList ratings, awards, age ratings, release data, and quality badges.

- **Landscape posters** - `shape=landscape` renders a dedicated 16:9 layout from backdrop artwork, with a unified bottom information band, height-relative typography, optional textless/original art, and configurable age-badge placement.

- **Poster-coloured vignettes** - top and bottom vignettes can sample the nearby artwork and render a one- or two-colour tint with saturation, lightness, blur, and blend controls. Frosted bars and notches can match the colour actually painted by the vignette, while burned-in titles automatically fall back to a clean black band.

- **Art fallback chain** - when a title has no textless poster on TMDB the landscape backdrop is cropped to portrait using face and visual-saliency detection. If no poster exists at all, you'll get either a minimalist gradient background or photorealistic fallback plus your usual ratings and info sash. Replace `static/genre_bg/<style>/<Genre>.png` with your own 500×750 PNG to use custom art.

- **TheTVDB fallback** - optional TheTVDB v4 integration. When a TMDB API key alone can't find a usable logo, backdrop, or (opt-in, text-detection-gated) poster, PostersPlus falls back to TheTVDB before dropping to a text title or genre canvas. Configurable per-asset toggles and logo priority.

- **Web configurator** - redesigned browser UI to tune every parameter and generate a ready-to-paste URL template. Rounded tabbed panels cover Core, Rating, Logo, Sash, Quality, and Weights, with touch-friendly row help, persistent settings, a preset gallery, header-level URL import, IMDb/TMDB artwork/MDBList/SIMKL title links, light/dark mode, and a mobile-optimised expanded preview.

- **Plex and Jellyfin sync** - companion scripts (`plex_sync.py` / `jellyfin_sync.py`) that read your media library, derive quality tokens from each title's actual file metadata, and push PostersPlus-generated posters back as library covers. Includes an `--inspect` mode for auditing token derivation without writing anything.

- **Composite poster cache** - fully rendered posters (WebP by default, JPEG optional) are cached by config hash and served directly on repeat requests — checked first against an in-memory LRU, then SQLite — with configurable TTL (plus jitter to avoid mass-expiry stampedes) and max-entry cap.

- **Cache warming** - optional background task that proactively pre-populates the TMDB and MDBList caches (metadata, images, logos, ratings) for trending, popular, and top-rated/now-playing titles ahead of real requests, so first views render fast instead of hitting upstream APIs cold. Can also pre-warm specific Stremio addon catalogs. Configurable budgets, schedule, and an opt-in quality-badge pre-fetch. Off by default.

- **Custom trending sources** - replace TMDB's global movie and TV rankings with ordered MDBList pages or TMDB-shaped endpoints. The same custom order drives Trending sashes and cache warming, making regional, service-specific, or hand-curated rankings possible.

- **Operator overrides** - drop a `discovery_overrides.json` into the cache volume to replace or merge the built-in notable-studio / director / cast lists without editing source. A huge optional env list to choose your own preferences about how the application runs, rather than having them forced on you.

- **OCR detection** - PP-OCRv5 validates posters marked as textless to avoid double-printing a title logo. Vote-gated foreground scans, a background queue, request coalescing, configurable concurrency, and versioned signatures keep live instances responsive while stale detections refresh safely.

- **Cinema greyscale** - greyscales content still in cinemas, as well as the ability to force the info sash to prioritize cinema for these examples.

- **Original art mode** - if you don't like textless posters with logo overlays, swap back to original art and use the bar or minimalist modes with the award sash for an overlay that still works with almost all posters by staying out of the way.

---

## Self-Hosted Requirements

- Docker
- A free [TMDB API key](https://www.themoviedb.org/settings/api) for posters, logos and metadata.
- A free [MDBList API key](https://mdblist.com/) for ratings and keywords.
- An [AIOMetadata](https://github.com/cedya77/aiometadata) config. Self hosted or public instance are both fine. Plex, Jellyfin or Bingecat don't need this.
- Optionally, a quality source for quality badges (choose one):
  - An [AIOStreams](https://github.com/Viren070/AIOStreams) self hosted instance (set `AIOSTREAMS_URL` + `AIOSTREAMS_AUTH`), **or**
  - Any standalone Stremio stream addon such as [Torrentio](https://torrentio.strem.fun) or [Comet](https://comet.elfhosted.com) (set `QUALITY_SOURCE=scraper` + `SCRAPER_URL` to the addon's base URL, e.g. `https://torrentio.strem.fun/`). Note: Stremthru Torz requires authentication and won't work standalone; use it via AIOStreams instead, **or**
  - A [QualiCache](https://github.com/UmbraProjects/QualiCache) instance (set `QUALITY_SOURCE=qualicache` + `QUALICACHE_URL`). QualiCache talks to the same addons but crawls them in the background, so PostersPlus reads quality from a cache instead of waiting on a scrape. See [Quality via QualiCache](#quality-via-qualicache).

---

## Quick Start

> **HTTPS or AIOMetadata's proxy option is required for production use.**
> If going HTTPS route ensure the access_key env is set to protect your instance.
> Good reverse proxy choices are [Traefik](https://traefik.io/) which has great support from Viren's templates or [Caddy](https://caddyserver.com/) which is very simple.
> If going for AIOMetadata's proxy you don't expose PostersPlus to the internet. Use http://postersplus:8000 in the URL instead of a domain to have them communicate via Docker's internal network. The proxy route is slightly slower but maximizes security.

### Using the pre-built image (recommended)

Pre-built images for `amd64` and `arm64` are published to the GitHub Container Registry on every release.

Create a `compose.yaml` with the following content, substituting your own values:

```yaml
services:
  postersplus:
    image: ghcr.io/umbraprojects/postersplus:dev # main if you want a stable, 1 update/month branch
    ports:
      - "8000:8000"    # change the left side if port 8000 is already in use
    restart: unless-stopped
    volumes:
      - ./postersplus-cache:/app/cache
    environment:
      - TMDB_API_KEY=your_tmdb_key
      - MDBLIST_API_KEY=your_mdblist_key
      - TEXTLESS_TEXT_DETECTION=true # set off for faster renders at the cost of potentially printing double logos
      - ACCESS_KEY=youraccesskey # Highly suggested if exposing to the internet.*
      # See .env.example for all available options
```

Then start it:

```bash
docker compose up -d
```

Once your reverse proxy is set up, open the configurator at your public HTTPS domain to tune your settings and generate a URL template for AIOMetadata. The URL it generates is based on the domain you access it from.

### Building from source

```bash
git clone https://github.com/UmbraProjects/PostersPlus.git
cd PostersPlus
cp .env.example .env   # fill in your keys
docker compose up -d --build
```

---

## Configuration

All configuration is done via environment variables. Copy `.env.example` to `.env` and fill in your values. Every variable is optional - API keys can be omitted from the server and passed per-request as URL parameters instead.

`.env.example` covers the settings most instances actually set. Tuning knobs — OCR thresholds, cache TTLs, logo sizing, anime providers, poster selection — live in [ADVANCED.md](ADVANCED.md), which you should not need to read to run PostersPlus.

| Variable | Default | Description |
|---|---|---|
| `TMDB_API_KEY` | - | TMDB API key for poster/metadata fetching |
| `MDBLIST_API_KEY` | - | MDBList API key for ratings and award data |
| `MDBLIST_API_KEY_2` | - | Optional second MDBList key. Retried in the same request when the primary key is rate-limited |
| `MDBLIST_CONCURRENCY` | `3` | Maximum concurrent outbound MDBList requests per worker |
| `TVDB_API_KEY` | - | Optional TheTVDB v4 API key. When set, TVDB is used as a *fallback* art source (logos, backdrops, optionally posters) for titles where TMDB returns nothing usable — reducing fallbacks to text titles / genre canvas. Leave blank to disable entirely |
| `TVDB_SUBSCRIBER_PIN` | - | Only required for user-supported ("subscriber") TVDB keys; leave blank for company keys |
| `TVDB_USE_LOGOS` | `true` | Use TVDB clearlogos when TMDB + Metahub have none |
| `TVDB_USE_BACKDROPS` | `true` | Use TVDB backgrounds (fanart) when no textless TMDB poster/backdrop exists |
| `TVDB_USE_POSTERS` | `false` | Use TVDB posters as a last resort. Off by default because they often carry burned-in title text; only used when text detection confirms a clean image |
| `TVDB_LOGO_PRIORITY` | `3` | Where a TVDB clearlogo sits in the logo chain: `1` = before TMDB and Metahub, `2` = after TMDB but before Metahub, `3` = last resort (only when both have nothing). TVDB logos are often higher quality, so `1`/`2` improve results but change logos currently sourced from TMDB/Metahub |
| `TVDB_CONCURRENCY` | `3` | Maximum concurrent outbound TVDB requests per worker |
| `TVDB_ARTWORK_CACHE_DURATION` | `14` | Days to cache a title's resolved TVDB id and artwork listing |
| `TVDB_NEG_CACHE_DURATION` | `3` | Days to cache a "no TVDB match / no art" result, so newly-added TVDB art is picked up sooner than a positive match |
| `TVDB_TYPES_CACHE_DURATION` | `30` | Days to cache the TVDB artwork-type catalogue, which rarely changes |
| `ACCESS_KEY` | - | Shared secret for request authentication. Leave blank to allow open access |
| `WORKERS` | `1` | Uvicorn worker processes. One worker avoids duplicate uncached renders, scans, and API work across processes |
| `AIOSTREAMS_URL` | - | Base URL of your AIOStreams instance (used when `QUALITY_SOURCE=aiostreams`) |
| `AIOSTREAMS_AUTH` | - | AIOStreams credentials as Base64 `user:password` |
| `QUALITY_SOURCE` | `aiostreams` | Quality data source: `aiostreams`, `scraper`, or `qualicache` |
| `SCRAPER_URL` | - | Base URL of a Stremio stream addon (e.g. `https://torrentio.strem.fun/`). Only used when `QUALITY_SOURCE=scraper`. Standalone addons like Torrentio and Comet work best; Stremthru Torz requires auth and should be used via AIOStreams instead |
| `QUALICACHE_URL` | - | Base URL of your QualiCache instance (e.g. `http://qualicache:8000`). Only used when `QUALITY_SOURCE=qualicache` |
| `QUALICACHE_API_KEY` | - | Must match QualiCache's own `ACCESS_KEY`. Leave blank if QualiCache is unauthenticated |
| `QUALITY_OLD_CACHE_DURATION` | `90` | Days to cache quality data for titles older than 2 weeks |
| `QUALITY_BG_CONCURRENCY` | `5` | Max concurrent background quality fetches |
| `QUALITY_WAIT_TIMEOUT` | `30` | Maximum seconds to wait when a request enables synchronous quality fetching |
| `CDN_CACHE_TTL` | `0` | Adds `Cache-Control: public, max-age=N` to poster responses, capped at the composite's remaining life. `auto` advertises that remaining life with no fixed ceiling, so a trending poster expires in a day and a settled title lasts the full `COMPOSITE_CACHE_TTL`. Set to `0` to disable |
| `IMAGE_FORMAT` | `webp` | Output format for composited posters: `webp` or `jpeg`. WebP gives smaller files at equivalent quality |
| `JPEG_QUALITY` | `85` | JPEG output quality for composited posters (70–95), used when `IMAGE_FORMAT=jpeg`. Raise to `92` for higher fidelity; lower to reduce file size |
| `WEBP_QUALITY` | `85` | WebP output quality for composited posters (70–95), used when `IMAGE_FORMAT=webp` (the default) |
| `CINEMA_MAX_AGE_YEARS` | `3` | Movies whose only known release is a theatrical date older than this are treated as "Streaming" rather than "Cinema" — guards against stale TMDB data missing a physical/digital date. `0` disables the gate |
| `TRENDING_FETCH_TIME` | - | Local time of day (e.g. `04:00`) to refresh the trending-titles list used by the Trending sashes. Empty = refresh on a rolling 24-hour interval instead of a fixed time |
| `TRENDING_FETCH_TIMEZONE` | `UTC` | Timezone for `TRENDING_FETCH_TIME`, e.g. `America/New_York` |
| `TRENDING_FETCH_COUNT` | `40` | Number of top trending titles that qualify for the **Trending** sash |
| `TRENDING_BROAD_FETCH_COUNT` | `100` | Number of additional lower-ranked trending titles (ranks past `TRENDING_FETCH_COUNT`, up to this count) that qualify for the lower-priority **Trending (Broad)** sash |
| `TRENDING_SOURCE_MOVIE` | - | Optional MDBList page or TMDB-shaped JSON endpoint whose order replaces TMDB's global movie trending list for both sashes and cache warming |
| `TRENDING_SOURCE_TV` | - | Optional MDBList page or TMDB-shaped JSON endpoint whose order replaces TMDB's global TV trending list for both sashes and cache warming |
| `TMDB_IMAGE_CACHE_JITTER_DAYS` | `10` | +/- half this many days of per-title jitter applied to TMDB poster/logo cache durations, so a large batch cached at once doesn't all expire the same day |
| `COMPOSITE_CACHE_TTL` | `604800` | Seconds to keep a rendered poster before re-rendering (default 7 days) |
| `COMPOSITE_CACHE_TTL_JITTER` | `172800` | +/- half this many seconds of per-title jitter applied to `COMPOSITE_CACHE_TTL`, so a large batch of composites rendered together doesn't all expire (and re-render) at once |
| `COMPOSITE_MAX_ENTRIES` | `0` | Cap on composite cache entries (SQLite). `0` = no cap |
| `COMPOSITE_MEM_ENTRIES` | `500` | Fully-rendered composites kept in the in-memory LRU (L1) cache, served without a SQLite read. ~100–300 KB each. `0` disables the in-memory cache |
| `DISABLE_COMPOSITE_CACHE` | - | Set to `true` to skip composite cache reads and writes entirely. Every request re-renders from scratch. For development only |
| `LOGO_CONTRAST_RESCUE` | `false` | Recolour a flat logo (white/black/accent) when it blends into the poster background. Multi-colour/outline logos are never touched. Experimental, off by default while tested; set `true` to enable |
| `LOGO_STRETCH_DISABLED` | `true` | Fill-stretch is off by default; every logo is kept at its true clamped size. Set `false` to enable the stretch below |
| `LOGO_STRETCH_FACTOR` | `1.2` | When stretching is enabled, a slim logo is enlarged toward its size cap by up to this factor (one axis only). `1.0` = no enlargement |
| `DEBUG_LOGO_SIZING` | `false` | Log per-logo sizing telemetry at INFO level. For tuning only |
| `TMDB_POSTER_MIN_VOTES` | `3` | Prefer textless posters with at least this many votes when they remain competitively rated |
| `TMDB_POSTER_MAX_SCORE_DROP` | `1.0` | Maximum rating downgrade allowed when preferring a textless poster that meets the vote minimum |
| `RATING_MIN_VOTES` | `10` | Ignore provider ratings below this vote count. Roger Ebert is exempt |
| `TEXTLESS_TEXT_DETECTION` | `true` | Detect burned-in title text on posters TMDB mislabelled as "textless" and skip our own logo so the title isn't doubled. Set `false` to opt out |
| `TEXTLESS_DETECTION_MAX_VOTES` | `3000` | Foreground OCR vote limit. Higher-vote assets render without waiting, skip composite caching, and enter the idle background scan queue. Raise for foreground accuracy; lower for faster stale-cache bursts |
| `TEXTLESS_FAKE_REPORT` | `true` | Record OCR-rejected TMDB posters in a deduplicated human-review report |
| `TEXTLESS_FAKE_REPORT_PATH` | `/app/cache/fake_textless_posters.txt` | Report location. The default persists in the existing cache volume |
| `PPOCR_BOX_THRESHOLD` | `0.70` | Minimum PP-OCR text-box confidence. Higher is stricter; changing it invalidates cached detections and composites |
| `PPOCR_WIDE_BOX_THRESHOLD` | `0.30` | Lower confidence accepted for wide, title-shaped text regions |
| `PPOCR_WIDE_MIN_ASPECT` | `3.0` | Minimum width-to-height ratio for the lower-confidence title fallback |
| `PPOCR_WIDE_MIN_AREA` | `0.01` | Minimum fraction of image area occupied by a lower-confidence title box |
| `PPOCR_WIDE_MIN_Y` | `0.55` | Minimum vertical centre for the poster-only geometric fallback when OCR cannot read a centred title block |
| `TEXTLESS_DETECTION_CONCURRENCY` | `1` | Independent PP-OCR sessions in a dedicated executor. Sessions split the ONNX thread budget rather than adding to it, so raising this makes each scan slower and only pays off during a cold-cache sweep; each extra session costs roughly 50 MB. Capped at the container's real CPU budget |
| `TEXTLESS_SCAN_TOP` | `0.08` | Fraction of poster height skipped from the top before counting text (covers top/middle/bottom titles; ignores top-edge logos) |
| `BAKE_PPOCR_MODEL` | `true` | Build-time only. Bake the ~4.6MB PP-OCRv5 Mobile model into the image |
| `DEFAULT_LOGO_LANGUAGE` | `en` | ISO language/locale code for title logos and poster language preference. `TMDB_LANGUAGE` is also accepted as a fallback alias. Region-qualified locales (`fr-fr`, `es-es`, `es-mx`, `pt-br`) select artwork tagged for that region only, falling back to English rather than to the bare language. |
| `DISCOVERY_OVERRIDES_PATH` | `/app/cache/discovery_overrides.json` | Optional custom path for discovery list overrides |

> CPU guidance: keep `WORKERS × TEXTLESS_DETECTION_CONCURRENCY` at or below the CPU cores available to the container. Larger values can oversubscribe CPU, duplicate uncached work across workers, and reduce sustained throughput.

> The ~4.6 MB PP-OCRv5 Mobile model is baked into the image by default. Set `BAKE_PPOCR_MODEL=false` to download it into the cache volume on first use.

When OCR rejects a TMDB poster marked as textless, Posters Plus records it in
`/app/cache/fake_textless_posters.txt`. Each image appears once, with direct
TMDB and image links for manual review. The report is advisory only and never
edits TMDB automatically; delete it at any time to start a fresh review list.

---

## Quality via QualiCache

The `aiostreams` and `scraper` backends scrape on the request path: the first
view of a title waits on an addon, and a slow or rate-limited Torrentio/Comet
shows up as posters served without badges.

[QualiCache](https://github.com/UmbraProjects/QualiCache) inverts that. It
crawls Stremio catalogues on its own schedule, queries the same addons in the
background, picks one best release from a known release group, and serves only
what is already in its cache. A read is a single SQLite lookup, so PostersPlus
never blocks on a scrape. Because it exposes a plain HTTP API, one QualiCache
instance can back PostersPlus and any other client at the same time.

```dotenv
QUALITY_SOURCE=qualicache
QUALICACHE_URL=http://qualicache:8000
# Only if QualiCache sets ACCESS_KEY
QUALICACHE_API_KEY=
```

Leave `AIOSTREAMS_URL` and `AIOSTREAMS_AUTH` unset — they're ignored when
`QUALITY_SOURCE` isn't `aiostreams`, and PostersPlus warns at startup if both
are configured. Point QualiCache itself at your addons with its own
`STREMIO_ADDONS` setting; PostersPlus never sees those URLs or credentials.

**Pending results.** A title QualiCache hasn't collected yet answers `pending`
rather than an error. PostersPlus serves the poster immediately without badges
and doesn't cache that composite, so the next request picks the badges up once
QualiCache has them. Crucially, pending doesn't count against the quality
source's failure budget — a cold title never triggers the backoff that a real
outage does. In practice, catalogue crawling means common and recently released
titles are usually warm before anyone asks for them.

QualiCache also ships an AIOStreams-shaped compatibility endpoint, so it works
with `QUALITY_SOURCE=aiostreams` and no PostersPlus changes. Prefer
`QUALITY_SOURCE=qualicache`: it reads QualiCache's tokens directly instead of
round-tripping them through the AIOStreams response shape, and it can tell
"still collecting" apart from "failed", which the compatibility endpoint can't
express.

QualiCache's token vocabulary is wider than PostersPlus's badge set. Tokens with
no badge (`8K`, `1440P`, `720P`, `SD`, `BLURAY`, `WEBRIP`, `HDTV`) are dropped
rather than mapped to an approximate equivalent, so a badge is never shown for
quality the release doesn't actually have.

---

## Plex and Jellyfin Sync

`plex_sync.py` and `jellyfin_sync.py` are companion scripts that read your media library, derive quality tokens from each title's own media-file metadata, and push PostersPlus-generated posters back as library covers. This keeps your Plex or Jellyfin art consistent with the same quality-badge logic used by the Stremio-facing poster endpoint, without relying on AIOStreams or a scraper addon for quality data.

### Requirements

```bash
# Plex
pip install -r requirements-plex.txt

# Jellyfin (httpx only, likely already installed)
pip install -r requirements-jellyfin.txt
```

### Configuration

Set the following environment variables before running, or edit the `_DEFAULT` constants near the top of each script:

**Plex**

| Variable | Description |
|---|---|
| `PLEX_BASE_URL` | Base URL of your Plex server, e.g. `http://192.168.1.50:32400` |
| `PLEX_TOKEN` | Your Plex auth token (sign in at plex.tv → Account → XML → `X-Plex-Token`) |
| `POSTERSPLUS_URL` | Full PostersPlus URL template including your preferred query parameters |

**Jellyfin**

| Variable | Description |
|---|---|
| `JELLYFIN_BASE_URL` | Base URL of your Jellyfin server, e.g. `http://192.168.1.50:8096` |
| `JELLYFIN_API_KEY` | API key from Jellyfin Dashboard → Advanced → API Keys |
| `POSTERSPLUS_URL` | Full PostersPlus URL template including your preferred query parameters |

The `POSTERSPLUS_URL` value should be the full URL template you'd normally give AIOMetadata. Copy it straight from the configurator's output box, replacing the `{tmdb_id}` and `{type}` placeholders. Both scripts fill these in automatically from library metadata, and add `imdb_id` for the items that have one.

### Usage

Run with `--inspect` first. It logs every library title with the quality tokens that would be derived from its media streams, without writing any posters:

```bash
python plex_sync.py --inspect
python jellyfin_sync.py --inspect
```

Once the output looks correct, run without the flag to fetch and push posters:

```bash
python plex_sync.py
python jellyfin_sync.py
```

Both scripts process Movies and TV Shows. TV quality tokens are derived from a representative episode selected by watch progress, air date, and episode count. Titles where no quality can be determined (unmatched files, virtual library entries from stream plugins) produce no quality badge and are skipped without error.

---

## URL Structure

Posters are served at `/poster` with parameters controlling every aspect of rendering:

```
https://yourdomain.com/poster?tmdb_id={tmdb_id}&type={type}
```

`tmdb_id` is the only required identity: it selects the artwork and the metadata. `imdb_id` is optional enrichment — send it if your client has one reliably (the Plex and Jellyfin sync scripts do) and it keys the rating cache by IMDb id, sharing that row with every other client. Don't put it in an AIOMetadata template: TMDB has no IMDb link for some titles, and a required placeholder with no value makes the resolver discard the entire URL, so those titles get no poster at all.

For a title with no IMDb id anywhere, TMDB artwork, logos, MDBList ratings, awards, sashes, genres and release status all work normally. Only the IMDb-keyed extras are unavailable: Metahub logo fallback, digital-release detection, and automatic stream-quality badges (an explicit `quality=` still works, which is why the Plex and Jellyfin sync scripts keep full badges either way).

Append `&debug=1` to any poster URL to receive a JSON response with all computed metadata (score, genre, sash label, quality tokens, award data, matched cast/directors) instead of rendering the image. Useful for diagnosing unexpected sashes or missing ratings.

Append `&nocache=1` (requires `ACCESS_KEY` to be set and valid) to force a fresh render of a single title, bypassing the composite cache read and re-caching the result. Lets you refresh one poster without flushing the whole cache.

### Landscape posters

Pass `shape=landscape` for the dedicated 16:9 renderer:

```
https://yourdomain.com/poster?tmdb_id={tmdb_id}&type={type}&shape=landscape
```

Landscape mode uses backdrop artwork, keeps the top corners clear for client overlays, and combines the genre, year, rating, sash, and age rating into one bottom information band. Typography, spacing, and overlays are sized relative to the canvas height for consistent proportions.

Two optional parameters control the landscape-specific choices:

- `landscape_art=textless|original` selects a language-neutral backdrop with a composited logo (the default) or the highest-ranked language-tagged backdrop with its own title treatment.
- `badge_pos=top_left|top_right|logo` places the age badge in a top corner or alongside the composited logo.

Landscape renders deliberately skip stream-quality fetching because this layout does not display quality tokens.

### Logo endpoint

`/logo` returns the best available title logo as its original PNG, using the same cached TMDB/Metahub selection chain as poster rendering:

```
https://yourdomain.com/logo?tmdb_id={tmdb_id}&type={type}&lang=en
```

`imdb_id` is optional and enables Metahub fallback when TMDB metadata cannot supply one. `access_key` and `tmdb_key` follow the same rules as `/poster`.

### Anime IDs (AniList / Kitsu)

Advanced metadata providers such as AIOMetadata can pass an anime-native id instead of `tmdb_id`/`imdb_id`, in which case the cover art, title, genres, air dates, status and community score all come from that provider:

```
https://yourdomain.com/poster?anilist_id={anilist_id}&type=series
https://yourdomain.com/poster?kitsu_id={kitsu_id}&type=series
```

No id conversion happens in either direction. If your client can't supply one of these ids, don't use these parameters — simpler providers group anime under TV series with `tmdb_id`/`imdb_id` and keep working exactly as before. Both bare (`12345`) and Stremio-prefixed (`kitsu:12345`) forms are accepted. When both params are supplied, AniList wins.

Enable **Anime IDs** in the configurator's Core tab (off by default) and it appends one placeholder:

```
?tmdb_id={tmdb_id}&stremio_id={id}&type={type}
```

`{id}` is AIOMetadata's raw Stremio meta id — `kitsu:7442` for a Kitsu-catalogue anime, `tt0903747` or `tmdb:1396` otherwise. PostersPlus reads the namespace off it and ignores anything that isn't an anime id, so the same URL serves your whole library. When it holds an IMDb id, that is also used as the title's identity, which shares its rating cache row with clients that send `imdb_id` directly.

**This is for AIOMetadata only — leave it off for anything else,** since no other metadata addon exposes anime IDs.

Why `{id}` rather than `{kitsu_id}`: the per-namespace placeholder is empty for every live-action title, and an empty *required* placeholder makes AIOMetadata's resolver abandon the whole URL — so it would have to be the optional `{kitsu_id?}` form. That syntax isn't universally accepted (Bingecat rejects it at config time, and it has been reported failing on AIOMetadata builds that nominally support it). `{id}` is a plain placeholder, present in every AIOMetadata version, and always populated, so it can never null the URL.

`anilist_id=` and `kitsu_id=` are still accepted for URLs generated before this, and both bare (`12345`) and prefixed (`kitsu:12345`) forms work.

What changes on this path:

- **Art** is the provider's single cover image. Burned-in-text scanning and backdrop rescue stay off for these covers, but when the request also carries a TMDB id, PostersPlus fetches its language-aware logo list and composites the best match by default. If the anime provider is unavailable or misses a title, a supplied TMDB id temporarily falls back to normal TMDB art without caching the degraded result. Anime cover art is ~0.72 aspect against the 500×750 canvas, so roughly 8% is cropped from the sides. Kitsu's `original` images are ~920×1270 and downscale cleanly; AniList's are ~460×636 and are upscaled slightly, so **prefer `kitsu_id` when your client has both**.
- **Ratings** include the provider score from the same response as the art, at no extra request. When an IMDb id is also supplied, that score joins the normal MDBList provider set instead of replacing it. Give the `anilist` or `kitsu` source a non-zero weight to use it. Note both score high and compressed (anime clusters ~65–80, and a poor show still scores mid-50s), so blend deliberately rather than matching your Letterboxd weight.
- **Quality badges** keep working — Torrentio, Comet, AIOStreams, and compatible QualiCache sources accept anime-native stream ids, so the id passes straight through when no IMDb id exists.
- **Sashes and enrichment** use any accompanying IMDb/TMDB ids for awards, trending, age ratings, logos, and release data. Without those ids, provider-only requests are limited to lifecycle status such as airing, ended, or cancelled.

If you only want MyAnimeList *scores* on anime that already has an IMDb id, you don't need any of this — MDBList already returns a `myanimelist` rating, so just give that source a non-zero weight.

### Operator endpoints

These are gated behind `access_key` when one is configured:

- `GET /stats`: cache row counts / sizes plus live runtime state (in-flight renders, background fetches, MDBList key cooldowns). Handy for spotting issues before they surface.
- `GET /debug/fallback-gallery`: a gallery of every genre's no-art fallback card (mascot + genre font), also reachable via the **Preview fallback art** button in the configurator's Logo section.

---

## Award Sashes

Sashes display contextual metadata about a title - awards, festival recognition, notable cast or crew, and more. The first matching sash in the priority list is shown.

| Sash | Triggers on |
|---|---|
| Oscar Winner, Emmy Winner | Oscar Best Picture winner, Emmy Outstanding Drama/Comedy/Limited winner |
| Globe Winner | Golden Globe winner (film drama/comedy, TV drama/comedy/limited) |
| Festival Prize | The top prize by name (Palme d'Or, Golden Lion, Golden Bear, Golden Leopard, Sundance GJ), or "Cannes Winner"-style wording for any other prize at those five festivals |
| Oscar Nominee, Emmy Nominee | Oscar Best Picture nominee, Emmy Outstanding nominee |
| Globe Nominee | Golden Globe nominee (same categories as above) |
| Notable Studio | A24, Neon, Pixar, and other curated studios |
| Notable Director | Curated list of notable directors |
| Notable Cast | Curated list of notable cast members |
| Trending | Rank 1–`TRENDING_FETCH_COUNT` (default top 40) in TMDB's list or the configured movie/TV trending source |
| New Season | TV show with a recent or upcoming S2+ season premiere |
| Returning | TV show with a recent or upcoming non-premiere episode |
| Premiere | Show initial release within the last two weeks |
| Just Added | Movie with a recent TMDB digital/TV release date |
| Season Finale | Recently completed final TV season |
| Cult Classic | Curated list of cult classics |
| Foreign Language | Non-English language title |
| Newly Streaming | Legacy combined recency signal |
| Metacritic Must-See | High Metacritic score |
| True Story | Based on a true story |
| Short / Mini / Binge | Short film, miniseries, or bingeable series |
| Trending (Broad) | Lower-ranked trending titles, rank `TRENDING_FETCH_COUNT`+1–`TRENDING_BROAD_FETCH_COUNT` (default 41–100) |
| Release Status | Title's current release state: Cinema / Streaming / Physical / Production for movies, Airing / Ended / Cancelled for TV. Lowest default priority; movies require an extra TMDB API call the first time |

Sash priority order is configurable in the web configurator via drag-and-drop. The Primary Client selector sets recommended edge insets: Stremio TV, Nuvio, Plex, and Jellyfin use `0` for both bar and notch; Stremio Desktop/Web use `0.007` for the bar and `0.004` for the notch. Both sliders remain manually adjustable, and loading a preset preserves them. Existing URLs can override the notch with `sash_badge_inset` and the bar with `bar_bottom_inset`. Individual sashes can be disabled entirely with the ✕ button - disabled sashes are serialised as `-slot_name` in the URL (e.g. `&sash_priority=wins,cast,-trending`).

`sash_priority` also accepts a shorter *diff* form, written against the default order and marked by a leading `default` token: `-slot` removes a sash and `slot@N` moves one to position N (0-based). `&sash_priority=default,-cult,festival@0` promotes the festival sash and drops the cult one, in place of naming all thirty slots. The configurator emits whichever form is shorter, and the full list keeps working exactly as before - including an all-exclusions value, which still means every sash off rather than the default order minus those.

In Notch mode, the label is sized from the notch height, so `sash_badge_size_h` (Height) scales the text along with the badge. To tighten the empty space above and below the label *without* resizing it, use `sash_badge_pad` (Padding, default `1.0`, range `0.5`–`1.5`) — it trims only the vertical padding and leaves both the font and the badge width untouched. `sash_badge_inset` is a different control again: it shifts the whole notch up or down rather than reshaping it. Padding stops shrinking once the label's line height is reached, so low values crop the gap, never the glyphs.

### Custom Trending Sources

Set `TRENDING_SOURCE_MOVIE` and/or `TRENDING_SOURCE_TV` to an ordinary MDBList page URL or any endpoint returning TMDB-shaped `{"results": [{"id": 1234}]}` JSON. The source order becomes the ranking for both Trending sashes and cache warming. Movie and TV sources are independent; leave either one empty to keep TMDB's global list for that media type. Entries must contain numeric TMDB ids.

### Customising Directors, Studios, and Cast

**Source editors** can modify the lists directly in `discovery.py`.

**Docker operators** can override them without editing source by placing a JSON file at `/app/cache/discovery_overrides.json` inside the cache volume. See `discovery_overrides.example.json` for the format.

---

## Ratings

Scores from multiple providers are normalised to a 0–100 scale and combined using configurable weights. Default weights use Letterboxd with Trakt fallback for movies, and Trakt (80%) and Rotten Tomatoes (20%) for TV. Weights are fully adjustable in the web configurator.

Weights renormalise over the sources actually present for a title, so a source with no score contributes nothing rather than dragging the average down. That makes the anime-only sources safe to weight: `myanimelist` (via MDBList, for anything with an IMDb id) and `anilist` / `kitsu` (only for titles requested by [anime id](#anime-ids-anilist--kitsu)) are inert on everything else. All three default to a weight of `0`.

---

## Poster Translations

Text rendered onto posters (genre labels and info-sash labels) can be localised. The language follows the request's **poster/logo language** setting.

To add a language, copy `languages/en.json` to `languages/<code>.json` (e.g. `fr.json`) and translate the **values** only; the keys are the canonical English strings and must stay unchanged. Translation is display-only with per-key English fallback: any missing key, malformed file, or language with no JSON falls back to English, so partial translations are safe.

Region-qualified files (`pt-br.json`) are supported and take precedence over the bare language (`pt.json`) for a `pt-br` request. Note that this differs from how region-qualified **artwork** is selected: a region file wins per *table*, not per *key*, so a `pt-br.json` that ships a partial `sashLabels` map does **not** borrow the missing entries from `pt.json` — they fall through to English. Region files should be full copies of `en.json`, not diffs.

> Note: contributed languages must be **Latin-script**. The bundled font has no CJK/Arabic glyphs and no right-to-left shaping, so those scripts will not render correctly.

---

## Caching

PostersPlus uses SQLite (WAL mode) for metadata and rendered-poster caching, plus filesystem caches for TMDB images. The cache volume is mounted at `/app/cache` and persists across container restarts. Expired database rows and image files are pruned automatically; render-affecting server settings and bundled assets are included in the composite cache signature.

| Cache | Default TTL |
|---|---|
| TMDB posters | 60 days |
| TMDB logos | 60 days |
| TMDB metadata | 7 days |
| Ratings (new titles) | 1 day |
| Ratings (older titles) | 14 days |
| Quality badges (new) | 1 day |
| Quality badges (older) | 90 days |
| Composite posters | 7 days |

---

## Cache Warming

An optional background task that proactively populates the TMDB metadata/image/logo cache and the MDBList rating/award cache for a mix of currently-trending, popular, and top-rated/now-playing/on-the-air titles, plus any Stremio addon catalogs you point it at — so the *first* real request for a hot title is already cached instead of hitting upstream APIs cold. Off by default; enable it once you understand your server API keys' rate limits, since it spends its own budget of upstream calls independent of real traffic.

| Variable | Default | Description |
|---|---|---|
| `CACHE_WARM_ENABLED` | `false` | Master switch for the background warm cycle |
| `CACHE_WARM_TMDB_BUDGET` | `2000` | Ceiling on TMDB metadata/image API calls per cycle. Cache hits are free and don't count against it |
| `CACHE_WARM_MDBLIST_BUDGET` | `500` | Ceiling on MDBList rating/award API calls per cycle |
| `CACHE_WARM_INTERVAL_HOURS` | `24` | Hours between the end of one cycle and the start of the next (ignored once `CACHE_WARM_AT_HOUR` is set, after the first cycle) |
| `CACHE_WARM_AT_HOUR` | - | Optional fixed local hour (e.g. `4` or `4:30`) to align steady-state cycles to, instead of running exactly `CACHE_WARM_INTERVAL_HOURS` after the previous cycle. Useful for scheduling the OCR-heavy cycle off-peak. Uses the container's `TZ` (UTC if unset). The very first cycle after startup always runs shortly after boot regardless |
| `CACHE_WARM_QUALITY_ENABLED` | `false` | Also pre-fetch quality-badge data (resolution/source/HDR tokens) for every warmed title via your configured quality source. **Warning:** against a public Stremio scraper addon (rather than your own self-hosted instance) this volume of traffic can get your server's IP rate-limited or blocked — only enable against your own AIOStreams/scraper instance |
| `CACHE_WARM_CATALOG_URLS` | - | Comma-separated Stremio addon manifest URLs (the same install links you'd paste into Stremio). Each catalog the manifest exposes is fetched and warmed first, ahead of trending/popular/supplemental, within the budgets above |
| `CACHE_WARM_CATALOG_MAX_ITEMS` | `100` | Max items pre-warmed per catalog (across pagination), so one large catalog can't consume the whole cycle's budget |

Candidates are split roughly 40% trending / 30% popular / 30% supplemental (top rated, now playing, on the air), deduplicated, and any configured catalog candidates are warmed first. Cycle progress (candidates found, budgets spent) is logged at startup and after each run.

---

## Donate & Discord

If you'd like to support development, I'd appreciate it: https://ko-fi.com/umbraprojects

Join the discord here to request features, follow development or report bugs: https://discord.com/invite/wEgTPNXUMU

---

## License

This project and any associated forks should remain open source under the [GNU Affero General Public License v3.0](LICENSE)