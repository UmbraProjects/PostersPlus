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

### `JIKAN_API_URL`

The Jikan v4 instance (a MyAnimeList API, no key needed) a later anime season's own MyAnimeList score is read from. MDBList only has one MyAnimeList score per show, its first season's, which is kept when Jikan can't answer or the season isn't scored yet. The public `api.jikan.moe` shut down on 1 October 2026, so the default is a community-run instance; to run your own, see [AIOMetadata's self-hosted Jikan guide](https://github.com/cedya77/aiometadata/blob/dev/docs/self-hosted-jikan.md) and point this at it. Leave it empty to always use the first season's score.

Default: `https://jikan.slokker.cc/v4`

### `CINEMETA_METADATA_CACHE_DURATION`

Days to cache a title's Cinemeta document — the art urls, title, genres, dates and the TMDB id it carries (which is what resolves an IMDb-only request without a TMDB key). Metahub art itself is cached under the normal image cache.

Default: `7`

### `CINEMETA_NEG_CACHE_DURATION`

Days to cache a "no such id on Cinemeta" result. Throttles and network errors are never cached — only definitive misses.

Default: `3`

### `CINEMETA_API_BASE`

Override the Cinemeta endpoint (useful for a proxy or a mirror).

Default: `https://v3-cinemeta.strem.io`

---

## Caching and output

### `CINEMA_ASSUMED_DIGITAL_DAYS`

TMDB is often slow to add a film's digital date, and sometimes never does. A movie whose only known past release is theatrical, with no digital date published, is treated as Streaming rather than Cinema once its theatrical date is older than this many days. This applies with or without a TMDB key: without one, the dates come from Cinemeta (theatrical, disc) and MDBList (digital). Studio windows have settled at 17–45 days for most films and about 60 for the largest releases. Films with at least `CINEMA_POPULAR_VOTES` TMDB votes use `CINEMA_POPULAR_DIGITAL_DAYS` instead. A published digital date, even a future one, always wins over the assumption, and a film assumed to be streaming still has its TMDB dates re-checked daily. Set to 0 to keep Cinema until a date is known (up to `CINEMA_MAX_AGE_YEARS`).

Default: `60`

### `CINEMA_POPULAR_VOTES`

TMDB vote count at which a movie counts as popular: big enough that TMDB will publish its digital date on time, and big enough for a long cinema run. Such films stay Cinema for up to `CINEMA_POPULAR_DIGITAL_DAYS` rather than `CINEMA_ASSUMED_DIGITAL_DAYS`. The vote count is TMDB's, refreshed with the title's metadata (weekly). A movie with no known vote count (a keyless Cinemeta render, for one) uses the shorter window; a film on a trending list without a vote count on its row counts as popular when `TRENDING_HIDE_UNRELEASED` judges it. Set to 0 to treat every movie alike (the shorter window).

Default: `1000`

### `CINEMA_POPULAR_DIGITAL_DAYS`

Like `CINEMA_ASSUMED_DIGITAL_DAYS`, for movies with at least `CINEMA_POPULAR_VOTES` TMDB votes: past this many days in cinemas with no digital date published, they are treated as Streaming. Long enough for the longest modern runs (about 120 days). Set to 0 to keep popular films at Cinema until a date is known (up to `CINEMA_MAX_AGE_YEARS`).

Default: `180`

### `CINEMA_MAX_AGE_YEARS`

Outer backstop: a movie whose only known past release is a theatrical date older than this many years is treated as "Streaming" rather than "Cinema", whatever `CINEMA_ASSUMED_DIGITAL_DAYS` and `CINEMA_POPULAR_DIGITAL_DAYS` say and even if TMDB lists a (stale) future digital date. Set to 0 to disable the gate entirely.

Default: `3`

### `TMDB_IMAGE_CACHE_JITTER_DAYS`

+/- half this many days of per-title jitter applied to the TMDB poster/logo cache durations, so a large batch cached at once (e.g. cache warming) doesn't all expire on the same day. Default: 10.

Default: `10`

### `COMPOSITE_CACHE_TTL_JITTER`

+/- half this many seconds of per-title jitter applied to COMPOSITE_CACHE_TTL, so a large batch of composites rendered around the same time (e.g. cache warming) don't all expire and re-render at once. Default: 172800 (2 days).

### `PROVISIONAL_CACHE_TTL`

How long a *provisional* poster is kept: one rendered with a piece missing — quality badges still being fetched, a rating source rate-limited or down, a text-detection scan still queued. It is served without an ETag and with `Cache-Control: public, max-age` no longer than what is left of this window, so neither the server nor a client holds an incomplete poster for long, while an outage of the thing it waits on costs one render per title and configuration per window rather than one per view. `0` never keeps them: every view renders again until the poster is complete (the behaviour before this setting). Default: `300`.

Default: `172800`

### `TRENDING_SOURCE_CHOICES`

More trending lists visitors may pick in the configurator's Choose Lists, while `TRENDING_LIST_CHOICE` is on. Comma-separated `type|Name|URL` entries, where `type` is `movie`, `tv`, `anime` or `anime_movie` and the URL is an MDBList list page or a TMDB-shaped JSON endpoint, as for `TRENDING_SOURCE_MOVIE`:

```
TRENDING_SOURCE_CHOICES=movie|Trakt Trending|https://mdblist.com/lists/<user>/<movie-list>,tv|Trakt Trending|https://mdblist.com/lists/<user>/<show-list>
```

The name is what visitors see, and its slug (`trakt-trending`) is what their URLs carry, so a list's URL can be changed without breaking them. Each entry is one more list for that type: it is fetched only once someone picks it, refreshed when it is due, and dropped after a week nobody asks for it. A pick that is later removed from here falls back to the instance's own list.

Default: (empty)

### `TRENDING_SOURCE_MAX_ITEMS`

Cap on entries taken from a custom trending source (default 500).

Default: `500`

### `COMPOSITE_MEM_ENTRIES`

Fully-rendered composites kept in each worker's in-memory LRU (L1) cache, served without a SQLite read. Off by default: the SQLite read it saves takes well under a millisecond (the OS page cache, shared by all workers, keeps hot rows in memory anyway), while each entry costs roughly 100-600 KB per worker. Configurator previews produce a new composite for every change, so they churn the cache rather than hit it.

Default: `0`

### `DISABLE_COMPOSITE_CACHE`

Disable composite poster caching entirely. Every request re-renders from scratch. Only use this during development — never in production. Set to `true` to disable; caching is on unless you do.

Default: `false`

### `LOG_VIEWER_MAX_MB`

Disk kept for the admin dashboard's Logs view: a structured copy of the log in `/app/cache/logs`, which the view filters by level, text, module, time, request and title. Keys are redacted the same way the console log redacts them. HTTP lines keep the client's address, as `docker logs` does, until the cap pushes them out. The copy is split across two files and the older one is dropped once they reach this size between them; 20 MB holds roughly 100,000 lines. `0` turns the Logs view off. `docker logs` is unaffected either way.

Default: `20`

### `PUBLIC_URL`

The address clients reach this instance on, e.g. `https://posters.example.com`. The [trending catalogs addon](CONFIGURATION.md#trending-catalogs-addon) puts it in front of every poster link it hands out. Left blank, it is worked out from each request's `Host`, `X-Forwarded-Host` and `X-Forwarded-Proto` headers, which works behind most reverse proxies. The catch is that those headers come from the client: behind a CDN or shared cache that does not key on them, a forged header could point a cached catalog's posters at another host. Setting this removes the guesswork.

Default: blank (derived from the request)

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

The everyday settings (`WATCHLIST_SOURCE`, `SIMKL_CLIENT_ID`, `TRAKT_CLIENT_ID`, `TRAKT_USERNAME`, `PMDB_API_KEY`) are in [CONFIGURATION.md](CONFIGURATION.md#watchlist-marker). These are the rest.

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

### `PMDB_LIST_ID`

Reads this PublicMetaDB list (its id, `lst_...`) instead of the `PMDB_API_KEY` account's watchlist. Any list the key can see works, including someone else's public list. Only used when `WATCHLIST_SOURCE` is `pmdb`.

Default: unset (the account's watchlist)

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

## Genres

A title usually has several genres. The first one it carries, in the order below, is the genre printed on the poster. It also sets the tint and background of the no-art fallback card and the font used for a text title. The easiest way to change an order is to drag the list in the [admin dashboard](README.md#admin-dashboard). In the environment, give comma-separated TMDB genre ids. Unknown or repeated ids are ignored. If you list only a few ids, the rest follow in their default order. If your list is missing just a few ids (an order saved before a genre was added), each missing one goes in front of the id that follows it in the default. Changing an order re-renders cached posters once.

TMDB gives TV shows a merged "Sci-Fi & Fantasy" genre (10765). PostersPlus splits it into Sci-Fi (878) or Fantasy (14) using the show's TMDB keywords. When the keywords tie, it asks Cinemeta for IMDb's genres. Shows that neither source decides keep 10765, which prints as Sci-Fi. "Action & Adventure" (10759) and "War & Politics" (10768) aren't split. They print as Action and War, and are listed separately so they can be ranked on their own.

TMDB has no Horror genre for TV, so American Horror Story printed Fantasy. A show gets Horror (27) when MDBList lists it. That answer comes with the ratings PostersPlus already fetches, so it costs no extra call. Until MDBList has answered for a show, and on servers without an MDBList key, the show's TMDB keywords decide: a keyword with "horror" in it gives Horror. A show with no keywords asks Cinemeta for IMDb's genres, and TVDB (when a TVDB key is set) if Cinemeta has nothing.

Rom-Com (`1074935`) isn't a TMDB genre. Any title TMDB (or AniList/Kitsu) gives both Comedy and Romance also carries it. Genres from Cinemeta or TVDB don't count, because IMDb and TVDB tag plain sitcoms like Friends with Romance. By default it ranks just above Comedy, so The Love Hypothesis and Notting Hill print Rom-Com while Friends, which TMDB tags Comedy only, stays Comedy. To turn it off, rank it below Comedy and Romance.

### `GENRE_PRIORITY`

The order for every title except those requested by anime id. Horror, Thriller, Sci-Fi and Fantasy come first because they say the most about a title. Mystery comes after them because TMDB puts it on so much of its TV. War ranks above Action and History. Drama and Adventure come near the end because so many titles carry them, and Animation is after them because the poster art already shows a title is animated.

Default: `27,53,878,10765,14,9648,80,1074935,35,10749,10751,10752,10768,28,10759,36,10402,37,99,18,12,16,10764,10762,10763,10766,10767`

### `ANIME_GENRE_PRIORITY`

The order for titles requested by an AniList or Kitsu id. Action, Adventure and Fantasy rank higher here than in the main order, because they describe anime better than the Mystery or Supernatural tags the providers add liberally. Animation stays last, as the label when nothing else matches.

Default: `1074935,10749,27,37,99,878,10765,53,12,28,10759,9648,14,35,80,10752,10768,36,10402,18,10762,10751,16`

---

## Rendering, poster selection and text detection

### `DISCOVERY_OVERRIDES_PATH`

JSON file that holds the notable studio, director and cast lists behind
those sashes, so Docker operators can customise them without editing
`discovery.py`. The admin dashboard's Sash lists view writes it; see
`discovery_overrides.example.json` to write it by hand. The default sits
inside the cache volume, so no extra mount is needed. The file is re-read
within a few seconds of changing, no restart needed.

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
