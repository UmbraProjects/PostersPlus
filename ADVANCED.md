# Advanced configuration

Tuning and debugging variables that are **not** in `.env.example`. These all have
working defaults and a running instance needs none of them.

This covers the poster service. The Plex and Jellyfin sync scripts have their own
variables (`PLEX_*`, `JELLYFIN_*`, `POSTERSPLUS_*`) documented in CONFIGURATION.md.

Set them the same way as anything in `.env.example`: as environment variables on
the container, or as lines in your `.env` — or from the admin dashboard, where
each group's *Advanced* fold lists them with this same help text.

> Several of these change how posters are selected or how burned-in text is
> detected. Those results are cached under a signature that includes the setting,
> so changing one invalidates the affected cache and re-renders on next request.

---

## Art sources (TVDB, AniList, Kitsu)

### `TVDB_ARTWORK_CACHE_DURATION`

Days to cache a title's resolved TVDB id and artwork listing.

Default: `14`

### `TVDB_NEG_CACHE_DURATION`

Days to cache a "no TVDB match / no art" result — kept shorter than `TVDB_ARTWORK_CACHE_DURATION` so newly-added TVDB art is picked up sooner.

Default: `3`

### `TVDB_TYPES_CACHE_DURATION`

Days to cache the TVDB artwork-type catalogue, which rarely changes.

Default: `30`

### `ANILIST_CONCURRENCY`

Caps concurrent calls, per provider — their limits differ by an order of magnitude. AniList advertises 90 req/min per IP but has served a degraded 30 for a long while, so keep it low. Kitsu publishes no hard limit and answers in ~0.2s. Art and metadata are cached after first fetch, so these only matter while the cache is cold.

Default: `3`

### `KITSU_CONCURRENCY`

Same cap for Kitsu, which publishes no hard limit and answers in ~0.2s, so it
tolerates more concurrency than AniList.

Default: `8`

### `ANIME_ID_MAP_URL`

Where the Kitsu/AniList → TMDB/IMDb mapping behind `ANIME_ID_MAP_ENABLED` is
downloaded from, once a day by one worker into `/app/cache/anime_ids.db`. Must
be in the format of Fribb's `anime-list-full.json`; override only to point at a
mirror. A failed download keeps the previous table, and with no table at all an
anime request renders from whatever ids it brought, as it would with the
mapping off. Its state is under `anime_id_map` on `/stats`.

Default: `https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json`

### `ANIME_METADATA_CACHE_DURATION`

Days to cache anime metadata. Shorter than the TVDB artwork window because the community score drifts, and the score is part of this payload.

Default: `7`

### `ANIME_NEG_CACHE_DURATION`

Days to cache a "no such id on this provider" result. Throttles and network errors are never cached — only definitive 404s.

Default: `3`

### `ANILIST_API_URL`

Override the provider endpoints (useful for a proxy or a mirror).

Default: `https://graphql.anilist.co`

### `KITSU_API_BASE`

Override the Kitsu endpoint (useful for a proxy or a mirror).

Default: `https://kitsu.io/api/edge`

### `CINEMETA_METADATA_CACHE_DURATION`

Days to cache a title's Cinemeta document — the art urls, title, genres, dates and the TMDB id it carries (which is what resolves an IMDb-only request without a TMDB key). Metahub art itself is cached under the normal image cache.

Default: `7`

### `CINEMETA_NEG_CACHE_DURATION`

Days to cache a "no such id on Cinemeta" result. Throttles and network errors are never cached — only definitive misses.

Default: `3`

### `CINEMA_ASSUMED_DIGITAL_DAYS`

Without a TMDB key, a movie's release status comes from Cinemeta's theatrical and disc dates and MDBList's digital date. When none of those says the film has left cinemas — no MDBList key, or MDBList has no digital date yet — a theatrical release older than this many days is treated as Streaming rather than Cinema. Studio windows have settled at 17–45 days for most films and about 60 for the largest releases. Never consulted when a digital, physical or TMDB date is known. Set to 0 to keep Cinema until a date is known.

Default: `60`

### `CINEMETA_API_BASE`

Override the Cinemeta endpoint (useful for a proxy or a mirror).

Default: `https://v3-cinemeta.strem.io`

---

## Caching and output

### `CINEMA_MAX_AGE_YEARS`

Movies whose only known release is a theatrical date older than this many years are treated as "Streaming" rather than "Cinema" — guards against stale TMDB data that never got a physical/digital release date added. Set to 0 to disable the gate entirely. Default: 3.

Default: `3`

### `TMDB_IMAGE_CACHE_JITTER_DAYS`

+/- half this many days of per-title jitter applied to the TMDB poster/logo cache durations, so a large batch cached at once (e.g. cache warming) doesn't all expire on the same day. Default: 10.

Default: `10`

### `COMPOSITE_CACHE_TTL_JITTER`

+/- half this many seconds of per-title jitter applied to COMPOSITE_CACHE_TTL, so a large batch of composites rendered around the same time (e.g. cache warming) don't all expire and re-render at once. Default: 172800 (2 days).

Default: `172800`

### `TRENDING_SOURCE_MAX_ITEMS`

Cap on entries taken from a custom trending source (default 500).

Default: `500`

### `COMPOSITE_MEM_ENTRIES`

Fully-rendered composites kept in an in-memory LRU (L1) cache, served without a SQLite read. ~100-300 KB each; 500 entries is roughly 50-150 MB. Set to 0 to disable the in-memory cache entirely. Default: 500.

Default: `500`

### `DISABLE_COMPOSITE_CACHE`

Disable composite poster caching entirely. Every request re-renders from scratch. Only use this during development — never in production. Set to `true` to disable; caching is on unless you do.

Default: `false`

---

## Ratings without MDBList

Every weighted rating source normally comes from MDBList — including the one
labelled "tmdb". Two of them can instead be sourced elsewhere: either
*instead* of MDBList, or only when MDBList comes up empty.

Both settings take the same three values, on the configurator's Weights tab
under **Fallback**:

| Value | Behaviour |
|---|---|
| `mdblist` | Default. The weight comes from MDBList, like every other source. |
| `fallback` | MDBList stays in charge; the local source fills in only when MDBList has no value for that title. |
| `dataset` / `direct` | Always use the local source. MDBList is never asked for this weight, so it works with no MDBList key at all. |

`fallback` is the one to reach for if you *do* use MDBList. It covers two
different gaps with one rule — a hard gap (a rate-limited or exhausted key,
a timeout, every configured key cooling down) and a soft gap (MDBList
answered fine but carried no score for that title, or one that
`RATING_MIN_VOTES` filtered out). Both look the same from the scoring code:
the value is absent, so the local source supplies it.

Note this is a *different layer* from `fallback_to_imdb`. That one is a
scoring fallback — it fires when no weighted source scored at all and reaches
for whatever `imdb` value is present. These fire earlier, when the ratings are
assembled, and are what put a value there for it to find. They compose:
backfill → weighting → `fallback_to_imdb` if the weights still produced
nothing.

> **You must also give the source a weight.** Both `imdb` and `tmdb` ship at
> weight `0` in the stock `MOVIE_WEIGHTS` / `TV_WEIGHTS` (the defaults are
> Letterboxd 0.8 / Rotten Tomatoes 0.2 for movies, Trakt 0.8 / Rotten Tomatoes
> 0.2 for TV — all MDBList-only). Switching the *source* changes where the
> value comes from, not whether it counts. Enable one of these without also
> raising its weight on the configurator's Weights tab and every score still
> reads `N/A`, because the only source with a non-zero weight is one you no
> longer have a key for.

### `imdb_rating_source` (URL param) / `IMDB_DATASET_ENABLED` (env)

Sources the IMDb weight from IMDb's own free, no-key, daily-refreshed
non-commercial dataset (`https://datasets.imdbws.com/title.ratings.tsv.gz`).
Set `IMDB_DATASET_ENABLED=true` on the server, then pick `dataset` (always
use it) or `fallback` (use it only when MDBList has no IMDb score) — default
`mdblist`, unchanged behaviour. Also a dropdown on the Weights tab.

`fallback` needs the same server-side setup as `dataset` — the table has to
exist and be loaded to be worth consulting — but it leaves every MDBList
answer you already get exactly as it was. Until the first download completes
it is simply inert; readiness is part of the composite cache key, so posters
rendered during that window aren't served stale afterwards.

A background task downloads and reloads the full dataset into a local SQLite
table on `IMDB_DATASET_REFRESH_HOURS` (default 24 — IMDb itself only
regenerates the file about once a day, so refreshing faster buys nothing).
Lookups are a single indexed local query, not a network call.
`IMDB_DATASET_MIN_VOTES` (default 10) filters out titles with too few votes to
be meaningful, mirroring `RATING_MIN_VOTES`. `IMDB_DATASET_PATH` (default
`/app/cache/imdb_ratings.db`) is where the table lives — keep it on the same
volume as the rest of the cache so it survives restarts.

Budget for it: the download is ~8.6 MB gzipped, and the resulting SQLite file
is **~84 MB** for ~1.7 million titles. The reload takes a few seconds and runs
off the event loop, so it never blocks requests. Refresh progress and any
download or parse failure are reported under `imdb_dataset` on `/stats`.

IMDb publishes these files for **personal and non-commercial use only** (see
<https://developer.imdb.com/non-commercial-datasets/>). That is a fine fit for
a self-hosted instance; it is not one for a commercial deployment, which is
part of why this is off by default.

With `IMDB_DATASET_ENABLED=false` the configurator disables both non-default
options outright and says why, rather than letting you build a URL that
silently scores `N/A`. A URL imported from an instance that does have it on
is coerced back to `mdblist` for the same reason. The server is forgiving
about it either way — a hand-written `imdb_rating_source=dataset` against a
server without the dataset just falls through to normal MDBList behaviour.

With `WORKERS` greater than 1, only one worker per interval performs the
download; the rest re-read the table it writes. Nothing to configure.

Default: `IMDB_DATASET_ENABLED=false`, `IMDB_DATASET_REFRESH_HOURS=24`,
`IMDB_DATASET_MIN_VOTES=10`, `IMDB_DATASET_PATH=/app/cache/imdb_ratings.db`

### `tmdb_rating_source` (URL param)

Sources the TMDB weight from TMDB's own `vote_average` — already fetched in
the same metadata call used for genre, year, and credits — instead of
MDBList. Pick `direct` (always) or `fallback` (only when MDBList has no TMDB
score) — default `mdblist`, unchanged behaviour. Also a dropdown on the
Weights tab.

No env var or background task needed; the value is already in hand on every
request. That makes `tmdb_rating_source=fallback` close to free — no
download, no table, no readiness window — so it is the cheapest way to keep
scores alive through a rate-limited MDBList key, on an instance that has
opted into nothing else.

`RATING_MIN_VOTES` (default 10) applies here just as it does to every
MDBList-sourced rating, in all three modes, so a title carrying a single
10/10 vote is skipped rather than scored 100.

Between the two — set to `dataset` / `direct`, and given a weight — an
instance can run entirely without an MDBList key: TMDB
metadata (genre, year), TMDB-only sashes (trending, Golden Globe,
studio/director/cast, foreign-language, release-status), and both the TMDB
and IMDb weighted ratings. What you lose without MDBList is the MDBList-only
sashes (festival, cult classic, true story, Metacritic must-see, Oscar
wins/nominations) and every other weighted rating source (Letterboxd, Trakt,
Rotten Tomatoes, Metacritic, Popcornmeter, Roger Ebert).

---

## Watchlist marker

The everyday settings (`WATCHLIST_SOURCE`, `SIMKL_CLIENT_ID`, `TRAKT_CLIENT_ID`, `TRAKT_USERNAME`) are in [CONFIGURATION.md](CONFIGURATION.md#watchlist-marker). These are the rest.

### `WATCHLIST_REFRESH_MINUTES`

How often the source is re-checked. Every cycle is one cheap call — MDBList: one page per 500 titles (counted against the key's daily quota); SIMKL: `/sync/activities`, with the lists only re-read when it reports a change; Trakt: two list calls — so a few minutes is fine if you want the marker to follow the tracker quickly.

Default: `30`

### `WATCHLIST_SIMKL_STATUSES`

Which SIMKL lists count as "the watchlist", comma-separated: any of `plantowatch`, `watching`, `hold` (movies only have `plantowatch`; `completed` and `dropped` are accepted but rarely wanted).

Default: `plantowatch`

### `SIMKL_CLIENT_SECRET`

Only for an AUTH V2 SIMKL app registered as *Server apps & services*, which is the one type that mints a secret and then requires it at the token endpoint. The recommended type, *TV, devices & command line*, needs nothing here — nor do V1 apps or *Mobile, desktop & browser apps*.

Default: unset

### `SIMKL_ACCESS_TOKEN`

Skips the device/PIN flow and uses this token as-is. Never refreshed, so a V2 token goes stale after seven days; mainly for a long-lived AUTH V1 token obtained elsewhere.

Default: unset

### `TRAKT_ACCESS_TOKEN`

Reads `/sync/watchlist` as the token's owner instead of `TRAKT_USERNAME`'s public profile — what a private profile needs. Obtain it through your own Trakt app's device flow; PostersPlus does not run one for Trakt.

Default: unset

---

## Cache warming

### `CACHE_WARM_AT_HOUR`

Optionally align steady-state warm cycles to a fixed local hour of day (e.g. "4" or "4:30"), instead of running exactly CACHE_WARM_INTERVAL_HOURS after the previous cycle. Useful for scheduling the OCR-heavy cycle off-peak. Uses the container's TZ (UTC if unset). Empty = old behaviour.

Default: unset (interval-based scheduling)

### `CACHE_WARM_QUALITY_ENABLED`

Also pre-fetch quality badge data (resolution/source/HDR tokens) for every warmed title via your configured quality source. WARNING: against a public Stremio scraper addon (rather than your own self-hosted instance) this volume of traffic can get your server's IP rate-limited or blocked. Only enable this against your own AIOStreams/scraper instance. Off by default.

Default: `false`

### `CACHE_WARM_CATALOG_URLS`

Comma-separated Stremio addon manifest URLs to pre-warm in addition to TMDB trending/popular/supplemental — useful for a custom catalog you want fast on first load. Each catalog is fetched and warmed first, within the `CACHE_WARM_TMDB_BUDGET` / `CACHE_WARM_MDBLIST_BUDGET` ceilings.

### `CACHE_WARM_CATALOG_MAX_ITEMS`

Max items pre-warmed per catalog (across pagination), so one large catalog can't consume the whole cycle's budget. Default: 100.

Default: `100`

---

## Rendering, poster selection and text detection

### `DISCOVERY_OVERRIDES_PATH`

JSON file that overrides the notable studio, director and cast lists behind
those sashes, so Docker operators can customise them without editing
`discovery.py`. See `discovery_overrides.example.json` for the format. The
default sits inside the cache volume, so no extra mount is needed.

Default: `/app/cache/discovery_overrides.json`


### `LOGO_CONTRAST_RESCUE`

When a flat (single-colour) logo's average colour is too close to the poster background, recolour it (white / black / complementary accent) so it stays legible. Multi-colour and outline logos are never touched. Experimental and off by default while it's tested — it can mis-handle some logos. Set to true to enable. Default: false.

Default: `false`

### `LOGO_STRETCH_FACTOR`

Logo fill-stretch: a slim logo whose clamped size leaves it looking lost is enlarged toward its size cap by up to this factor (one axis only). 1.0 = no enlargement. Only applies when stretching is enabled (see `LOGO_STRETCH_DISABLED`). Default: 1.2.

Default: `1.2`

### `LOGO_STRETCH_DISABLED`

Fill-stretch is OFF by default (every logo kept at its true clamped size). Set to false to enable the stretch described in `LOGO_STRETCH_FACTOR`. Default: true.

Default: `true`

### `DEBUG_LOGO_SIZING`

Emit per-logo sizing telemetry (source dims, aspect, final dims) at INFO level. Handy when tuning logo size; noisy in normal operation. Default: false.

Default: `false`

### `TMDB_POSTER_MIN_VOTES`

Minimum votes preferred when selecting among TMDB textless posters. Candidates cannot be more than MAX_SCORE_DROP below the highest-rated textless option, so several negative votes cannot force selection of very poorly rated art. Defaults: 3 votes and a maximum 1.0-point downgrade.

Default: `3`

### `TMDB_POSTER_MAX_SCORE_DROP`

How far below the highest-rated textless option a candidate may fall before it is
rejected, in TMDB rating points. See `TMDB_POSTER_MIN_VOTES`.

Default: `1.0`

### `RATING_MIN_VOTES`

Ignore provider ratings with fewer votes than this. Roger Ebert is exempt because MDBList reports each review as one rating. Default: 10.

Default: `10`

### `TEXTLESS_TEXT_DETECTION`

Detect burned-in title text on posters TMDB mislabelled as "textless" and skip compositing our own logo so the title isn't doubled. Uses PP-OCRv5 Mobile; its ~4.6MB model is downloaded once into the cache volume. On by default; set to false to opt out. Default: true.

Default: `true`

### `TEXTLESS_FAKE_REPORT`

Append OCR-rejected TMDB posters to a deduplicated human-review file at /app/cache/fake_textless_posters.txt. Default: true.

Default: `true`

### `TEXTLESS_FAKE_REPORT_PATH`

Optional custom path inside the container. Keep it under /app/cache to retain the report in the existing cache volume.

Default: `/app/cache/fake_textless_posters.txt`

### `PPOCR_BOX_THRESHOLD`

Minimum PP-OCR text-box confidence. Higher is stricter. Default: 0.70.

Default: `0.70`

### `PPOCR_WIDE_BOX_THRESHOLD`

Lower-confidence fallback for wide, title-shaped regions. A box qualifies when it meets `PPOCR_WIDE_MIN_ASPECT`, `PPOCR_WIDE_MIN_AREA` and `PPOCR_WIDE_MIN_Y`. This catches stylised titles that PP-OCR scores around 0.30-0.69 without accepting the compact shapes in the Alien artwork.

Default: `0.30`

### `PPOCR_WIDE_MIN_ASPECT`

Minimum width-to-height ratio for the wide-box fallback. See
`PPOCR_WIDE_BOX_THRESHOLD`.

Default: `3.0`

### `PPOCR_WIDE_MIN_AREA`

Minimum share of the poster's area a box must cover for the wide-box fallback.
See `PPOCR_WIDE_BOX_THRESHOLD`.

Default: `0.01`

### `PPOCR_WIDE_MIN_Y`

Minimum vertical centre for the poster-only geometric fallback used when OCR cannot read a centred, title-shaped box. Backdrop scans require title confirmation.

Default: `0.55`

### `TEXTLESS_DETECTION_CONCURRENCY`

Independent PP-OCR sessions for parallel cold-cache scans. Sessions SPLIT the ONNX thread budget rather than adding to it, so raising this makes each single scan slower: measured on 4 cores, ~88ms at 1 session and ~139ms at 2, against bulk throughput of 8.9 vs 11.0 scans/sec. The background scan queue drains one item at a time, so a second session only earns its ~50MB during a cold-cache sweep of a large library. Default 1. Thread sizing follows the container's real CPU budget (cgroup quota), not the host's core count.

Default: `1`

### `TEXTLESS_SCAN_TOP`

Fraction of poster height skipped from the TOP before counting title text. Titles can sit top/middle/bottom, so this scans almost the whole poster; the small default margin ignores studio/network bugs at the very edge. Raise it if top-edge logos cause false positives; 0 scans the entire poster. Default: 0.08.

Default: `0.08`

### `BAKE_PPOCR_MODEL`

BUILD-TIME ONLY (read by compose at `docker compose build`): bake the ~4.6MB PP-OCRv5 Mobile model into the image. Set false to download it on first use.

Default: `true`

---

## Model files

Overrides for the bundled detection models. Useful for an air-gapped host, a
shared read-only model volume, or testing a different PP-OCR revision — not
needed otherwise.

### `PPOCR_MODEL_PATH`

Where the PP-OCRv5 detection model is read from. Defaults to the copy baked into
the image at `/app/models/ch_PP-OCRv5_det_mobile.onnx`, falling back to
`/app/cache/` when the image was built with `BAKE_PPOCR_MODEL=false`.

### `PPOCR_MODEL_URL`

Where the model is downloaded from when it is not already present.

### `PPOCR_MODEL_SHA256`

Expected checksum of that download. A mismatch fails the load rather than
running an unverified model, so change this together with `PPOCR_MODEL_URL`.

### `PPOCR_SKIP_MODEL_HASH`

Set to `1`/`true`/`yes` to skip checksum verification. Only sensible when you are
deliberately supplying your own model via `PPOCR_MODEL_PATH`.

Default: unset

### `YUNET_MODEL_PATH`

Where the YuNet face-detection model is read from. Face detection soft-disables
if it is missing, falling back to the saliency crop.

Default: `models/face_detection_yunet.onnx` alongside the source
