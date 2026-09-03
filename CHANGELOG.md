# Changelog

## Unreleased

### Anime

- Added anime-native poster requests through AniList and Kitsu. Clients that
  supply `anilist_id`, `kitsu_id`, or an AIOMetadata-compatible `{id}` can use
  the provider's cover art, title, genres, air dates, lifecycle status, and
  community score without converting the title to a TMDB or IMDb id.
- Added an off-by-default Anime IDs configurator option for AIOMetadata poster
  URLs. Anime-native ids are also carried through to compatible quality sources,
  so stream-quality badges continue to work when no IMDb id exists.
- Anime requests now keep any accompanying TMDB and IMDb ids for logos, MDBList
  ratings, awards, age ratings, release data, and other enrichment. AniList and
  Kitsu scores participate in the normal weighted-rating pipeline and default
  to zero weight.
- Anime cover art can receive a TMDB logo by default. If an anime provider is
  unavailable or misses a title, rendering temporarily falls back to TMDB art
  instead of a genre canvas without caching the degraded result.
- Improved anime provider caching, concurrency limits, genre selection, request
  identity, and placeholder handling. Definitive misses are negative-cached,
  while throttles and transient provider failures are not.

### Poster Rendering

- Added a dedicated 16:9 poster layout through `shape=landscape`, with backdrop
  artwork, height-relative sizing, a unified bottom information band, and clear
  top corners for client overlays. `landscape_art` selects textless or original
  artwork and `badge_pos` controls the age-rating badge position.
- Added poster-coloured top and bottom vignettes with saturation, blur,
  lightness, two-colour ramp, and blend controls. Tint selection now samples the
  artwork near the visible seam, rejects shadow-only and conflicting colours,
  and limits excessive chroma for more consistent results across a shelf.
- Frosted notches and bars can match the colour actually painted by a tinted
  vignette. Matching preserves the vignette's lightness and falls back to the
  normal frost colour when the band does not have a reliable tint.
- Posters confirmed to contain a baked-in title use a plain black vignette
  instead of blurring and tinting the title inside the artwork.
- Expanded Minimalist mode with a Split layout, optional centring, and separate
  field and rating separators. Pip, bullet, and rating-star treatments are
  exposed only where they apply, including the score-coloured separator in Year
  mode.
- Added independent notch padding so the space above and below a label can be
  tightened without shrinking the font, changing the badge width, or moving the
  notch.

### Configurator

- Redesigned the configurator with rounded panels, sentence-case group headings,
  text tabs, consistent spacing and controls, a cleaner preview panel, and
  refreshed preset and import dialogs across every settings tab.
- Reworked inline help into row tooltips that also work on touch devices, and
  improved control grouping, contrast, button styling, colour swatches, and the
  sash-priority editor.
- Moved Import from URL into the header and added a title-link menu with IMDb,
  TMDB artwork, MDBList, and SIMKL shortcuts. Fixed menu links that could open
  `#` before their targets were initialized.
- Generated poster URLs and presets no longer carry an `imdb_id` placeholder,
  which previously discarded the whole URL for any title without an IMDb link.
  A title with no linked IMDb id is now reported as a normal state rather than an
  error, previews load from the TMDB id alone, and the result is remembered for a
  week so the resolver is not re-run on every load.
- Plex and Jellyfin sync no longer skip library items that have a TMDB id but no
  IMDb id. Quality badges from the local file continue to work for those items,
  and an `imdb_id` baked into a copied recipe URL can no longer be applied to
  items it does not belong to.
- Wait for Quality is now sent for the Combined badge mode, which offered the
  toggle but left it out of the generated URL.

### Quality

- Added QualiCache as a quality source: set `QUALITY_SOURCE=qualicache` and
  `QUALICACHE_URL` (plus `QUALICACHE_API_KEY` if QualiCache sets an access key).
  QualiCache crawls Stremio addons in the background and answers from its own
  cache, so poster rendering no longer waits on a scrape and one instance can
  serve PostersPlus and other clients at once.
- Titles QualiCache hasn't collected yet report as pending rather than failed.
  The poster is served without badges and the composite isn't cached, so a later
  request picks the badges up — and a cold title no longer counts against the
  quality source's failure budget the way a real outage does.
- QualiCache tokens with no PostersPlus badge (`8K`, `1440P`, `720P`, `SD`,
  `BLURAY`, `WEBRIP`, `HDTV`) are dropped rather than mapped to an approximate
  equivalent.
- Quality backend selection now runs through one dispatcher instead of being
  repeated at each call site. `/status` reports the active backend as
  `quality_source`.
- The Quality Bookmark badge mode now seeds Badge Size at 30 rather than 16.
  At 16 the corner mark was barely visible at the poster sizes most clients
  render at, so the mode looked like it hadn't worked. The right value varies
  by client, which the mode's tooltip now says.

### Ratings

- Added an MDBList-free way to source two of the weighted rating inputs.
  `tmdb_rating_source=direct` uses TMDB's own vote average — already fetched
  alongside genre/year/credits, so it costs nothing extra and needs no MDBList
  key. `imdb_rating_source=dataset` sources the IMDb weight from IMDb's own
  free, no-key, daily-refreshed non-commercial dataset
  (`title.ratings.tsv.gz`), downloaded and refreshed on a schedule
  (`IMDB_DATASET_ENABLED`, `IMDB_DATASET_REFRESH_HOURS`,
  `IMDB_DATASET_MIN_VOTES`, `IMDB_DATASET_PATH`) and looked up locally with no
  per-title network call. Both default to the existing MDBList-sourced
  behaviour and are exposed as dropdowns on the Weights tab, and either can be
  used with zero MDBList key configured.
- Both settings also take `fallback`, which keeps MDBList as the source of
  truth and consults the local source only when MDBList has no value for that
  title. That covers a hard gap — a rate-limited or exhausted key, a timeout,
  every configured key cooling down — and a soft gap, where MDBList answered
  but carried no score for the title (or one `RATING_MIN_VOTES` filtered out),
  with the same rule. `tmdb_rating_source=fallback` needs no server-side setup
  at all, which makes it the cheapest way to keep scores alive through an
  MDBList outage. This is a different layer from `fallback_to_imdb`: that one
  fires when the *weights* score nothing and reaches for whatever `imdb` value
  is present, whereas these put a value there for it to find. They compose.
- The configurator now disables the two dataset-backed IMDb source options
  when `IMDB_DATASET_ENABLED` is off server-side, and coerces an imported URL
  that names one back to `mdblist`. Selecting an option the server can't
  honour produced a URL that looked configured and silently scored `N/A`.
  `/server-caps` already reported the state; nothing was reading it.
- Only one worker per interval downloads the IMDb dataset. With `WORKERS` > 1
  every worker ran its own copy of the refresh loop against the same
  database, so the losers of the table swap failed with `database is locked`
  and — worse — kept a stale row count, which left them reporting an empty
  dataset on `/server-caps` and splitting the composite cache signature.
- Corrected `.env.example`'s `MDBLIST_API_KEY` documentation, which called it
  `[Required]`; it has been optional in the request path for some time (see
  the "IMDb ids are now optional" entry above) and is now spelled out exactly
  which sashes and the score are unavailable without it.

### Metadata And Caching

- Poster responses now advertise the composite's own expiry, so a caching client
  keeps a trending-sashed poster for a day and a settled title for the full
  `COMPOSITE_CACHE_TTL`. A configured `CDN_CACHE_TTL` acts as a ceiling the
  deadline can lower, `CDN_CACHE_TTL=auto` drops the ceiling, and `0` still
  sends no `Cache-Control`. `304` responses carry the same freshness.

- IMDb ids are now optional. `tmdb_id` is the required identity — it selects the
  artwork and metadata — and `imdb_id` is optional enrichment. Titles TMDB has no
  IMDb link for previously returned an error and, through AIOMetadata, lost their
  poster entirely because a required placeholder with no value discards the whole
  URL. Existing URLs that send both ids are unchanged.
- Ratings, awards, keywords, and age ratings are now looked up through MDBList's
  TMDB route when no IMDb id is available, so TMDB-only titles keep their score
  and sashes. A title MDBList does not know still renders from TMDB metadata with
  an `N/A` score.
- Stream-quality lookups now resolve their id after metadata, so a title whose URL
  omits `imdb_id` still gets quality badges via the IMDb id TMDB itself supplies.
  Anime keeps its provider-native id for these lookups. Titles with no IMDb id
  anywhere skip the lookup rather than issuing one nothing can answer; an explicit
  `quality=` override is unaffected.
- Rating cache, coalescing, and back-off state are now keyed on one immutable
  per-request identity (`tmdb:<id>` when there is no IMDb id) rather than the raw
  `imdb_id` parameter. Cache warming writes the same identity the request path
  reads. Metahub logo fallback, digital-release detection, and IMDb links run only
  when an IMDb id is actually available.
- `/poster?debug=1` now reports the resolved identities — `canonical_id`,
  `rating_provider`, `rating_media_id`, `quality_id`, and `effective_imdb_id`.
- Added `TRENDING_SOURCE_MOVIE` and `TRENDING_SOURCE_TV` so operators can replace
  TMDB's global trending list with an MDBList page or a TMDB-shaped endpoint.
  The configured order drives both trending sashes and cache warming, enabling
  regional or service-specific rankings.
- Custom trending sources now isolate movie and TV entries, reject rows without
  numeric TMDB ids, follow canonical MDBList URLs, refresh cleanly when the
  configured source changes, and avoid exposing credentials or query strings in
  cache signatures and logs.
- Release-status caches now use status-aware lifetimes: active, in-production,
  and cinema titles refresh quickly, while ended, cancelled, physical, and
  established streaming releases remain cached longer. Known release dates set
  the next refresh boundary directly.
- Composite posters now expire no later than the release data rendered into
  them. Disk and in-memory cache entries share the same deadline, and cache
  warming reuses the trending snapshot it already fetched.
- Rating-provider failure counters are now pruned together with their expired
  backoff state.

### Performance And Reliability

- Reduced startup memory by loading genre fallback backgrounds on demand into a
  bounded cache instead of decoding the whole gallery, and reduced per-thread
  SQLite page-cache memory. Fallback fonts are now cached as well.
- Made fallback-title rendering faster and more reliable by starting font
  fitting from a monotonic width search, fitting long titles rather than cutting
  them off, and ellipsizing every landscape fallback line that needs it.
- Reduced score and quality-bar composition work by drawing only the affected
  strips instead of repeatedly compositing full-canvas layers.
- OCR thread sizing now respects the container's actual cgroup CPU quota rather
  than the host CPU count. `TEXTLESS_DETECTION_CONCURRENCY` now defaults to `1`
  to avoid slower scans and roughly 50 MB of unnecessary memory per idle
  session; larger values remain available for cold-cache library sweeps.
- Landscape requests no longer wait for quality data the layout does not render,
  and transient custom-trending failures use a short retry cooldown rather than
  refetching once per poster.

### Configurator

- Generated poster URLs are about a third of their previous length - roughly
  1500 characters down to 450 on the shipped presets. Some metadata services
  truncate or reject URLs past 2000 characters, and most of what was there
  restated settings the server would have chosen anyway. Three changes get it
  there: parameters already at their default are left out, `sash_priority` is
  sent as a diff against the default order, and rating sources weighted at zero
  are no longer named. The server parses the result identically and every URL
  generated before this keeps working unchanged.
- `sash_priority` now accepts a diff form: `default,-cult,festival@0` removes
  the cult sash and promotes the festival one, instead of listing all thirty
  slots. The full list is still accepted and still means what it always did.
- The defaults the configurator omits are read from the server at load time
  rather than restated in the page, so they cannot drift apart. If the server
  cannot be reached the full-length URL is generated instead.

### Fixes And Documentation

- Fixed festival sashes naming a top prize the film did not win. MDblist tags a
  title `festival-cannes-winner` if it won *anything* at Cannes, and that was
  read as "Palme d'Or" — so the whole 2023 slate, from the Grand Prix winner
  down to the Un Certain Regard one, wore a Palme d'Or sash — nine films, of
  which one had won it. Every festival in the list had the same fault.
  The top prize is now looked up by TMDB id against a list built from Wikidata,
  and the keyword only supports the weaker claim it can actually carry: a title
  that won something at Cannes but not the Palme reads "Cannes Winner". A top
  prize now also shows when MDblist is unreachable, since the list is local.
  Cached titles convert on first startup without re-fetching anything.
- Cross-checked every top-prize list against the festivals' own winners tables,
  edition by edition. That restored 34 winners the first source had no record
  of — Joker's 2019 Golden Lion, four recent Locarno Leopards, Cannes' 1946
  eleven-way tie — each of which had been showing the weaker sash.
- Removed 9 films that were wearing a top prize they did not win. Three were
  Golden Bear winners for Best Short Film rather than the Golden Bear, two were
  Berlinale and Locarno sidebar prizes, and one was a mismatched id: Precious
  premiered at Sundance as "Push: Based on the Novel by Sapphire", and its Grand
  Jury Prize had landed on the unrelated 2009 science-fiction film Push, which
  wore the sash while Precious went without.
- Removed the Toronto, Busan, Rotterdam, SXSW and Tribeca festival sashes. Their
  labels — People's Choice, New Currents, Tiger Award, SXSW Jury, Tribeca AA —
  named specific prizes no available source can confirm, and unlike the five
  festivals that remain there is no list to check them against. Cannes, Venice,
  Berlin, Locarno and Sundance are unaffected.
- Fixed quality badges never appearing unless Wait for Quality was on. A poster
  served before its quality arrived was correctly kept out of the composite
  cache, but still carried an ETag identical to the finished render's, so
  clients and CDNs revalidated their badge-less copy and were told it was still
  current. Renders the server declines to keep now ship no validator and ask not
  to be stored. Clients holding a badge-less poster from before this fix keep it
  until the composite TTL lapses or the URL changes.
- Fixed missing ratings leaving an empty score in the information strip, and
  fixed fallback titles that could be clipped instead of resized to fit.
- Fixed landscape fallbacks losing their title, TV shows retaining a stale
  ended status after revival, and release sashes surviving past a newly reached
  digital-release boundary.
- Split the oversized `.env.example` into a concise starter configuration and a
  new `ADVANCED.md` tuning reference. Added previously undocumented OCR and face
  model path overrides and corrected OCR concurrency guidance and defaults.

### Localization

- Added Brazilian Portuguese (`pt-br`) poster-output translations, contributed
  by @danilopagotto82.
- Region-qualified translation files take precedence over the bare language, so
  a `pt-br` request uses `languages/pt-br.json` rather than `languages/pt.json`.
  Selecting `pt-br` also restricts logo artwork to Brazil-tagged entries,
  falling back to English rather than to Portugal-tagged art.
- Translated the remaining fixed sash vocabulary in every shipped language: the
  release-status labels (`Physical`, `Streaming`, `Cinema`, `Production`,
  `Airing`, `Ended`, `Cancelled`) and the ten festival winner labels, from
  `Palme d'Or` through `Tribeca AA`. Previously these rendered in English on an
  otherwise translated poster.

## v1.1.0 - 2026-06-09

This release is compared with the original `v1.0.0` release. It also includes
the maintenance fixes published in the v1.0.x releases.

### Highlights

- Added several new poster layouts, including Frosted Bar, Minimalist, Clean,
  and expanded quality and age-rating treatments.
- Rebuilt textless-poster validation around the PP-OCRv5 Mobile detector, with
  background scanning and load controls for live installations.
- Added smarter TMDB poster selection so a tiny number of votes cannot easily
  promote a badly rated poster over substantially better artwork.
- Expanded artwork selection with original-art controls, language-aware poster
  matching, improved logo fallback, and face- and saliency-aware cropping.
- Added a preset gallery and reorganized the configurator into a mobile-friendly
  tabbed interface.
- Added poster-output translations for French, Portuguese, and Italian.

### Poster Rendering

- Added independent top and bottom vignette controls with Off, Low, Medium, and
  High strengths.
- Added Frosted Bar mode with:
  - Frosted, black, silver, gold, and rating-focused styles.
  - Optional rating, year, and sash content.
  - Rating progress and out-of-ten display variants.
  - Poster-derived tinting that can be shared with the sash notch.
- Expanded Minimalist mode with improved title, metadata, and fallback-art
  presentation.
- Added Clean mode for a restrained score and metadata layout.
- Added poster-derived sash colors and an option to match the Frosted Bar.
- Added diagonal, notch, and hidden sash display modes.
- Added filled and frosted notch styles.
- Split sash sizing into dedicated width, height, inset, and font controls.
- Added winner-star treatment for selected award sashes.
- Added release-status sashes for BluRay, Streaming, Cinema, and Production.
- Added greyscale treatments for Cinema and Production releases.
- Added options to keep release artwork in color when stream quality is known,
  or use greyscale when no quality is available.
- Added six quality display choices covering the quality notch, quality with
  age rating, badge row, combined text badge, age rating only, and hidden output.
- Added a minimum quality threshold (`badge_min_score`) to all quality display
  modes. When set, the badge is suppressed for streams whose quality score falls
  below the configured value; no-data states are always rendered regardless.
- Added age-rating badges and tracking.
- Improved score bars, badge alignment, spacing, gradients, metadata placement,
  and long-title handling across layouts.

### Artwork And Logos

- Added a Primary or Top Rated source selector for original artwork.
- Added language-aware poster selection, including support for original artwork
  in the requested language.
- Improved logo selection priority across requested, native, original, and text
  fallbacks.
- Added Metahub as an additional logo fallback.
- Added configurable logo sizing and safer contrast and stretch behavior.
- Added face-aware and saliency-aware backdrop cropping.
- Added text-aware crop selection to reduce accidental clipping of useful
  artwork.
- Added minimalist and photoreal genre fallback backgrounds.
- Improved title fallback rendering when no usable logo is available.
- Added a fallback gallery endpoint for reviewing generated title and genre
  artwork.

### Textless Poster Detection

- Replaced the previous EAST detector with PP-OCRv5 Mobile.
- Added title-aware OCR rules to detect posters incorrectly marked as textless
  while avoiding rejection solely for a matching standalone logo.
- Added specialized handling for wide, low-contrast, repeated, and
  design-integrated text.
- Added versioned detection signatures so tuning changes invalidate stale OCR
  results automatically.
- Added a deduplicated cache-volume report of TMDB posters that OCR identifies
  as incorrectly marked textless, including direct review links.
- Added request coalescing so simultaneous requests for the same poster share a
  single scan.
- Added a dedicated text-detection executor with configurable concurrency.
- Added foreground vote gating to keep uncached burst traffic responsive:
  - Posters at or below the configured vote limit are scanned during the request.
  - Posters above the limit are served without caching the composite and queued
    for an idle background scan.
  - Once the background scan completes, later requests use the cached detection
    result and can cache the completed composite normally.
- Bundled the compact detector model in standard Docker builds by default.

### TMDB Poster Selection

- Added a minimum-vote preference when ranking textless poster candidates.
- Preserved the previous selection behavior when no candidate reaches the
  minimum vote count.
- Added a maximum score-drop safeguard so vote confidence cannot promote a
  heavily downvoted poster over much better-rated artwork.
- Included the ranking policy in cache signatures so selection-setting changes
  take effect without manual cache removal.

### Ratings

- Added a minimum vote count for rating providers. Scores with fewer than 10
  votes are ignored by default.
- Exempted Roger Ebert from the vote minimum because its source represents a
  single critic rating.
- Added a per-configuration "Fallback to IMDb" toggle. When enabled, IMDb is
  used only if the selected weighted sources produce no score.
- Improved normalization, missing-provider handling, and provider metadata
  caching.
- Added MDBList secondary API key rotation and rate-limit backoff.
- Improved cache invalidation when rating policy or provider metadata changes.
- Refined the default movie weighting toward Letterboxd with Trakt as a
  low-weight fallback.

### Quality And Release Data

- Added Stremio scraper support as an alternative quality source.
- Improved AIOStreams quality parsing and quality-token normalization.
- Improved digital release synchronization and release-status prioritization.
- Improved background quality refresh behavior and failure handling.
- Added server capability reporting so the configurator can hide unsupported
  options cleanly.

### Configurator

- Rebuilt the configurator as a tabbed interface covering Core, Rating, Logo,
  Sash, Quality, and Weights settings.
- Added a preset gallery with ready-made poster styles.
- Added settings persistence in the browser.
- Restored importing an existing Posters Plus URL for editing.
- Added editable values alongside range sliders.
- Added expanded preview and crop simulation.
- Added controls for original artwork, poster language behavior, logo sizing,
  sash styles, Frosted Bar, age ratings, release colors, and the IMDb fallback.
- Added a light/dark mode toggle to the header. Preference persists in the
  browser across sessions.
- Improved responsive and mobile layouts.
- Improved generated URL handling when the server is accessed over a LAN.
- Added a composite-cache toggle for testing and troubleshooting.
- Updated default values: top vignette defaults to Medium (was High),
  minimalist rating horizontal position defaults to 0.065 (was 0.05), match
  notch color for Frosted Bar modes is enabled by default, diagonal sash height
  defaults to 0.135 (was 0.12), diagonal sash corner distance defaults to 1.20
  (was 1.15), minimum quality threshold defaults to score 5 for Badge Row /
  Quality Notch / Combined Text Badge modes and score 2 for Quality Age Rating
  mode, and IMDb fallback is enabled by default.
- Updated preset gallery: all presets now include the IMDb fallback setting.
- Updated Primary Client selector label to list Plex and Jellyfin alongside
  Stremio TV and Nuvio, reflecting the shared flush-edge inset profile.

### Plex and Jellyfin Sync

- Added `plex_sync.py`, a companion script that reads a Plex library, derives
  quality tokens from each title's actual media file metadata, and pushes
  PostersPlus-generated posters back as library covers.
- Added `jellyfin_sync.py`, a companion script with the same workflow for
  Jellyfin libraries, using the Jellyfin REST API directly without a
  third-party SDK.
- Both scripts detect resolution, HDR format, audio codec, and release type
  (Remux, WEB-DL) from file paths and stream display titles.
- Both scripts include an `--inspect` mode that logs derived quality tokens for
  every library title without writing any posters, making it easy to audit
  token derivation against known titles before a full sync.
- TV show quality is derived from a representative episode selected by watch
  progress, air date, and episode count.

### Localization

- Added French, Portuguese, and Italian poster-output translations.
- Added translated genre and sash labels.
- Poster translations follow the selected logo language.
- Missing translation keys fall back to English individually.

### Performance And Reliability

- Changed the default Uvicorn worker count from 2 to 1. A single worker avoids
  loading duplicate OCR models and was faster in testing for typical installs.
- Set text-detection concurrency to 2 by default.
- Added in-flight request coalescing for expensive shared work.
- Hardened SQLite use with WAL mode, busy timeouts, retry handling, and safer
  multi-request cache writes.
- Added cache pruning, reclaim, and vacuum maintenance.
- Improved metadata, logo, poster, rating, and composite cache invalidation.
- Improved handling of stale cache rebuilds and large request bursts.
- Improved Docker builds for amd64 and arm64, including reliable multi-platform
  `latest` publishing.
- Added more detailed diagnostics for text detection, artwork selection, cache
  behavior, quality lookup, and render timing.

### Fixes

- Fixed several false-positive and false-negative text detections found during
  broad real-world poster testing.
- Fixed stale OCR results surviving detector or threshold changes.
- Fixed cases where a skipped textless scan could be treated as a final cached
  decision.
- Fixed duplicate work when many requests asked for the same uncached poster.
- Fixed edge cases in poster ranking with very small or negative vote samples.
- Fixed logo fallback, logo contrast, and oversized-logo edge cases.
- Fixed missing or malformed metadata causing incomplete poster renders.
- Fixed score-bar totals, normalization text, and missing-provider behavior.
- Fixed sash and quality visibility interactions.
- Fixed configurator spacing, slider, dropdown, preview, and mobile layout
  issues.
- Fixed backdrop crop centering on false-positive face detections, where a
  large low-confidence background blob could outrank a smaller, genuinely
  detected face on bounding-box size alone.
- Fixed Docker workflow races that could publish an older image as `latest`.

### Upgrade Notes

#### Recommended defaults

```env
WORKERS=1
TEXTLESS_DETECTION_CONCURRENCY=2
TEXTLESS_DETECTION_MAX_VOTES=3000
RATING_MIN_VOTES=10
TMDB_POSTER_MIN_VOTES=3
TMDB_POSTER_MAX_SCORE_DROP=1.0
PPOCR_BOX_THRESHOLD=0.70
PPOCR_WIDE_BOX_THRESHOLD=0.30
PPOCR_WIDE_MIN_ASPECT=3.0
PPOCR_WIDE_MIN_AREA=0.01
PPOCR_WIDE_MIN_Y=0.55
TEXTLESS_SCAN_TOP=0.08
BAKE_PPOCR_MODEL=true
```

- Keep `WORKERS x TEXTLESS_DETECTION_CONCURRENCY` at or below the number of
  available CPU cores unless the host has been tested under realistic load.
- Larger values can improve short bursts on powerful systems, but also increase
  CPU contention, memory use, duplicate model memory across workers, and
  pressure on SQLite.
- `TEXTLESS_DETECTION_MAX_VOTES` controls the foreground speed versus immediate
  detection tradeoff. Lower values defer more scans; higher values scan more
  posters before responding.

#### Text detector migration

- EAST has been replaced by PP-OCRv5 Mobile.
- Existing EAST settings such as `TEXTLESS_MIN_BOXES`, `EAST_INPUT_WIDTH`,
  `EAST_INPUT_HEIGHT`, `EAST_MODEL_URL`, `EAST_MODEL_PATH`, and
  `BAKE_EAST_MODEL` are no longer used.
- Standard Docker images include the PP-OCR detector model. Builds with
  `BAKE_PPOCR_MODEL=false` download it into the model cache at runtime.

#### Compatibility

- Existing v1.0 poster URLs remain supported.
- Legacy sash and quality parameters continue to map to their current
  equivalents.
- The `combined_badge_min_score` URL parameter is accepted as a fallback for
  `badge_min_score` so existing Combined Text Badge URLs continue to work.
- Compact mode, which appeared during v1.1 development, was replaced by Frosted
  Bar before release.
- Cache schema migrations run automatically.
- Rating, artwork-selection, OCR, and composite signatures automatically refresh
  results affected by changed policies.
- The IMDb fallback is stored in the generated configuration URL and does not
  require a server environment variable.

### Included v1.0.x Maintenance

- Corrected release and Docker publishing workflows.
- Fixed showcase and documentation links.
- Improved multi-platform image publishing and `latest` tag consistency.
