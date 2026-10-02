# Configuration

Everything here can be set from the [admin dashboard](README.md#admin-dashboard) at `/admin`, which shows the same groups, help text and defaults. This page is the reference for doing it with environment variables instead, and for the server-side features that need more explanation than a field's help text.

- [Dashboard or environment](#dashboard-or-environment)
- [Settings reference](#settings-reference)
- [Quality via QualiCache](#quality-via-qualicache)
- [Watchlist marker](#watchlist-marker)
- [Custom trending sources](#custom-trending-sources) and the [trending catalogs addon](#trending-catalogs-addon)
- [Customising directors, studios and cast](#customising-directors-studios-and-cast)
- [Caching](#caching) and [cache warming](#cache-warming)
- [Plex and Jellyfin sync](#plex-and-jellyfin-sync)

Tuning knobs a running instance never needs are in [ADVANCED.md](ADVANCED.md). Poster URL parameters are in [URL_REFERENCE.md](URL_REFERENCE.md).

---

## Dashboard or environment

There are two ways to configure an instance, and they can be mixed:

1. **The admin dashboard** at `/admin`- set one variable, `ADMIN_KEY`, and manage everything else from the browser.
2. **Environment variables**- copy `.env.example` to `.env`, or set them in your compose file.

Every setting is optional: API keys can be omitted from the server and passed per-request as URL parameters instead. When the same setting is in both places, **the dashboard wins** and the field says so.

> **Using a `.env` file?** Compose only reads a `.env` beside `compose.yaml` to fill in `${VAR}` placeholders in the compose file itself. Its entries reach the container only through an `env_file:` block in the compose file (the [quick start](README.md#getting-started) example has one). That also applies to the env-file panel in stack managers like Dockge, Arcane or Komodo. Without `env_file:`, the dashboard reports "no `ADMIN_KEY`" even though the stack's env file has one. On Compose older than 2.24, which doesn't understand `required:`, use `env_file: .env` and make sure the file exists. Another option is to pass each variable through by name, as `- ADMIN_KEY=${ADMIN_KEY}` under `environment:`. A value written directly under `environment:` takes precedence over the same key in `env_file:`.

### Admin dashboard details

- **Overview**- live instance state: rendered-poster and cache counts, DB size, renders in flight against the concurrency cap, each MDBList key's remaining daily quota and cooldown, the watchlist snapshot, cache warming, the IMDb dataset, and which trending sources are in use. The same data as `GET /stats`, laid out. A **Restart server** button lives here too.
- **Settings**- every setting in the [reference](#settings-reference) below, grouped the same way, with its help text, validation (ranges, choices, URLs) and a chip saying where the current value comes from: **saved** (this dashboard), **env** (`.env` / compose) or **default**. Advanced settings sit behind a fold in each group. The **Watchlist** group also holds the [SIMKL account](#watchlist-marker) panel: link the account by code, see whether it is linked, unlink it.
- **Artwork**- choose the art every user gets for a title. Search for it, and the page shows what a default request gets today next to every poster, logo and backdrop TMDB, Fanart (with `FANART_API_KEY`) and TVDB (with `TVDB_API_KEY`) have. There are five things to choose:
  - **Textless poster**: the art our logo goes on, in every mode except Original Art. The burned-in-text scan is skipped for it, so choose art with no title on it. For a title with no textless poster, switch the grid to **Backdrops**, pick one and frame it: drag a poster-shaped window over it and zoom in if you like. *Adjust crop* reframes any textless choice, including your own images.
  - **Original-art poster**, per language: served as-is to users with Original Art on.
  - **Logo**, per language, or *No language* for a neutral one. It applies whatever the user's poster source.
  - **Landscape**: the landscape layout's art, with our logo on top.
  - **Landscape original art**, per language: served as-is to landscape users with Original Art on.

  A poster choice says which poster sources it replaces (TMDB, Fanart, TVDB). A user whose source isn't ticked gets that source's usual pick. A per-language choice follows the user's language order: it is used when the order reaches its language before a language TMDB already has art in. So a German user keeps TMDB's German poster when you've only chosen an English one. Logos and landscape art apply whatever the user's poster source (landscape always draws from TMDB). Anime covers from AniList or Kitsu aren't affected.

  You can also use your own image: paste a link to any image, or upload a file. For [ThePosterDB](https://theposterdb.com), which has no API, copy a poster's download link (`https://theposterdb.com/api/assets/<id>`). The server downloads a link once, when you choose it, checks that it's an image (up to 25 MB, from a public address only), and keeps its own copy in `custom_art/` in the cache volume. A kept image is deleted when no choice uses it any more.

  The rendered previews use the instance defaults unless you give them your own settings: under *Rendered preview*, choose *Use your settings* and paste a poster URL from the configurator's *Copy config*. Only its settings are kept, in your browser. The title, language and any API keys in it are replaced or dropped.

  **Link from the configurator** (a switch in the editor, off by default) adds an icon to the configurator's live preview that opens the selected title in this editor. It's handy on your own instance. On a public one, leave it off: it would only point visitors at the admin login.

  Thumbnails load in your browser straight from the providers, so browsing downloads nothing to the server. A chosen image is downloaded once, when a poster first needs it, and cached like any other art. Choices are stored in the cache database, apply straight away (no restart), and clear that title's rendered posters. Clients that already hold the old poster keep it until their cache revalidates it.

- **Reports**- what users flagged with the configurator's Report button, once `REPORTS_ENABLED` is on: text on a textless poster, the wrong art, the wrong logo, wrong details, or something else with a note. Reports for the same title are grouped, with the poster rendered from the reporter's own settings (their URL is kept with every key removed), a link into the Artwork editor, and Resolve, Dismiss, Delete and Block reporter. `REPORTS_PER_IP` caps how many reports one address files a day. An address that sends more than `REPORTS_PURGE_THRESHOLD` in 7 days, refused ones included, has its unresolved reports deleted and is blocked. A report you resolve stops counting against both limits, so someone whose reports are right isn't held back; dismissing or deleting one gives nothing back, and reopening a resolved one counts it again. Blocked addresses are listed at the bottom, where they can be unblocked. Addresses aren't stored: a reporter is a keyed hash of its address (IPv6 by its /64). Because the limits are per address, they need the real client address: behind a reverse proxy set `FORWARDED_ALLOW_IPS` (see *Security* below). While a request shows the proxy's forwarded address being ignored, the Report button is hidden, reports are refused, and this view says what to set.

- **Logs**- a searchable copy of the server log, so you don't need `docker logs`. Filter by level (or *Problems only*: warnings, errors and HTTP responses of 400 or more), time range, module, app or HTTP lines, and text (words, `-word` to exclude, `"a phrase"` or `/a regex/`). Every line records the HTTP request that logged it: with *Whole requests* on, a matching line brings the rest of its request's lines (dimmed), and a line's *Show this request* lists everything logged while serving it. The title box searches TMDB by name (or takes an IMDb id or `tvdb:<id>`), shows the title's TMDB, IMDb and TVDB ids to copy, and filters to the lines that mention any of them. Ids inside log lines are links that do the same. A TMDB id doesn't say whether it is a movie or a series, so its kind is read from the line or the rest of its request (a cache key, the poster URL's `type=`), and when neither says, you pick from the movie and the series with that id. *Live* fetches new lines every 2 seconds; *Download* saves what's listed as a text file. The copy lives in `/app/cache/logs`, keys redacted as in the console log (and the settings blob in trending addon URLs left out, as it can carry a user's keys), and `LOG_VIEWER_MAX_MB` (Caching, default 20) caps its size. Like `docker logs`, it records client addresses on HTTP lines, kept until the size cap pushes them out.

**What saving does.** Saved values go to `settings.json` in the cache volume; the dashboard never edits your `compose.yaml` or `.env` (it cannot see them). At startup the file is read alongside the environment and takes precedence per key, so a change made here always takes effect, and an env line you already have keeps working until you save over it. *Reset* on a field drops the saved value and the env or default shows through again.

**Restart to apply.** Every module reads its configuration at startup, so saves apply on the next start. The dashboard says which settings are waiting and shows a *Restart required* notice with a **Restart now** button: the server stops gracefully (in-flight renders finish) and the container's restart policy starts it again- `compose.yaml` ships with `restart: unless-stopped`; without a policy the container stays stopped and you `docker compose up -d` it by hand. The page waits for the server to come back and reloads itself.

**Security.** `ADMIN_KEY` is deliberately env-only, and it is not `ACCESS_KEY`: that one travels in every poster URL your clients hold, so it is shared with all of your users, not just you. A key shorter than 12 characters leaves the dashboard disabled, with the reason in the log; without one, `/admin` explains how to turn it on. The API takes the key only in the `X-Admin-Key` header (never a query parameter, which would end up in access logs); wrong keys are slowed down and eight failures from one address lock it out for ten minutes. Behind a reverse proxy (Caddy, Traefik, nginx) that "address" is the proxy's unless you set `FORWARDED_ALLOW_IPS` to the proxy's IP or subnet (e.g. `172.18.0.0/16` for a Docker network, or `*` if nothing but the proxy can reach port 8000): uvicorn only trusts `X-Forwarded-For` from those addresses, and without it every visitor shares one lockout, so anyone can lock you out of the dashboard. Secrets are never sent back to the page- a set key shows as `Set (…ab12)` and can be replaced or cleared. Put the instance behind HTTPS before using this over the internet, as with `ACCESS_KEY`.

## Settings reference

<!-- settings-reference:start (generated by tools/settings_docs.py, do not edit by hand) -->

Grouped as the admin dashboard groups them. Defaults apply when neither the dashboard nor the environment sets a value. Advanced settings (tuning knobs a running instance needs none of) are in [ADVANCED.md](ADVANCED.md) and behind each group's *Advanced* fold in the dashboard.

#### API keys

| Variable | Default | Description |
|---|---|---|
| `TMDB_API_KEY` | - | Fetches posters, logos and metadata. Strongly recommended; without one (and no per-client tmdb_key) titles render from Cinemeta and need an imdb_id on the request. |
| `MDBLIST_API_KEY` | - | Ratings, awards, keywords and age ratings. Without it the score reads N/A and the MDBList-only sashes are unavailable. |
| `MDBLIST_API_KEY_2` | - | Retried in the same request when the primary key is rate-limited; a key that has spent its daily quota stays parked until MDBList's reset. |
| `TVDB_API_KEY` | - | Optional TheTVDB v4 key. When set, TVDB is a fallback art source (logos, backdrops, optionally posters) for titles where TMDB returns nothing usable, reducing fallbacks to text titles and genre canvases. Blank disables it entirely. |
| `TVDB_SUBSCRIBER_PIN` | - | Only for user-supported (subscriber) TVDB keys; leave blank for company keys. |
| `FANART_API_KEY` | - | Optional Fanart project key, needed for the Fanart poster source (see FANART_POSTERS). |

#### Access & serving

| Variable | Default | Description |
|---|---|---|
| `ADMIN_KEY` | - | Enables the [admin dashboard](README.md#admin-dashboard) at `/admin`. At least 12 characters. Env-only: it is the one setting the dashboard cannot manage, because it is what protects the dashboard. |
| `ACCESS_KEY` | - | Shared secret every poster and configurator request must carry as access_key. Leave blank for open access. |
| `CONFIGURATOR_EXTERNAL_AUTH` | `false` | Turn on only if the configurator sits behind its own login (Authelia, Pangolin, an SSO proxy). The configurator then opens without ?access_key= and fills the access key into previews and copied URLs itself, while posters still require it. Anyone who can reach the configurator can read the access key, so it is only as safe as that login, and the login must cover every path the configurator uses: `/`, `/server-caps`, `/search`, `/resolve-imdb`, `/resolve-tmdb` and `/debug/fallback-gallery` (/server-caps hands out the key). No effect without an access key. `true` or `false`. |
| `SHOW_ADMIN_LINK` | `false` | Show an Admin link in the configurator's header, pointing at this dashboard. Off by default so visitors to a public instance aren't invited to try it; the dashboard still needs ADMIN_KEY either way, and the link stays hidden while the dashboard is disabled. `true` or `false`. |
| `MAX_POSTER_RESOLUTION` | `500` | Largest portrait width the resolution URL parameter (500, 780, 1000, 1500, 2000) may request. 500 turns larger sizes off; a request above the limit gets the largest allowed size. Larger posters cost far more CPU and memory to render (2000 px is about 12x a 500), so raise this only on an instance you control. One of `500`, `780`, `1000`, `1500`, `2000`. |
| `PREVIEW_AT_RESOLUTION` | `false` | Render the configurator's live preview at the poster resolution picked there, instead of always at 500 wide. Useful for judging sharpness; each settings change then costs a render at that size (a 2000 px render is about 12x a 500). Only used when `MAX_POSTER_RESOLUTION` is `780` or `1000` or `1500` or `2000`. `true` or `false`. |
| `CDN_CACHE_TTL` | `auto` | Cache-Control: public, max-age=N on poster responses, capped at the composite's remaining life so a cached copy never outlives the trending rank or release status baked into it. auto (the default) advertises that remaining life with no fixed ceiling: a trending poster expires in a day, a settled title lasts the full composite TTL. A number caps it; 0 sends no Cache-Control. |

#### Quality source

| Variable | Default | Description |
|---|---|---|
| `QUALITY_SOURCE` | `aiostreams` | Where stream-quality badges come from. QualiCache never scrapes on the request path; a cold title returns pending instead of blocking. One of `aiostreams`, `scraper`, `qualicache`. |
| `AIOSTREAMS_URL` | - | Base URL of your AIOStreams instance. Used when the quality source is aiostreams. Only used when `QUALITY_SOURCE` is `aiostreams`. |
| `AIOSTREAMS_AUTH` | - | AIOStreams credentials as Base64 user:password. Only used when `QUALITY_SOURCE` is `aiostreams`. |
| `SCRAPER_URL` | - | Base URL of a Stremio stream addon, e.g. https://torrentio.strem.fun/. Only used when the quality source is scraper. Standalone addons like Torrentio and Comet work best; Stremthru Torz requires auth and should be used through AIOStreams instead. Only used when `QUALITY_SOURCE` is `scraper`. |
| `QUALICACHE_URL` | - | Base URL of a QualiCache instance. Only used when the quality source is qualicache. Only used when `QUALITY_SOURCE` is `qualicache`. |
| `QUALICACHE_API_KEY` | - | Must match QualiCache's own ACCESS_KEY when it has one. Only used when `QUALITY_SOURCE` is `qualicache`. |
| `QUALICACHE_MIN_TRUST` | `medium` | Lowest release-group tier to accept from QualiCache. Only used when `QUALITY_SOURCE` is `qualicache`. One of `high`, `medium`, `low`. |

#### Output

| Variable | Default | Description |
|---|---|---|
| `IMAGE_FORMAT` | `webp` | Output format for composited posters. WebP is smaller at the same quality. One of `webp`, `jpeg`. |
| `JPEG_QUALITY` | `85` | JPEG output quality (70-95), used when the image format is jpeg. Raise to 92 for higher fidelity; lower to reduce file size. |
| `WEBP_QUALITY` | `85` | WebP output quality (70-95), used when the image format is webp (the default). Higher is better quality and larger files. |
| `DEFAULT_LOGO_LANGUAGE` | `en` | ISO language or locale code for title logos and poster language preference when a request names none. Region-qualified locales (fr-fr, es-es, es-mx, pt-br) select artwork tagged for that region only, falling back to English rather than to the bare language. TMDB_LANGUAGE is accepted as a legacy alias in the environment. |

#### Trending

| Variable | Default | Description |
|---|---|---|
| `TRENDING_FETCH_TIME` | - | Local time of day (e.g. 04:00) to refresh the trending list used by the Trending sashes. Every poster showing a rank is cached until then, so the ranks all change at once. Blank refreshes 24 hours after the previous refresh instead. |
| `TRENDING_FETCH_TIMEZONE` | `UTC` | IANA timezone for the fetch time, e.g. America/New_York. |
| `TRENDING_FETCH_COUNT` | `40` | Ranks 1 to this number get the Trending sash. |
| `TRENDING_BROAD_FETCH_COUNT` | `100` | Lower-ranked trending titles, from the trending count up to this rank, qualify for the lower-priority Trending (Broad) sash. |
| `TRENDING_SOURCE_MOVIE` | - | An MDBList list page or any TMDB-shaped JSON endpoint whose order replaces TMDB's global movie trending list. Blank keeps TMDB's list. |
| `TRENDING_SOURCE_TV` | - | An MDBList list page or any TMDB-shaped JSON endpoint whose order replaces TMDB's global TV trending list for both sashes and cache warming. Blank keeps TMDB's list. |
| `TRENDING_SOURCE_ANIME` | - | An MDBList list page or any TMDB-shaped JSON endpoint whose order replaces AniList's trending anime series, for the Trending Anime catalog and the rank on every anime series poster. Blank keeps AniList's list. Only used while the trending catalogs addon is on. |
| `TRENDING_SOURCE_ANIME_MOVIE` | - | An MDBList list page or any TMDB-shaped JSON endpoint whose order replaces AniList's trending anime films, for the Trending Anime Movies catalog and the rank on every anime film poster. Blank keeps AniList's list. Only used while the trending catalogs addon is on. |
| `TRENDING_CATALOGS_ENABLED` | `true` | Serve the trending lists behind the Trending sashes as a Stremio addon with Trending Movies, Series, Anime and Anime Movies catalogs, at /trending/manifest.json (/trending/<access key>/manifest.json when an access key is set). Import it into your metadata addon and the "#N Today" labels match the row order. Anime then ranks on its own lists (from AniList, unless an anime trending source is set) and leaves the movie and TV lists, so every anime poster, whatever id it is requested by, carries its rank in the anime catalogs. On by default; turn off to serve no addon. `true` or `false`. |
| `TRENDING_HIDE_UNRELEASED` | `false` | Leave titles that are not out at home yet off the trending lists: films still in cinemas or not released at all, series whose first episode has not aired, anime series AniList lists as not yet airing, and anime films not out at home by their TMDB dates. The remaining titles are ranked 1, 2, 3 without gaps, so the catalogs and the rank on every poster still agree. Takes effect at the next trending refresh. Off by default. `true` or `false`. |

#### Watchlist

| Variable | Default | Description |
|---|---|---|
| `WATCHLIST_SOURCE` | - | Self-hosted only: marks every title in one user's watchlist with a Watchlist sash. mdblist (the watchlist of the MDBList key's account, also the free route for Trakt, which MDBList mirrors), simkl, trakt, pmdb (a PublicMetaDB watchlist), or any MDBList list page URL. Blank disables the feature. |
| `WATCHLIST_REFRESH_MINUTES` | `30` | How often the watchlist source is re-checked. Each check is one cheap call (one MDBList page per 500 titles; SIMKL's activities timestamp, with the list only re-read when it changed; two Trakt calls; one PMDB list lookup plus one page per 500 titles, well inside PMDB's free-tier hourly limit at the default interval). Only used when `WATCHLIST_SOURCE` is set. |
| `SIMKL_CLIENT_ID` | - | From a free app at simkl.com/settings/developer. Register it as "TV, devices & command line": PostersPlus links by code (the device/PIN flow), so that type needs no secret and no redirect URL. The account is then linked once from the admin dashboard's Watchlist group. Only used when `WATCHLIST_SOURCE` is `simkl`. |
| `TRAKT_CLIENT_ID` | - | From an existing Trakt API app (creating one needs Trakt VIP). Only used when `WATCHLIST_SOURCE` is `trakt`. |
| `TRAKT_USERNAME` | - | The public profile whose watchlist is read. Only used when `WATCHLIST_SOURCE` is `trakt`. |
| `PMDB_API_KEY` | - | A PublicMetaDB API key (pm-...), created under Settings → API on publicmetadb.com. Only read access is used. Only used when `WATCHLIST_SOURCE` is `pmdb`. |

#### Poster reports

| Variable | Default | Description |
|---|---|---|
| `REPORTS_ENABLED` | `false` | Add a Report button to the configurator's live preview so users can flag a poster (text on a textless poster, wrong art, wrong logo, wrong details). Reports land in the dashboard's Reports view. Needs the admin dashboard (ADMIN_KEY) to read them. Behind a reverse proxy, set FORWARDED_ALLOW_IPS to the proxy's address: the limits below are per address, and reports stay paused while every visitor arrives as the proxy. `true` or `false`. |
| `REPORTS_PER_IP` | `10` | How many reports one address may file in 24 hours. Further reports are refused with a message saying so. Reports you resolve stop counting, so someone whose reports are right is not held back; dismissed and deleted ones still count. Only used when `REPORTS_ENABLED` is `true`. |
| `REPORTS_PURGE_THRESHOLD` | `50` | An address that sends more than this many reports within 7 days, refused ones included and resolved ones not, has its unresolved reports deleted and is blocked from reporting. Blocked addresses are listed in the Reports view, where they can be unblocked. 0 turns this off. Only used when `REPORTS_ENABLED` is `true`. |

#### Ratings

| Variable | Default | Description |
|---|---|---|
| `IMDB_DATASET_ENABLED` | `false` | Download IMDb's free, no-key, daily-refreshed ratings dataset into a local table so the imdb rating weight can be served without MDBList. Selected per request or instance with imdb_rating_source=dataset. `true` or `false`. |
| `IMDB_DATASET_PATH` | `/app/cache/imdb_ratings.db` | Where the dataset's SQLite table is kept. Only used when `IMDB_DATASET_ENABLED` is `true`. |
| `IMDB_DATASET_REFRESH_HOURS` | `24` | How often the dataset is re-downloaded. Only used when `IMDB_DATASET_ENABLED` is `true`. |
| `IMDB_DATASET_MIN_VOTES` | `10` | Titles with fewer IMDb votes than this are ignored, as the rating minimum votes does for MDBList sources. Only used when `IMDB_DATASET_ENABLED` is `true`. |
| `RATING_MIN_VOTES` | `10` | A rating source with fewer votes than this is ignored for the weighted score. |

#### Caching

| Variable | Default | Description |
|---|---|---|
| `QUALITY_OLD_CACHE_DURATION` | `90` | Stream quality for older titles is stable, so it is cached this long; new titles keep a 1-day window. |
| `COMPOSITE_CACHE_TTL` | `604800` | How long a fully rendered poster is kept before it is re-rendered. Default 604800 (7 days). |
| `COMPOSITE_MAX_ENTRIES` | `500000` | Oldest entries are evicted past this many. A composite is roughly 50-150 KB, so the default 500000 holds about 50 GB. 0 relies on the TTL alone, which lets requests with ever-new settings grow the cache without bound. |

#### Cache warming

| Variable | Default | Description |
|---|---|---|
| `CACHE_WARM_ENABLED` | `false` | Pre-populate the TMDB and MDBList caches for trending, popular and catalog titles in the background. Off by default; enable once the server keys' quotas are understood. `true` or `false`. |
| `CACHE_WARM_TMDB_BUDGET` | `2000` | Ceiling on actual TMDB API calls per warm cycle; cache hits do not count. Only used when `CACHE_WARM_ENABLED` is `true`. |
| `CACHE_WARM_MDBLIST_BUDGET` | `500` | Ceiling on actual MDBList calls per warm cycle. Only used when `CACHE_WARM_ENABLED` is `true`. |
| `CACHE_WARM_MDBLIST_RESERVE` | `300` | MDBList's limit is a per-key daily quota (1,000/day on a free key) shared with live poster requests. The warmer stops spending a key once its remaining daily requests, reported by MDBList on every response, fall to this floor, so a cycle cannot leave the rest of the day without ratings. 0 disables the floor. Only used when `CACHE_WARM_ENABLED` is `true`. |
| `CACHE_WARM_INTERVAL_HOURS` | `24` | Hours between the end of one warm cycle and the start of the next. Ignored after the first cycle once a warm hour is set. Only used when `CACHE_WARM_ENABLED` is `true`. |

#### TVDB fallback art

| Variable | Default | Description |
|---|---|---|
| `TVDB_USE_LOGOS` | `true` | Use TVDB clearlogos when TMDB and Metahub have none. `true` or `false`. |
| `TVDB_USE_BACKDROPS` | `true` | Use TVDB backgrounds when no textless TMDB poster or backdrop exists. `true` or `false`. |
| `TVDB_USE_POSTERS` | `false` | Use TVDB posters as a last resort. To replace a poster with text, only a no-language TVDB poster is used (these are textless), and only when text detection also finds it clean. For a title with no art at all, a TVDB poster in the user's language comes after those. `true` or `false`. |
| `TVDB_POSTER_SOURCE` | `false` | Let users pick TVDB as their poster source: its best no-language poster (TVDB's no-language posters are textless), or under Original Art its best poster in their language. Landscape users can pick TVDB backdrops the same way. TMDB when TVDB has none. Needs the TVDB key. Adds poster downloads, cache and text scans for users who pick it. `true` or `false`. |
| `TVDB_LOGO_PRIORITY` | `3` | Where a TVDB clearlogo sits in the logo chain: 1 before TMDB and Metahub, 2 after TMDB but before Metahub, 3 last resort (only when both have nothing). TVDB logos are often higher quality, so 1 or 2 improve results but change logos currently sourced from TMDB or Metahub. One of `1`, `2`, `3`. |
| `TVDB_CONCURRENCY` | `3` | Maximum concurrent outbound TVDB requests per worker. |

#### Fanart

| Variable | Default | Description |
|---|---|---|
| `FANART_POSTERS` | `false` | Let users pick Fanart as their poster source, for every title or for anime only: its most-liked textless poster, or under Original Art its most-liked poster in their language. TMDB when Fanart has none. Needs the Fanart key and, for series, the TVDB key. Adds poster downloads, cache and text scans for users who pick it. `true` or `false`. |

#### Cinemeta fallback

| Variable | Default | Description |
|---|---|---|
| `CINEMETA_ENABLED` | `true` | Render from Stremio's Cinemeta catalogue (IMDb-keyed, no API key) when no TMDB key is available or TMDB has no record for the IMDb id, and try its Metahub art before the genre canvas when TMDB has no artwork. Needs an imdb_id (or a tt... stremio_id) on the request. `true` or `false`. |

#### Anime sources

| Variable | Default | Description |
|---|---|---|
| `ANIME_SOURCES_ENABLED` | `true` | Serve art, titles, genres and a community score from AniList and Kitsu when a client passes an anilist_id or kitsu_id (or a kitsu:/anilist: stremio_id). Clients that only speak imdb/tmdb are unaffected. Neither provider needs an API key. `true` or `false`. |
| `ANIME_COMPOSITE_LOGO` | `true` | Composite a title logo over anime cover art. That art rarely carries a logotype (or only a small block of Japanese corner text), so a proper logo is usually an improvement; off serves the provider's art untouched. Logos come from TMDB, Metahub or TVDB, so the request needs a tmdb_id or imdb_id, or anime id mapping to supply one. `true` or `false`. |
| `ANIME_ID_MAP_ENABLED` | `true` | Fill in the TMDB and IMDb ids an anime request didn't send, from the community Kitsu/AniList mapping list (downloaded daily into a local table). Lets a client that only sends a kitsu: or anilist: id get TMDB logos, landscape backdrops and IMDb-keyed ratings; art still comes from the anime provider. Also what lets a MyAnimeList id (mal_id, or a mal: stremio_id) render, as the Kitsu or AniList entry it maps to. `true` or `false`. |

#### Rendering

| Variable | Default | Description |
|---|---|---|
| `RANDOM_POSTERS` | `false` | Let users pick a random one of the top five posters (TMDB, Fanart or TVDB) instead of the top one. Each title can then store up to five posters in the disk cache instead of one; the pick changes when the poster re-renders. `true` or `false`. |

#### Text detection

| Variable | Default | Description |
|---|---|---|
| `TEXTLESS_TEXT_DETECTION` | `true` | Detect title text on posters TMDB mislabelled as textless and skip compositing a logo over them. Uses the PP-OCRv5 Mobile detector. `true` or `false`. |
| `TEXTLESS_DETECTION_MAX_VOTES` | `3000` | Foreground OCR vote limit. Titles with more TMDB votes render without waiting, skip composite caching, and enter the idle background scan queue. Raise for foreground accuracy; lower for faster stale-cache bursts. Changing it invalidates cached composites. Only used when `TEXTLESS_TEXT_DETECTION` is `true`. |
| `TEXTLESS_BACKDROP_FALLBACK` | `true` | When a poster TMDB tags as textless turns out to have its title burned in, use the runner-up textless poster (titles with 6+ of them) or a crop of the backdrop with a logo instead. Adds about half a second to the first render of those titles. Changing it invalidates cached composites. Only used when `TEXTLESS_TEXT_DETECTION` is `true`. `true` or `false`. |

#### Performance

| Variable | Default | Description |
|---|---|---|
| `WORKERS` | `1` | Uvicorn worker processes. One worker avoids duplicate uncached renders, scans and API work across processes. |
| `QUALITY_BG_CONCURRENCY` | `5` | Caps concurrent background quality fetches when many uncached titles appear at once. |
| `QUALITY_WAIT_TIMEOUT` | `30` | How long a request with wait_for_quality=true waits for the scraper. |
| `MDBLIST_CONCURRENCY` | `3` | Maximum concurrent outbound MDBList requests per worker. MDBList drops requests past roughly 3 per key. |
| `MDBLIST_MIN_INTERVAL` | `0.2` | Minimum seconds between the start of one outbound MDBList request and the next, across live renders and cache warming. MDBList has a short per-IP burst limit on top of the daily quota, and 3 unpaced concurrent calls can reach it during a cold catalog warm; 0.2 holds the server to 5 calls per second. 0 turns the pacing off. |
| `POSTER_RENDER_CONCURRENCY` | `8` | Maximum uncached poster renders in flight per worker. Cache hits are never held back; a burst of fresh renders (a cold catalog grid) queues past this rather than exhausting the upstream connection pool. Raise on a machine with headroom, lower on a small VPS. |

<!-- settings-reference:end -->

> CPU guidance: keep `WORKERS × TEXTLESS_DETECTION_CONCURRENCY` at or below the CPU cores available to the container. Larger values can oversubscribe CPU, duplicate uncached work across workers, and reduce sustained throughput.

> Sizing, measured with 120 uncached posters requested at once (a cold catalog grid), one worker, Oracle A1 (Ampere) cores:
>
> | Cores | `TEXTLESS_DETECTION_CONCURRENCY` | `POSTER_RENDER_CONCURRENCY` | 120 cold posters | Peak memory |
> |---|---|---|---|---|
> | 1 | 1 (default) | 8 (default) | ~66 s | ~680 MB |
> | 2 | 1 | 8 | ~36 s | ~670 MB |
> | 2 | **2** | 8 | ~31 s | ~850 MB |
> | 4 | 1 | 8 | ~30 s | ~790 MB |
> | 4 | **2** | 8 | ~21 s | ~850 MB |
> | 4 | 3 | 8 | ~18 s | ~1.1 GB |
>
> `POSTER_RENDER_CONCURRENCY` is not a throughput knob: 4 to 32 measured the same wall time at every core count, and higher values only raise peak memory (each admitted render holds its decoded art while it waits for CPU). Leave it at `8`; `4`–`6` is a sensible ceiling on a 1 GB host. What scales with cores is `TEXTLESS_DETECTION_CONCURRENCY`: at the default of 1 the burned-in-text scans run one at a time and are most of the floor, so on 2 or more cores with 2 GB or more of RAM set it to `2`. Beyond ~18 s the single-process event loop is the ceiling and more cores do not help one worker.

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

QualiCache inverts that. It is a hosted service, run alongside PostersPlus for
its users and Nuvio HTPC's, that checks the same kind of addons in the background
and picks one best release from a known release group. PostersPlus only reads
what is already in its cache, so it never blocks on a scrape. QualiCache itself
is not open source; connect to the hosted instance with the URL and access key
below.

```dotenv
QUALITY_SOURCE=qualicache
QUALICACHE_URL=https://quality.myaio.xyz
QUALICACHE_API_KEY=HN7Aj1Z4CV95k0ZxJJ583nmU
# Accept known ranked groups only (high), unknown groups too (medium), or all tiers (low)
QUALICACHE_MIN_TRUST=medium
```

Leave `AIOSTREAMS_URL` and `AIOSTREAMS_AUTH` unset- they're ignored when
`QUALITY_SOURCE` isn't `aiostreams`, and PostersPlus warns at startup if both
are configured.

**Pending results.** A title QualiCache hasn't collected yet answers `pending`
rather than an error. PostersPlus serves the poster immediately without badges
and doesn't cache that composite, so the next request picks the badges up once
QualiCache has them. Crucially, pending doesn't count against the quality
source's failure budget- a cold title never triggers the backoff that a real
outage does. In practice, common and recently released titles are usually warm before
anyone asks for them.

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

## Watchlist marker

Self-hosted instances can mark every title in **one** user's watchlist with an amber **Watchlist** sash. It is a single list for the whole instance by design: the rendered-poster cache is shared by every client of an instance, so a per-user watchlist would fragment it per user and multiply upstream quota. That also makes it a poor fit for the public instance, which leaves it unset.

Set `WATCHLIST_SOURCE` to one of:

| Value | What it reads | What you need |
|---|---|---|
| `mdblist` | The MDBList watchlist of the account behind `MDBLIST_API_KEY` | Nothing extra. **Trakt users:** enable Trakt sync in MDBList's preferences and MDBList mirrors your Trakt watchlist here- Trakt's own API now needs a VIP-gated app key, so this is the free route |
| `simkl` | The account's *Plan to Watch* list (`WATCHLIST_SIMKL_STATUSES` adds `watching` / `hold`) | A free SIMKL app: create one at [simkl.com/settings/developer](https://simkl.com/settings/developer/), choosing **TV, devices & command line**- PostersPlus links by code, so that type needs no secret and no redirect URL (pick **AUTH V2** if offered; V1 still works but retires around April 2027)- and set `SIMKL_CLIENT_ID`. Then open the [admin dashboard](README.md#admin-dashboard)'s **Watchlist** group: a *SIMKL account* panel offers a link code; open the link, sign in, approve, and the panel flips to linked. (The same link and code are printed in the container log, which is the route without an `ADMIN_KEY`.) Tokens live in the cache volume and V2 tokens refresh themselves. Only an app registered as *Server apps & services* also needs `SIMKL_CLIENT_SECRET` |
| `trakt` | `TRAKT_USERNAME`'s watchlist | `TRAKT_CLIENT_ID` from an existing Trakt API app (creating one requires Trakt VIP as of August 2026). Reads the public profile with no OAuth; a private profile needs `TRAKT_ACCESS_TOKEN` too |
| `pmdb` | The [PublicMetaDB](https://publicmetadb.com) watchlist of the account behind `PMDB_API_KEY` (`PMDB_LIST_ID` reads another list instead) | A PMDB API key from **Settings → API** on publicmetadb.com. PMDB lists carry only TMDB ids, so the sash needs the title's TMDB id: it shows wherever the server or client has a TMDB key, but not on a Cinemeta-only render |
| an MDBList list URL | That list, via its JSON export- a shared household "to watch" list, for example | Nothing; public lists need no key |

The same panel has an **Unlink account** button once linked: it forgets the token, takes the sash off every poster that had it, and offers a new code- for switching accounts, or moving from a V1 app to a V2 one. (A V2 grant is revoked at SIMKL as well; a V1 token has no revoke endpoint, so remove PostersPlus at simkl.com/settings/connected-apps if you want it gone there too.) Linking is an operator action, which is why it lives behind `ADMIN_KEY` rather than in the configurator: the link code is withheld from users of the instance, since approving it with their own account would point the instance at their watchlist. The configurator only shows which source is configured and how many titles it holds.

The list is re-checked every `WATCHLIST_REFRESH_MINUTES` (default 30). Each check is cheap- one MDBList page per 500 titles, SIMKL's tiny `/sync/activities` call with the list itself only re-read when it changed, two Trakt calls, PMDB's list lookup plus one page per 500 titles- and when a title is added or removed, only the cached posters for *that* title are re-rendered, so the marker follows the tracker within one interval. The snapshot survives restarts. The sash is first in the default priority (a queued title beats an Oscar winner); drag it lower in the configurator if you would rather keep the prestige sashes on top. Plex/Jellyfin users need to re-run the sync script to push the updated posters.

## Custom trending sources

Set `TRENDING_SOURCE_MOVIE` and/or `TRENDING_SOURCE_TV` to an ordinary MDBList page URL or any endpoint returning TMDB-shaped `{"results": [{"id": 1234}]}` JSON. The source order becomes the ranking for both Trending sashes and cache warming. Movie and TV sources are independent; leave either one empty to keep TMDB's global list for that media type. Entries must contain numeric TMDB ids.

## Trending catalogs addon

The Trending sashes print a rank ("#10 Today"), but a Trending row in your metadata addon is built from its own copy of the list, fetched at a different time. TMDB's list moves every few minutes, so the two rarely agree. PostersPlus serves the lists behind the sashes as a small Stremio addon, so the row order matches the labels exactly. It is on by default; set `TRENDING_CATALOGS_ENABLED=false` to turn it off.

The manifest is at `/trending/manifest.json`, or `/trending/<ACCESS_KEY>/manifest.json` when an access key is set (the configurator shows the full URL under Core). It has four catalogs:

| Catalog | List |
|---|---|
| Trending Movies | TMDB's day list, or `TRENDING_SOURCE_MOVIE`, without anime |
| Trending Series | TMDB's day list, or `TRENDING_SOURCE_TV`, without anime |
| Trending Anime | AniList's trending anime series (TV, TV short and ONA), or `TRENDING_SOURCE_ANIME` |
| Trending Anime Movies | AniList's trending anime films, or `TRENDING_SOURCE_ANIME_MOVIE` |

Each catalog lists ranks 1 to `TRENDING_BROAD_FETCH_COUNT`, and item N is rank N. Each poster URL it hands out carries an `rv=` naming the list it was cut from, so when a list is rebuilt outside the daily refresh, a client holding the previous images (each with its old rank) fetches new ones instead of keeping them for their max-age. In AIOMetadata, import the manifest as a custom manifest and set each catalog's cache time to 0, so the row is re-read from PostersPlus whenever it opens. A longer cache time works too, but the row then lags the labels for up to that long after each daily refresh. AIOMetadata's own filters, such as an age-rating cap, can still remove titles from a row, which leaves a gap in the numbers.

With the addon enabled, anime ranks only on the two anime lists, which are built first. Every title on them is left off the movie and series lists, whatever its language, so no title is in two rows with two ranks; so is Japanese anime that isn't trending on AniList, by the anime id mapping or TMDB's Animation-from-Japan. The movie and series lists are numbered without them. Chinese and Korean animation that isn't on the anime lists stays on the series and movie lists, and so does Western animation. Every anime poster then shows its anime rank, whatever id it is requested by and whichever catalog it is in. An AniList or Kitsu id (a MyAnimeList id arrives as one of those) is one entry, a season on both sites, and shows that entry's rank. A TMDB or IMDb id is the whole show and shows the best rank any of its seasons holds. A title that isn't on the anime list shows its TMDB rank, if it has one.

`TRENDING_SOURCE_ANIME` and `TRENDING_SOURCE_ANIME_MOVIE` replace AniList's lists, in the same shapes as the movie and TV sources. Their rows are TMDB ids, so their catalogs hand out IMDb (or TMDB) ids and posters are ranked by the TMDB id they carry.

## Customising directors, studios and cast

**Source editors** can modify the lists directly in `discovery.py`.

**Operators** can edit them in the admin dashboard's **Sash lists** view: search TMDB for a studio or person to add, change the label the sash shows, remove entries, or go back to the built-in list. Changes apply without a restart, and posters re-render as they are next requested.

The dashboard stores the lists in `/app/cache/discovery_overrides.json` inside the cache volume, which can also be written by hand. See `discovery_overrides.example.json` for the format.

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

An optional background task that proactively populates the TMDB metadata/image/logo cache and the MDBList rating/award cache for a mix of currently-trending, popular, and top-rated/now-playing/on-the-air titles, plus any Stremio addon catalogs you point it at- so the *first* real request for a hot title is already cached instead of hitting upstream APIs cold. Off by default; enable it once you understand your server API keys' rate limits, since it spends its own budget of upstream calls independent of real traffic.

The switch and its budgets are the **Cache warming** group of the [settings reference](#settings-reference) (and of the admin dashboard, whose Overview shows the last run). The MDBList budget is quota-aware: the warmer stops spending a key once its remaining daily requests fall to `CACHE_WARM_MDBLIST_RESERVE`, so a cycle cannot leave the rest of the day without ratings.

Candidates are split roughly 40% trending / 30% popular / 30% supplemental (top rated, now playing, on the air), deduplicated, and any configured catalog candidates are warmed first. Cycle progress (candidates found, budgets spent) is logged at startup and after each run.

---

## Plex and Jellyfin Sync

`plex_sync.py` and `jellyfin_sync.py` are companion scripts that read your media library, derive quality tokens from each title's own media-file metadata, and push PostersPlus-generated posters back as library covers. This keeps your Plex or Jellyfin art consistent with the same quality-badge logic used by the Stremio-facing poster endpoint, without relying on AIOStreams or a scraper addon for quality data.

#### Requirements

```bash
# Plex
pip install -r requirements-plex.txt

# Jellyfin (httpx only, likely already installed)
pip install -r requirements-jellyfin.txt
```

#### Configuration

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

#### Usage

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
