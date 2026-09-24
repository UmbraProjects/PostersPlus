# URL reference

What goes in a poster URL, and how the overlays behind it decide what to show. You don't need any of this to get started: the configurator builds the URL for you. It's here for when you want to hand-edit one or understand why a poster looks the way it does.

- [Poster URLs](#poster-urls) · [Client templates](#client-templates) · [Without a TMDB key](#without-a-tmdb-key)
- [Landscape posters](#landscape-posters) · [Logo endpoint](#logo-endpoint) · [Anime IDs](#anime-ids-anilist--kitsu) · [Operator endpoints](#operator-endpoints)
- [Award sashes](#award-sashes) · [Ratings](#ratings) · [Poster translations](#poster-translations)

Server settings are in [CONFIGURATION.md](CONFIGURATION.md).

---

## Poster URLs

Posters are served at `/poster` with parameters controlling every aspect of rendering:

```
https://yourdomain.com/poster?tmdb_id={tmdb_id}&type={type}
```

Either id identifies the title — `tmdb_id`, `imdb_id`, or a `tt…` value in `stremio_id` — and sending both is best. `tmdb_id` selects the artwork and the metadata directly. An `imdb_id` on its own is resolved to a TMDB id first (TMDB's `/find`, persisted so it costs one call per title ever), and the request then renders exactly as if the client had sent both; if TMDB says the IMDb id is a series rather than a movie, TMDB's type wins. `imdb_id` alongside `tmdb_id` is optional enrichment — send it if your client has one reliably (the Plex and Jellyfin sync scripts do) and it keys the rating cache by IMDb id, sharing that row with every other client. Neither id should be *required* in a template. A required placeholder with no value makes the resolver discard the entire URL, so the title gets no poster at all — and both ids hit that: TMDB has no IMDb link for some titles, and a client's catalogue often has no TMDB id for one (Nuvio's frequently doesn't). Since either id renders on its own, the optional `{name?}` form is the right one for both on the clients that implement it: an id that resolves to empty is simply not sent. The configurator emits exactly that for those clients.

### Client templates

Which placeholders a URL may use is a fact about the client that resolves them, not a preference, so the configurator's **Copy config** button picks the shape for you:

There are only two shapes of identity behind the list:

| Client | Identity parameters |
|---|---|
| AIOMetadata, Nuvio, Xperience | `tmdb_id={tmdb_id?}&imdb_id={imdb_id?}&stremio_id={id}&type={type}` |
| Bingecat, Discover+ | `tmdb_id={tmdb_id}&type={type}` |

Nuvio's pattern resolver takes AIOMetadata's placeholder set, optional `{name?}` form included, and Xperience builds Nuvio configurations, so all three end up in front of the same resolver and share the optimal URL. Bingecat and Discover+ reject `{name?}` at config time and won't save a URL containing one; as each gains the form it moves onto the optimal URL, and once neither is left every client shares a single one.

Nuvio adds one parameter the others have no equivalent for: `shape={shape}`, which its resolver fills with the shape the catalogue asked for. One URL then covers the portrait slot, the 16:9 slot and the Continue Watching backdrop, where every other client needs the landscape URL copied separately from the landscape view. The placeholder's presence is the switch — without it Nuvio replaces only portrait posters and leaves the other shapes with their own art — so **Copy config** emits it for Nuvio and the landscape choices ride along whichever way the preview is pointing. The settings the configurator keeps per shape travel twice on it — the portrait value under the plain name, the landscape one as `landscape_<name>` — so each layout renders with its own; the URL is the same whichever view you copy it from, and any value a render would pick anyway is left out.

Left-click copies for the client you last chose (the first press opens the menu, since there is nothing to repeat yet); right-click always opens the menu, as does a press-and-hold on touch. The choice is remembered per browser and is not part of the saved configuration, so an imported or shared config URL never carries someone else's client with it.

Required is the right form for the short template: `tmdb_id` is the only id those clients can send, so a title they have no TMDB id for has nothing to render from, and nulling the URL leaves the client's own poster in place. Nothing visible is lost by its missing `imdb_id` — `tmdb_id` identifies the title and the server reads the IMDb id back out of TMDB's metadata for the enrichment that needs one, though sending a concrete `imdb_id` does key the rating cache by IMDb id, sharing that row with every other client.

A build that doesn't understand `{name?}` leaves the placeholder in the URL verbatim; a literal `{tmdb_id?}` or `{imdb_id?}` is read as "no id" and the request renders from whatever else it carries, so that degrades to the same place rather than to a 400.

For a title with no IMDb id anywhere, TMDB artwork, logos, MDBList ratings, awards, sashes, genres and release status all work normally. Only the IMDb-keyed extras are unavailable: Metahub logo fallback, digital-release detection, and automatic stream-quality badges (an explicit `quality=` still works, which is why the Plex and Jellyfin sync scripts keep full badges either way).

### Without a TMDB key

A TMDB key is still the recommended setup — it is where textless posters, poster/logo language selection, the TMDB-keyed sashes (trending, release status) and TMDB's own rating come from. But an instance with no key on the server and none on the request can still render any title it has an IMDb id for, from Stremio's Cinemeta catalogue (`CINEMETA_ENABLED`, on by default): its Metahub background is cropped to portrait with the logo composited on top, exactly like a TMDB backdrop; `textless=false` serves its official one-sheet; landscape uses the background as shot. Title, year, genre, runtime, status, dates, episodes, cast and director come from the same document, so the genre canvas, the info sash, the director/cast sashes, movie release status (from Cinemeta's theatrical and disc dates and MDBList's digital date), the TV lifecycle and structure sashes (Airing / Ended / Cancelled, Mini Series, Binge Ready, New Season, Returning) and the Globe / Emmy / festival top-prize sashes (keyed on the TMDB id Cinemeta carries) all work; MDBList ratings, awards, keywords and quality badges are IMDb-keyed and unaffected. What a missing key does cost: no TMDB key loses the studio sash and the foreign-language sash, and without MDBList as well a movie's digital release is assumed after `CINEMA_ASSUMED_DIGITAL_DAYS` (60) unless the movieleaks feed has seen it sooner; no MDBList key loses the Oscar, cult, true-story and Metacritic sashes, the age rating, and the score unless `imdb_rating_source` / `tmdb_rating_source` supply one. Cinemeta also carries TMDB's id for most titles, which is what resolves an `imdb_id`-only request without a key. The configurator's search works without a key too: it searches Cinemeta's catalogue, picks titles by IMDb id, resolves the TMDB id from Cinemeta on selection, and previews from the IMDb id alone when there is none. A verbatim `{tmdb_id}` / `{tmdb_id?}` placeholder on a request is read as "no TMDB id", like the `imdb_id` one, so a client that has no TMDB id for a title and sends its IMDb id still renders.

The same path is taken when a key *is* configured but TMDB has no record for the IMDb id, and Cinemeta's art is tried as a last tier before the genre canvas when TMDB knows a title but has no artwork. A `tmdb_id`-only request without a key cannot be served — Cinemeta is IMDb-keyed — and is refused with a 400 saying so. The configurator's search and preview still need a TMDB key.

Append `&debug=1` to any poster URL to receive a JSON response with all computed metadata (score, genre, sash label, quality tokens, award data, matched cast/directors) instead of rendering the image. Useful for diagnosing unexpected sashes or missing ratings.

Append `&nocache=1` (requires `ACCESS_KEY` to be set and valid) to force a fresh render of a single title, bypassing the composite cache read and re-caching the result. Lets you refresh one poster without flushing the whole cache.

### Landscape posters

Pass `shape=landscape` for the dedicated 16:9 renderer:

```
https://yourdomain.com/poster?tmdb_id={tmdb_id}&type={type}&shape=landscape
```

`shape` also accepts `poster` as a synonym for `portrait`, which is the word Nuvio's `{shape}` placeholder substitutes; an absent, unrecognised or unsubstituted value renders portrait, and all of those spellings share one cached composite. The one value that is refused is `square`: there is no square renderer, and coercing it would push a 2:3 poster into a 1:1 tile, so it answers `400` and Nuvio's fallback keeps the addon's own artwork for those items.

Landscape mode uses backdrop artwork, keeps the top corners clear for client overlays, and combines the genre, year, rating, sash, and age rating into one bottom information band. Typography, spacing, and overlays are sized relative to the canvas height for consistent proportions.

Optional parameters control the landscape-specific choices:

- `landscape_art=textless|original` selects a language-neutral backdrop with a composited logo (the default) or the highest-ranked language-tagged backdrop with its own title treatment.
- `badge_pos=top_left|top_right|logo` places the age badge in a top corner or alongside the composited logo.
- Landscape defaults differ from portrait for the shared vignette settings: a bare `shape=landscape` URL renders with `vignette_poster_color_bottom=true`, two-tone on, local blending off, saturation 2.0, lightness 1.3, blur 1.0 and `landscape_color_link=badge_follows_vignette`. Pass any of them explicitly to override.
- `landscape_badge_scale=0.5–2.5` scales the info badge (type and padding together); `1.0` is the tuned size.
- `landscape_info_scale=0.5–2.0` scales the `Genre • Year • Score` line in the bottom-right corner; `1.0` is the tuned size. It grows up and to the left, and drops the genre first if it would reach the logo.
- `landscape_score_out_of_10=true` prints that line's score out of 10 with one decimal (`8.7`, `8.0`), and `10` for a perfect score. Off by default (`87`).
- `landscape_color_link=off|badge_follows_vignette|vignette_follows_badge` links the colour of the info badge and a tinted band (`vignette_poster_color_bottom=true`): the badge takes the band's colour, or the band takes the whole-frame colour the badge uses. Only the hue is shared — the band still darkens it, the badge still lifts it for legibility.
- `landscape_<name>` sets a landscape-only value for a setting both shapes read: `vignette_poster_color_bottom`, `vignette_color_ramp`, `vignette_color_local`, `vignette_color_style`, `vignette_color_saturation`, `vignette_color_lightness`, `vignette_color_blur`, `hide_genre`, `hide_rating`, `textless` and `sash_mode`. A landscape render reads `landscape_<name>`, then `<name>`, then its own default, and a portrait render never reads it — which is what lets one `shape={shape}` URL give each layout different values.

The configurator previews both shapes: the landscape button in the preview header switches the live preview to the 16:9 render, reveals the landscape choices under Core → Landscape (art, info size, score out of 10) and Sash → Badge (position, size, colour link), and makes **Copy config** copy the landscape URL, so a client with a landscape slot can be given the same settings as the portrait one. (Nuvio is the exception — its URL carries `shape={shape}` and is already both, so there is no separate landscape copy to take.) The landscape URL carries only the settings the landscape renderer reads (identity, language, sash priority and release-status filters, weights, Hide Genre, Hide Rating, Textless, and the bottom vignette colour with its sliders); the Rating, Logo and Quality tabs are hidden while it is showing, since nothing on them applies. Your portrait settings are kept — switching back restores them.

Landscape renders deliberately skip stream-quality fetching because this layout does not display quality tokens.

### Logo endpoint

`/logo` returns the best available title logo as its original PNG, using the same cached TMDB/Metahub selection chain as poster rendering:

```
https://yourdomain.com/logo?tmdb_id={tmdb_id}&type={type}&lang=en
```

Either id identifies the title, as on `/poster`. With both, `imdb_id` enables the Metahub fallback when TMDB metadata cannot supply one; without a TMDB key, Metahub is the only logo source. `access_key` and `tmdb_key` follow the same rules as `/poster`.

### Anime IDs (AniList / Kitsu)

Advanced metadata providers such as AIOMetadata can pass an anime-native id instead of `tmdb_id`/`imdb_id`, in which case the cover art, title, genres, air dates, status and community score all come from that provider:

```
https://yourdomain.com/poster?anilist_id={anilist_id}&type=series
https://yourdomain.com/poster?kitsu_id={kitsu_id}&type=series
```

No id conversion happens in either direction. If your client can't supply one of these ids, don't use these parameters — simpler providers group anime under TV series with `tmdb_id`/`imdb_id` and keep working exactly as before. Both bare (`12345`) and Stremio-prefixed (`kitsu:12345`) forms are accepted. When both params are supplied, AniList wins.

There is nothing to switch on: the configurator's [client templates](#client-templates) append the placeholder for the clients that can resolve an anime id and leave it off for the ones that can't.

```
?tmdb_id={tmdb_id?}&imdb_id={imdb_id?}&stremio_id={id}&type={type}
```

`{id}` is the raw Stremio / Nuvio meta id — `kitsu:7442` for a Kitsu-catalogue anime, `tt0903747` or `tmdb:1396` otherwise. PostersPlus reads the namespace off it and ignores anything that isn't an anime id, so the same URL serves your whole library. When it holds an IMDb id, that is also used as the title's identity, which shares its rating cache row with clients that send `imdb_id` directly.

Why `{id}` rather than `{kitsu_id}`: the per-namespace placeholder is empty for every live-action title, and an empty *required* placeholder makes the resolver abandon the whole URL — so it would have to be the optional `{kitsu_id?}` form, which Bingecat and Discover+ reject at config time. `{id}` is a plain placeholder, present in every build, and always populated, so it can never null the URL.

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
- `GET /admin/api/status`, `GET`/`PUT /admin/api/settings`, `POST /admin/api/restart`: the [admin dashboard](README.md#admin-dashboard)'s API, gated by `ADMIN_KEY` (header `X-Admin-Key`), not `access_key`.
- `GET /admin/api/watchlist`: the [watchlist marker](CONFIGURATION.md#watchlist-marker)'s snapshot state and, for SIMKL, whether the account is linked and any link code awaiting approval. `POST /admin/api/watchlist/simkl/link` issues a code now rather than on the loop's hourly re-prompt; `POST /admin/api/watchlist/simkl/unlink` forgets the account (revoking a V2 grant at SIMKL), drops the snapshot and re-renders the posters that carried the sash. All three back the dashboard's SIMKL panel and take the admin key like the rest.

---

## Award Sashes

Sashes display contextual metadata about a title - awards, festival recognition, notable cast or crew, and more. The first matching sash in the priority list is shown.

| Sash | Triggers on |
|---|---|
| Watchlist | The title is in the instance's configured watchlist (`WATCHLIST_SOURCE`, self-hosted only). First in the default order — inert on instances without one |
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
| Release Status | Title's current release state: Cinema / Streaming / Physical / Production for movies, Airing / Renewed / Ended / Cancelled / Production for TV. Airing means episodes are actually going out (one aired in the last fortnight, or the next is due within one); a show between seasons is Renewed when TMDB lists its next season, and shows no status when nothing is announced. With dates on, a series is dated the same way a movie is: `Dec 25 Premiere` for an unaired show, `Mar 4 Season 3` for a dated next season, `Jan 8 Returns` after a mid-season break. Lowest default priority; movies require an extra TMDB API call the first time. When TMDB has dated an unreleased movie, the sash shows the date and what it opens instead — `Oct 16 Cinema`, `Oct 23 Streaming`, or `Dec 2027 Cinema` a year or more out (in cinemas: the next digital/disc date; in production: the first release anywhere). `release_status_dates=false` keeps the bare status |

Sash priority order is configurable in the web configurator via drag-and-drop. The Primary Client selector sets recommended edge insets: Stremio TV, Nuvio, Plex, and Jellyfin use `0` for both bar and notch; Stremio Desktop/Web use `0.007` for the bar and `0.004` for the notch. Both sliders remain manually adjustable, and loading a preset preserves them. Existing URLs can override the notch with `sash_badge_inset` and the bar with `bar_bottom_inset`. Individual sashes can be disabled entirely with the ✕ button - disabled sashes are serialised as `-slot_name` in the URL (e.g. `&sash_priority=wins,cast,-trending`).

`sash_priority` also accepts a shorter *diff* form, written against the default order and marked by a leading `default` token: `-slot` removes a sash and `slot@N` moves one to position N (0-based). `&sash_priority=default,-cult,festival@0` promotes the festival sash and drops the cult one, in place of naming all thirty slots. The configurator emits whichever form is shorter, and the full list keeps working exactly as before - including an all-exclusions value, which still means every sash off rather than the default order minus those.

In Notch mode, the label is sized from the notch height, so `sash_badge_size_h` (Height) scales the text along with the badge. To tighten the empty space above and below the label *without* resizing it, use `sash_badge_pad` (Padding, default `1.0`, range `0.5`–`1.5`) — it trims only the vertical padding and leaves both the font and the badge width untouched. `sash_badge_inset` is a different control again: it shifts the whole notch up or down rather than reshaping it. Padding stops shrinking once the label's line height is reached, so low values crop the gap, never the glyphs.

---

## Ratings

Scores from multiple providers are normalised to a 0–100 scale and combined using configurable weights. Default weights use Letterboxd with Trakt fallback for movies, and Trakt (80%) and Rotten Tomatoes (20%) for TV. Weights are fully adjustable in the web configurator.

Weights renormalise over the sources actually present for a title, so a source with no score contributes nothing rather than dragging the average down. That makes the anime-only sources safe to weight: `myanimelist` (via MDBList, for anything with an IMDb id) and `anilist` / `kitsu` (only for titles requested by [anime id](#anime-ids-anilist--kitsu)) are inert on everything else. All three default to a weight of `0`.

### Anime Weights

Anime can score with its own weights. `anime_movie_weights` and `anime_tv_weights` take the same `source:weight` list as `movie_weights` / `tv_weights`, and apply to any title that has a `myanimelist`, `anilist` or `kitsu` rating — that is the whole test, no genre guesswork. MDBList returns a MyAnimeList score for the anime it knows, so a title requested by ordinary TMDB/IMDb id qualifies just as an anime-native request does.

Both parameters are opt-in. A URL that names neither scores its anime with the movie and TV weights exactly as before, so existing URLs are unaffected. In the configurator, the Weights tab's **Separate Anime Weights** toggle reveals the two groups.

The source lists are what MDBList actually returns for anime. Anime films carry every movie source. Anime series never carry a Metacritic critic score or a Roger Ebert review, so `anime_tv_weights` does not offer them; Letterboxd, which `tv_weights` omits, does appear for about half of anime series and is offered.

| Parameter | Sources |
|---|---|
| `anime_movie_weights` | `myanimelist`, `anilist`, `kitsu`, `letterboxd`, `trakt`, `tomatoes`, `popcorn`, `imdb`, `metacritic`, `metacriticuser`, `tmdb`, `rogerebert` |
| `anime_tv_weights` | `myanimelist`, `anilist`, `kitsu`, `trakt`, `tomatoes`, `popcorn`, `imdb`, `metacriticuser`, `tmdb`, `letterboxd` |

`debug=1` reports `is_anime` and the `rating_weights` a title was scored with.

---

## Poster Translations

Text rendered onto posters (genre labels and info-sash labels) can be localised. The language follows the request's **poster/logo language** setting.

To add a language, copy `languages/en.json` to `languages/<code>.json` (e.g. `fr.json`) and translate the **values** only; the keys are the canonical English strings and must stay unchanged. Translation is display-only with per-key English fallback: any missing key, malformed file, or language with no JSON falls back to English, so partial translations are safe.

Region-qualified files (`pt-br.json`) are supported and take precedence over the bare language (`pt.json`) for a `pt-br` request. Note that this differs from how region-qualified **artwork** is selected: a region file wins per *table*, not per *key*, so a `pt-br.json` that ships a partial `sashLabels` map does **not** borrow the missing entries from `pt.json` — they fall through to English. Region files should be full copies of `en.json`, not diffs.

> Note: poster text is drawn in Inter, which covers **Latin, Greek and Cyrillic**. It has no CJK, Arabic, Hebrew, Indic or Thai glyphs, and the image has no complex-text or right-to-left shaping, so those scripts will not render correctly. `tests/test_i18n_sash_vocabulary.py` fails on any character the font cannot draw.
