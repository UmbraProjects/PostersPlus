# Changelog

## Unreleased

### Backdrop crops find close-up faces

- When no face is found in a backdrop, it is checked again at half size. The
  face detector misses large close-ups at full size, so the portrait crop fell
  back to a guess and could frame a wall, a background or the back of a hood
  instead of the actor. Cached backdrop crops are redone.

### Faster, more accurate burned-in-text scans

- Textless posters are scanned for burned-in text in two passes: first at
  0.65x size, then at full size only when the small pass comes back clear but
  still found a title-sized block of text (about a quarter of scans). A scan
  takes roughly a third less time: ~85 ms instead of ~130 ms for a typical
  clean poster on a 4-core ARM host.
- Fixes a mismatch between detected text boxes and their confidence scores.
  RapidOCR sorted the boxes without their scores, so the size, position and
  confidence rules were often judging one box by another's score. The detector
  is now driven directly, which also drops some wasted preprocessing.
- On ~2,900 cached posters, checking every changed verdict by eye: 4 wrong,
  against 25 before. Stylised titles are caught more often, and signs,
  shirts and chalkboards in the scene are less often mistaken for a title.
- Cached scan results are redone under the new detector signature, so each
  textless poster is scanned once more as it is next requested.

### Landscape star beside the score

- Landscape gains a **Star beside score** switch (`landscape_score_star=true`),
  under Core → Landscape. The separator in front of the score becomes a star,
  as Clean mode has it on a portrait: `Genre • Year ★ 87`. It hides with Hide
  Rating, and works with the out-of-10 switch (`★ 8.7`).

### Unrated posters in Clean and Minimalist

- Clean mode shows just the genre when a title has no rating, instead of
  "★ N/A".
- Minimalist with append set to Year draws the rating separator light grey when
  there is no rating, instead of leaving a gap between genre and year.
- Cached composites now record the drawing revision that made them and a few
  facts about what they show (for now, the score). A drawing change that only
  affects some posters re-renders just those, rather than the whole cache.
  Posters cached before this update have no facts, so unrated ones keep the
  old look until they expire (`COMPOSITE_CACHE_TTL`).

### Backdrop for fake textless posters

- `TEXTLESS_BACKDROP_FALLBACK` (Text detection, on by default): when a
  poster TMDB tags as textless turns out to have its title burned in, the
  poster is swapped for a crop of the title's backdrop with a logo on it,
  instead of being served as-is without one. It needs a logo to put on the
  crop (or a `textless=true` request) and a crop that scans clean; otherwise
  the poster is kept as before.
- It costs about half a second on the first render of an affected title
  (backdrop download, crop, one more text scan). Later renders reuse the
  cached crop and scan. Titles above `TEXTLESS_DETECTION_MAX_VOTES` get the
  swap once the background scans have finished.
- Posters already cached with a fake textless poster keep it until they
  expire (`COMPOSITE_CACHE_TTL`); turning the setting off re-renders them.

### Hide Year, and an optional Admin link

- **Rating → Labels → Hide Year** (`hide_year=true`) drops the release year
  from the label in every rating mode and from the landscape info strip, and
  can be set per shape (`landscape_hide_year`) like Hide Genre. Minimalist's
  Year mode shows the score only as the colour of the separator before the
  year, so with the year hidden it prints the score instead.
- `SHOW_ADMIN_LINK` (Access & serving, off by default) adds an Admin link to
  the configurator's header. It stays hidden while the dashboard is disabled,
  so turning it on without an `ADMIN_KEY` shows nothing.

### PublicMetaDB watchlist

- `WATCHLIST_SOURCE=pmdb` reads the Watchlist sash from a
  [PublicMetaDB](https://publicmetadb.com) account's watchlist, with a key
  from Settings → API set as `PMDB_API_KEY`. `PMDB_LIST_ID` reads another
  list instead, including someone else's public one.
- PMDB lists carry only TMDB ids, so the sash appears wherever the title's
  TMDB id is known — with a server or client TMDB key — and not on
  Cinemeta-only renders.

### Faster poster rendering

- A poster takes about 25% less CPU to render the first time and about 45%
  less when its art has been rendered before (a different client or setting,
  a trending change, a cache refresh). On a 4-core server a cold catalog grid
  now renders about twice as fast. Rendered posters are unchanged pixel for
  pixel.
- WebP posters are encoded with less compression effort: about half the time
  for files about 1.5% larger.
- The faces the tinted vignette avoids are detected once per image and
  remembered, instead of on every render.
- The vignette's blur, the text-title fitting and the notch sash reuse work
  that doesn't change between renders.

### Trending catalogs addon has a logo

- The addon's manifest now points to a Posters+ Trending logo, so it no
  longer shows up blank in Stremio's addon list. Its address comes from
  `PUBLIC_URL` when set, otherwise from the request, like the poster links.

### Reliability and hardening

- Errors reported on `/server-caps` and `/stats` show less detail.
- Debug pages encode the parameters they echo back.
- A custom top or bottom gradient's height and opacity are range-checked like
  every other setting, and number parameters no longer accept `nan`.
- An access key with non-ASCII characters is refused with a 403 rather than a
  500.
- The admin dashboard refuses `nan` and `inf` in number settings. Before, they
  were saved and broke whatever used them after a restart.
- Stream scraper requests are logged by host only.
- Behind a reverse proxy, set `FORWARDED_ALLOW_IPS` to the proxy's address so
  the admin dashboard sees each visitor's own address (now documented). The
  admin lockout table no longer grows without limit.
- New optional `PUBLIC_URL` setting for the address used in the trending
  addon's poster links. Without it they still come from the request's headers.
- The image is about 80 MB smaller, keeps its code read-only to the app, and
  startup no longer re-owns every file in the cache.
- A poster request riding on another request's render could wait forever if
  that render was cancelled or failed early. It now falls back to rendering
  the poster itself.
- Changing `ACCESS_KEY` no longer breaks the re-rendering of cached trending
  and watchlist posters. The key is no longer stored with them.
- `/debug/canvas` now caches what it renders and renders off the event loop.
- Unknown parameters (`&x=…`) no longer create a separate cached poster, and
  URLs that differ only in spelling (`0.3` / `0.30`, `1` / `true`) now share
  one. Cached posters are re-rendered once after updating.
- Cache reads and writes on the poster path no longer run on the event loop,
  and a change in the trending list clears the old posters in one pass over
  the cache instead of one pass per title.
- Upstream JSON is now fetched compressed.

### Configurator no longer adds a stale access key

- The configurator remembered the access key in the browser and fell back to
  it when the page's URL had none. After the server's `ACCESS_KEY` was removed,
  that old key kept going into every copied URL. The key now comes only from
  the page's own URL, and any copy saved by an earlier version is cleared.
- A key left in a bookmarked configurator URL after the server stopped
  requiring one is dropped as well, so it no longer ends up in copied URLs.

### Weight sliders take typed values

- Clicking a rating weight's percentage now opens it for typing, like every
  other slider in the configurator. Typed weights update the total and the
  URL straight away.

### API keys no longer cost AIOMetadata its posters

- A TMDB or MDBList key entered in the configurator put `{tmdb_key}` /
  `{mdblist_key}` in the copied URL. AIOMetadata drops the whole URL when a
  placeholder it can't fill is required, so anyone without that key in
  AIOMetadata (or who removed it later) lost every poster. AIOMetadata and
  Xperience URLs now use the optional `{tmdb_key?}` / `{mdblist_key?}`, and
  a missing key falls back to the server's own. Bingecat and Discover+ keep
  the plain form, since they reject `{name?}`.
- A key parameter that arrives still holding its placeholder is read as no
  key, not sent to TMDB or MDBList as one.
- An MDBList key in the URL that has used up its daily quota now hands over
  to the server's MDBList key, when one is set, until the quota resets.
  Before, that user's ratings stopped until the next day.

### Trending lists get a second attempt

- A trending list that couldn't be read (TMDB, a custom source or AniList) is
  now read once more after a short pause. If that fails too, it is left for
  five minutes instead of being retried on every poster request.
- Posters drawn while their list was unreadable are kept for those five
  minutes instead of seven days, so a title's rank comes back as soon as the
  list does. The scheduled refresh also retries within the hour when a list
  has never been read.

### Trending catalogs addon

- A Trending row in your metadata addon is built from its own copy of the
  list, so its order rarely matched the "#N Today" on its posters.
  PostersPlus now serves the lists behind the Trending sashes as a Stremio
  addon with Trending Movies, Series and Anime catalogs. It is on by default
  (`TRENDING_CATALOGS_ENABLED=false` turns it off). Import it into AIOMetadata (cache time 0) and each row's order
  matches its labels. The configurator shows the manifest URL.
- Trending Anime is AniList's trending list. With the addon on, a poster
  requested with an AniList id shows its rank on that list rather than its
  TMDB TV rank.

### Trending ranks change together

- Two posters could both read "#10 Today". Each one was cached for a day from
  when it was drawn, so posters drawn before the daily refresh stayed in
  apps beside ones drawn after it. Every poster showing a rank now expires
  exactly when the ranks refresh, and apps are told the same.
- With TMDB as the source, the scheduled refresh (`TRENDING_FETCH_TIME`) never
  actually fetched new ranks. It redrew the trending posters with the old
  ones instead. It now refreshes the ranks and then redraws those posters.
  Without a fetch time, the refresh happens 24 hours after the previous
  one rather than on a clock that restarts with the container.
- Cache warming no longer replaces ranks that are still current.
- A title TMDB listed on two pages no longer leaves a rank number empty and
  pushes the titles around it a place out.
- Trending anime posters are now redrawn and cleared from memory like any
  other title when their rank changes.

### The configurator remembers your settings on reload

- **Minimum Quality to Display** set to HD Web came back as the strictest
  tier but one after a reload, so most titles lost their quality badge in
  the preview and in copied URLs. A few other settings left at the server's
  default reverted the same way. Settings are now saved in full. Anything
  already lost needs setting once more.
- **Badge Size** no longer resets to the mode's default on every reload or
  when a landscape preset is loaded. Changing the display mode still picks
  that mode's size.
- With the portrait rating hidden, your rating weights were left out of the
  saved settings and out of Nuvio's `{shape}` URL, so landscape posters were
  scored with the server's weights. They're kept now.

### Shows waiting to premiere stay unreleased

- TMDB sometimes marks a show "Returning Series" before its first episode
  airs. Those shows read "Season 1" as if renewed, and Hide Rating Until
  Released showed their score. They now read like any show waiting to
  premiere ("Sep 30 Premiere"), and the score stays hidden until an episode
  has aired.

### Monster resolves as one series

- IMDb lists Monster (`tt13207736`) as one anthology with a season per story.
  TMDB lists each story as its own show, so the IMDb id sometimes failed to
  resolve and the metadata provider's plain poster showed instead. Monster
  now renders as its newest story that has premiered: Ed Gein today, and
  Lizzie Borden from the day it airs.
- A TMDB id now decides the title. An IMDb id sent beside it is kept only
  when TMDB links that TMDB id to it, so a Monster story asked for by its
  TMDB id gets its own rating, sash and quality, not the whole anthology's.
  An IMDb-only request for Monster renders exactly like its newest story's
  TMDB id. Ordinary titles are unaffected.

### Importing a URL keeps you on this instance

- Importing a URL copied from another instance no longer carries that
  instance's address or access key over. Copy config now always points at
  the instance you're using, with its own key, so switching instances is
  just a matter of importing your old URL.

### Landscape score out of 10

- Landscape gains the **Display score out of 10** switch portrait already had
  (`landscape_score_out_of_10=true`), under Core → Landscape. The score on the
  `Genre • Year • Score` line reads `8.7` rather than `87`, and a perfect
  score reads `10`. It hides with Hide Rating, as the portrait switches do.

### QualiCache trust defaults to medium

- `QUALICACHE_MIN_TRUST` now defaults to `medium`, so releases from unknown
  groups count as well as the known ranked ones. Set it to `high` to keep the
  old behaviour.

### Hide Rating Until Released

- A new **Hide Rating Until Released** switch (`hide_unreleased_rating=true`)
  hides the score only on titles nobody can have watched yet: a film not out
  in cinemas or anywhere else, or a series that has not aired an episode.
  Trakt and IMDb accept ratings for announced titles, so a show years from
  air could print a score from a handful of votes. Everything else keeps its
  score, and a hidden one comes back by itself once the title is out: the
  poster is re-rendered on the release-status schedule.
- It uses the same release status as the info sash, but doesn't need the
  status sash turned on, and turning it on doesn't add a sash or greyscale
  the art. Films need a TMDB key, or Cinemeta, for their release dates.
  Without either, a film's score is always shown.
- A series TMDB still lists as "In Production" keeps its score once an
  episode has aired. Films that have only had a festival premiere still
  count as unreleased.

### One Nuvio URL for both shapes

- Nuvio's custom poster pattern gained a `{shape}` placeholder, which its
  resolver fills with the shape the catalogue asked for. **Copy config** now
  emits `shape={shape}` for Nuvio, so a single URL covers the portrait slot,
  the 16:9 slot and the Continue Watching backdrop — where every other client
  still needs the landscape URL copied separately from the landscape view.
  The placeholder's presence is the switch on Nuvio's side: without it, it
  replaces only portrait posters and leaves the other shapes alone.
- Because that URL is resolved for both layouts, it is not filtered to either:
  the portrait-only settings ride along and the landscape choices are emitted
  whichever way the preview is pointing — the URL is the same from either view.
- The settings the configurator keeps per shape — the bottom vignette tint and
  its sliders, Hide Genre, Hide Rating, Textless and the sash mode — now also
  take a `landscape_`-prefixed parameter (`landscape_hide_rating`,
  `landscape_vignette_poster_color_bottom`, …) that only a landscape render
  reads. A dual URL carries both values, so each layout gets its own: with one
  parameter between them, the portrait value would have reached the 16:9 slot
  too, and a URL copied with the portrait defaults would have rendered
  landscape without its tinted band. A landscape render reads the prefixed
  parameter, then the plain one, then its own default, so `shape=landscape`
  URLs written before the split render unchanged. The configurator writes the
  prefixed form for landscape URLs, leaves out any value a render would pick
  anyway, and imports a dual URL into both shapes' settings.
- `shape` accepts `poster` as a synonym for `portrait` — Nuvio's word for the
  2:3 slot. It already rendered correctly, because an unrecognised shape fell
  through to the portrait default, but the spelling reached the composite
  cache key: `poster`, `portrait`, an unsubstituted `{shape}` and no shape at
  all were four cache entries for one render, and are now one.
- `shape=square` answers `400` instead. Nuvio asks for it when an addon
  declares `posterShape: "square"` on a catalogue item, and there is no square
  renderer to answer with — a portrait poster squashed into a 1:1 tile is
  worse than not answering, and the error hands the item back to Nuvio's
  fallback, which restores the addon's own artwork.

### Series release status and dates

- A series' release status came from TMDB's status word alone, and TMDB's
  "Returning Series" means "not declared finished", not "on air" — so The Last
  of Us (last episode May 2025) and Severance (March 2025) both read **Airing**.
  The status now comes from the episode data TMDB already sends with the
  series, at no extra API call: **Airing** only while episodes are going out
  (one in the last fortnight, or the next due within one); **Renewed** when
  TMDB lists the next season; and no status at all for a show returning in
  name only, so a lower sash gets the space instead of a claim.
- With release dates on, a series is dated the way a movie is: `Dec 25
  Premiere` for a show that hasn't aired, `Mar 4 Season 3` for a dated next
  season, `Jan 8 Returns` after a mid-season break. The weekly next-episode
  date is left off on purpose — it would change every week and crowd out
  better sashes. Translated in every shipped language; where the window comes
  before the day the season uses the short form (`T3 4 mar`), so the two
  numbers don't run together.
- New **Renewed** sash slot, last in the default order so saved sash orders
  keep their positions. A saved list that names Airing gets Renewed right
  behind it (add `-renewed` to leave it out), since those shows used to read
  Airing.

### A leak post no longer outranks an announced digital date

- The Odyssey (2026) showed "Streaming" while still in cinemas. Its IMDb id
  had been posted to r/movieleaks in August — a knock-off film posted under
  Nolan's id — and the digital-release cache takes every IMDb id in every post
  at face value, overriding TMDB's status. TMDB meanwhile listed the digital
  release for November. The feed is there to catch releases that beat TMDB's
  date, and real ones beat it by days, so a leak now counts unless TMDB
  schedules the digital release more than 14 days out — for both the
  release-status sash and the "New" sash. With no TMDB date to compare (no
  key, or the lookup failed) it is trusted as before. Composites already
  cached with the wrong status re-render when their row expires (at most 30
  days, sooner if the title's next release date comes first).

### Anime from clients that only send the anime id

- Anime landscape posters requested through Nuvio's own `{shape}` URL came
  back as the genre canvas, where the same titles through AIOMetadata (as the
  Nuvio HTPC fork uses) rendered properly. The difference was the ids: Nuvio's
  resolver turns a `kitsu:7442` catalogue item into a Kitsu id and nothing
  else, while AIOMetadata sends `tmdb_id` and `imdb_id` alongside it. The anime
  providers ship one cover image and no backdrop or logo, so without a TMDB id
  the landscape render had nothing to draw on, and portrait anime went without
  a logo too.
- New `ANIME_ID_MAP_ENABLED` (on by default) fills in the TMDB and IMDb ids an
  anime request didn't send, from Fribb's community Kitsu/AniList mapping — the
  same data AIOMetadata resolves from — downloaded daily into a local table. A
  Kitsu-only request now renders the same poster, byte for byte, as the
  AIOMetadata request for the title. Ids the client did send are kept, the TMDB
  id is only used when it is the kind being rendered (movie or series), and
  the art and metadata still come from the anime provider. A sequel season
  maps to its parent show, as it does in AIOMetadata, so its logo and backdrop
  are the show's.

### Hide Rating

- **Rating → Labels → Hide Rating** (`hide_rating=true`) takes the score off
  the poster in whichever display mode is drawing it, alongside the existing
  Hide Genre. Every mode loses its own representation of it: the Rating Bar
  mode's accent bar, Clean's `★ 87`, Minimalist's score segment, and Bar
  mode's `★ 87` along with the fill in the two Rating Bar styles, which fall
  back to plain Frosted and Pure Black rather than drawing an empty stripe.
- A score shown as a colour is still a score, so those go too. Minimalist's
  Year layout carries the rating in the separator between the genre and the
  year; with the rating hidden that separator drops back to the text colour,
  and — having nothing left to encode — it is drawn even for a title with no
  score, where before the slot was left empty.
- The controls that only style a score go with it: the Glow group, the Colour
  Palette, the out-of-10 switches, Minimalist's Rating Separator and Bar
  mode's Rating Bar Colour. The Bar mode Style you picked is kept while they
  are hidden, so turning Hide Rating back off gives the Rating Bar back. Like
  Hide Genre the switch is kept per shape, and in landscape
  (`landscape_hide_rating`) it drops the score from the 16:9 info strip.

### Landscape in the configurator

- The preview header has a landscape button. It switches the live preview to
  the 16:9 render, shows the landscape choices under Core → Landscape, and
  makes Copy config copy the `shape=landscape` URL (the shape is saved with
  the rest of the settings and round-trips through Import; presets leave it
  alone).
- In landscape view the Rating, Logo and Quality tabs are hidden, along with
  every other control the landscape renderer does not read (vignette levels
  and top-band toggles, fallback style, sash and notch styling), so nothing
  on screen can be reported as not working there. Hide Genre, Hide Rating
  and Textless, which it does read, move into the Landscape group meanwhile. The landscape
  URL carries only the settings that apply; the saved configuration still
  carries everything, so the portrait settings survive a reload.
- Landscape has defaults of its own for the settings it shares with portrait:
  a bare `shape=landscape` URL renders the tinted band (two-tone, saturation
  2.0, lightness 1.3, blur 1.0, local blending off) with the badge matched to
  it, the textless art with the logo, the badge top-left and the sash shown.
  An explicit parameter still wins, and portrait defaults are unchanged. The
  configurator keeps a separate set of those values per shape, so switching
  the preview never disturbs the other shape's settings, and its Sash mode
  reads Hidden | Shown in landscape — there is no diagonal sash or notch
  there, only the badge.
- Bottom Colour from Poster can actually be switched off in landscape. The
  configurator only ever wrote the toggle's on state, so off was an absent
  parameter — the server's default — and in landscape that default is on.
  Both vignette colour toggles are now written in both states (and still
  dropped from the URL when they match the shape's default).
- Three landscape presets (tinted band, dark band, original art). The preset
  gallery is grouped by shape with the shape being previewed first, a preset
  switches the preview to its own shape, and it only touches the controls
  that shape reads — a landscape preset leaves the portrait configuration
  alone and vice versa. Import from a config URL follows `shape=landscape`.
- Added `landscape_badge_scale` (Sash → Badge → Badge Size, 0.6–2.0):
  scales the landscape info badge's type and padding together, for a shelf
  watched from across a room.
- Added `landscape_color_link` (Sash → Badge → Colour Link): the info
  badge takes the tinted band's colour, or the band takes the whole-frame
  colour the badge uses. Hue only — the band still darkens it and the badge
  still lifts it.
- Landscape renders of anime ids (AniList / Kitsu) drew on the genre canvas:
  the providers ship one cover and no backdrop. They now use TMDB's backdrops,
  already fetched for the logo list, when the request carries a `tmdb_id` and
  a TMDB key is available; the cover remains the portrait art.
- The landscape band no longer shows a line where it starts. Its alpha ramp
  was the portrait curve, which begins at full slope; on a short canvas the
  eye reads that kink as a rule across the art, and no height or strength
  setting could hide it. The band is now a smoothstep — flat at its top edge
  and at its peak — with a gamma that gathers the darkness under the text
  row, so it can be tall enough to fade properly (0.45 h) without reaching
  visibly into the picture.
- The logo's height is no longer tied to the band: it used to be capped at
  the band's top edge, so a shallower band shrank every stacked logo with
  it. The logo keeps its own 0.30 h ceiling and stands on its drop shadow.
- Cached landscape composites are re-rendered once (render cache version 7).
- The landscape logo's drop shadow was blurred on a canvas exactly the logo's
  size, so it clamped at the edges and came out as a straight-edged slab
  around any logo whose ink reached its bounding box. It is now built on a
  padded canvas and reads as a soft pool under the wordmark.
- The landscape info pill is about 15% larger and casts a soft drop shadow,
  so it stands off bright art in the top corners the way the logo does; the
  genre / year / score strip casts a tighter one for the same reason, with
  the letters punched out of it so the translucent type does not darken,
  and its ink lifted a step.
- Landscape prefers a wide logo: within the language bucket that wins, a
  logo with an aspect ratio of 2.0 or more is ranked ahead of stacked or
  square ones, which the wide, short logo box could only draw small. Votes
  still decide among the wide ones, and portrait selection is unchanged.
- The landscape band's poster-colour tint now honours Blend Into Nearby Art
  the way the portrait bottom vignette does, and samples at the finer cell
  counts the wider canvas was meant to use, so the low end of Blur follows
  the art instead of going coarse.

### Copy config picks your client

- **Copy config** copies the URL in the shape your client can actually
  resolve. The first press opens a short menu — AIOMetadata, Nuvio, Xperience,
  Bingecat or Discover+ — and after that a left-click copies for that client
  again while a right-click reopens the menu to pick another (press and hold on
  touch, which has no right-click). The choice is remembered per browser,
  marked with a tick in the menu, and named in the button's tooltip along with
  where to paste the result.
- AIOMetadata, Nuvio and Xperience get both core ids in the optional form —
  `tmdb_id={tmdb_id?}&imdb_id={imdb_id?}`. A *required* placeholder the client
  cannot fill makes the resolver drop the whole URL, and both ids hit that in
  practice: TMDB has no IMDb link for some titles, and a client's catalogue
  often has no TMDB id for one. Since either id renders on its own now, an
  unresolved one is simply not sent instead of costing the poster. This also
  brings the IMDb-keyed extras (Metahub logo fallback, digital-release
  detection, automatic quality badges) straight from the template.
- That is one URL for three of the five clients: Nuvio's resolver takes
  AIOMetadata's placeholder set and Xperience builds Nuvio configurations, so
  all three share it. Bingecat and Discover+ reject `{name?}` at config time
  and keep `tmdb_id={tmdb_id}` in the required form — correct there, since it
  is the only id they send and a title they cannot resolve one for has nothing
  to render from anyway. As each gains the form it moves onto the optimal URL,
  and once neither is left every client shares a single one.
- The **Anime IDs** toggle is gone. Whether `stremio_id={id}` can be resolved
  is a fact about the client rather than a preference, so it now rides on the
  template: on for the clients that can substitute it, off for the ones that
  cannot. Existing URLs are unaffected, and the client choice is not
  part of the saved configuration, so a shared or imported config URL never
  carries someone else's client with it.

### Identity

- `/poster` and `/logo` accept either id. An `imdb_id`-only request (or a
  `tt…` `stremio_id`) is resolved to a TMDB id through TMDB's `/find` and
  persisted, so it renders exactly as a request carrying both ids would and
  shares its composite cache entry with one. TMDB's type wins when it
  disagrees with the request. Missing both ids is a 400 that names both.

### MDBList burst limit

- MDBList throttles per IP as well as per key: a burst of calls within a few
  seconds gets 503s, then `429` with `Retry-After: 10` for every key on the
  address, with the daily quota untouched. That 429 was handled as a key
  problem — the key was cooled down, a sibling key was tried (and refused
  too), and one without `Retry-After` parked the key for an hour with
  thousands of calls left. A burst 429 or 503 now pauses all MDBList calls
  from the process for `Retry-After` (10 s when absent) and touches no key;
  a live render waits the pause out and retries once, so the poster is
  complete and cacheable rather than provisional. Quota 429s keep the
  sleep-until-reset-and-rotate behaviour.
- Added `MDBLIST_MIN_INTERVAL` (default `0.2` s): a minimum spacing between
  MDBList request starts shared by live renders and the cache warmer. With
  `MDBLIST_CONCURRENCY=3` an unpaced cold catalog warm reached ~10 calls/s,
  which is where the limit was hit. `/stats` and the admin overview show the
  pacing rate and any burst pause in progress.

### Cinemeta fallback

- Added Stremio's Cinemeta catalogue as a key-less, IMDb-keyed art and
  metadata source (`CINEMETA_ENABLED`, on by default). An instance with no
  TMDB key — on the server or the request — renders any title it has an IMDb
  id for: the Metahub background cropped to portrait with the logo on top,
  the one-sheet in original-art mode, the background as shot in landscape.
  Title, year, genre, runtime, status, cast and director come from the same
  document; MDBList ratings, awards and quality badges are unaffected.
  Cinemeta's own TMDB id is what resolves an `imdb_id`-only request without
  a key.
- The same path carries a title TMDB has no record for when a key is
  configured, and Metahub art is tried as a last tier before the genre
  canvas when TMDB knows a title but has no artwork. Art availability is
  probed on the CDN (Cinemeta names image urls for every title) and cached.
- A `tmdb_id`-only request without a key is still refused — Cinemeta is
  IMDb-keyed — with a 400 that says an `imdb_id` would render.
- The configurator searches without a TMDB key: `/search` falls back to
  Cinemeta's catalogue (IMDb-keyed rows in TMDB's shape), a new
  `/resolve-tmdb` finds the TMDB id on selection (TMDB's `/find` with a key,
  Cinemeta's `moviedb_id` without), and the preview renders from the IMDb id
  alone when there is none. The Presets' ignored inset parameters are gone.
- More sashes survive without a TMDB key. A Cinemeta-spined movie takes its
  release status from Cinemeta's theatrical and disc dates plus MDBList's
  `released_digital` (remembered whenever a rating is fetched), so a film
  that has gone digital reads Streaming rather than Cinema; without MDBList
  as well, `CINEMA_ASSUMED_DIGITAL_DAYS` (default 60) treats a theatrical
  release older than that as Streaming, and the movieleaks cache still
  supplies Streaming earlier,
  a series gets its season and episode structure from Cinemeta's episode
  list (Mini Series, Binge Ready, New Season, Returning, Season Finale), and
  a Trending sash backed by a custom MDBList source no longer waits on a key
  it never needed.
- A verbatim `{tmdb_id}` or `{tmdb_id?}` on `/poster` and `/logo` is read as
  "no TMDB id", as `{imdb_id}` already was, so a client that leaves the
  placeholder in for a title it has no TMDB id for renders from the IMDb id
  instead of getting a malformed-id 400.

### Localization

- Added Polish (`pl`) poster-output translations, contributed by @skoruppa.
- Added poster-output translations for every configurator language the label
  font can draw: German, Dutch, Swedish, Danish, Norwegian, Finnish, Greek,
  Czech, Slovak, Hungarian, Romanian, Croatian, Russian, Ukrainian, Bulgarian,
  Turkish, Vietnamese, Indonesian, Malay, Swahili and Afrikaans. These are
  machine-assisted first drafts; corrections from native speakers are welcome.
  Japanese, Korean, Chinese, Arabic, Persian, Urdu, Hebrew, Hindi, Bengali,
  Tamil, Telugu and Malayalam stay untranslated: Inter has no glyphs for those
  scripts, and the image has no complex-text shaping to lay them out.
- French, Italian and Portuguese gained the 27–29 language-name sash labels
  they were missing, which previously rendered in English.
- The landscape badge uppercases its label the way the language does: Turkish
  `i` becomes `İ` rather than `I`, and Greek drops the tonos in capitals.
- The configurator marks every fully translated language with ★.

## v1.2.0 - 2026-09-20

This release is compared with `v1.1.0`.

### Highlights

- Added anime-native poster requests through AniList and Kitsu, with separate
  anime rating weights for MyAnimeList, AniList and Kitsu scores.
- Added a 16:9 landscape poster layout, poster-coloured vignettes, and frosted
  elements that match the painted vignette colour.
- Made IMDb ids optional: `tmdb_id` is the identity, so titles TMDB has no IMDb
  link for now render with ratings and sashes instead of failing.
- Added QualiCache as a quality source, so poster rendering answers from a
  shared cache instead of waiting on a scrape.
- Added MDBList-free rating inputs: TMDB's own vote average and IMDb's daily
  dataset, each usable as the primary source or as a fallback when MDBList has
  no value.
- Added background cache warming for trending, popular, and custom-catalog
  titles, with quota-aware MDBList spending, plus custom trending sources.
- Fixed festival sashes naming a top prize the film did not win; top prizes are
  now checked against Wikidata-built lists verified edition by edition.
- Redesigned the configurator with new presets, row tooltips, a title-link
  menu, and generated URLs about a third of their previous length.
- Hardened the server for cold-catalog bursts: fresh renders queue behind an
  admission cap, and the healthcheck no longer leaves zombie processes.
- Added Brazilian Portuguese translations and translated the remaining sash
  vocabulary in every shipped language.

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
- The release-status sash now shows the date an unreleased movie arrives, and
  where, when TMDB has published one — `Oct 16 Cinema`, `Oct 23 Streaming`, or
  `Dec 2027 Cinema` when it is a year or more away — instead of a bare
  `Cinema` / `Production`. In cinemas the date is the next digital or disc
  release; in production it is the first release anywhere. Translated in every
  shipped language, and switchable off with the new Release Status: Show Date
  toggle (`release_status_dates=false`).

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
- QualiCache `BLURAY` and `WEBRIP` answers now fold into the silver Web badge.
  Older shows whose best trusted release is an encode rather than a remux or
  WEB-DL (*Lost*, *Futurama*) previously showed no quality badge at all, since
  the source token was dropped and a resolution alone is never drawn. Only a
  true remux keeps gold. Tokens with no PostersPlus equivalent (`8K`, `1440P`,
  `720P`, `SD`, `HDTV`) are still dropped rather than approximated.
- Quality backend selection now runs through one dispatcher instead of being
  repeated at each call site. `/status` reports the active backend as
  `quality_source`.
- The Quality Bookmark badge mode now seeds Badge Size at 30 rather than 16.
  At 16 the corner mark was barely visible at the poster sizes most clients
  render at, so the mode looked like it hadn't worked. The right value varies
  by client, which the mode's tooltip now says.

### Ratings

- Added separate rating weights for anime. `anime_movie_weights` and
  `anime_tv_weights` take the same `source:weight` list as `movie_weights` /
  `tv_weights` and apply to any title carrying a MyAnimeList, AniList or Kitsu
  rating — MDBList supplies the MyAnimeList score for anime it knows, so this
  covers anime requested by ordinary TMDB/IMDb id as well as the anime-native
  path. Both are opt-in: a URL naming neither scores its anime with the movie
  and TV weights exactly as before, so nothing changes for existing URLs. The
  source lists are what MDBList actually returns for anime: anime films carry
  every movie source, while anime series never carry a Metacritic critic score
  or a Roger Ebert review (dropped) but do often carry Letterboxd (added). The
  Weights tab gains a **Separate Anime Weights** toggle that reveals the two
  groups, and `debug=1` now reports `is_anime` and the `rating_weights` used.
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
- Renamed the award sash labels so winners and nominees no longer share the
  same text: "Best Picture" / "Golden Globe" became "Oscar Winner" / "Oscar
  Nominee" and "Globe Winner" / "Globe Nominee", in every shipped language.
  A new `sash_winner_star` toggle prefixes winners with a star, replacing the
  old heuristic that guessed from the shared label.
- Fixed movies wearing TV awards. TMDB movie and TV ids are separate
  namespaces, but the Emmy and Golden Globe id lists were searched as one, so
  *Back to the Future* (movie/105) inherited *Sex and the City*'s Emmy and
  *Donnie Darko* (movie/141) inherited *Cheers*'s. Lookups now use the film or
  TV lists by media type, and cached rating rows rebuild their Globe / Emmy
  labels on read so existing rows correct themselves.
- Fixed unreleased movies reading `Streaming`. TMDB flips a film to `Released`
  ahead of its first date, and limited-theatrical and festival-premiere dates
  were not being read at all, so a title like *You Can See Everything* (two
  festival premieres, limited release in October) had no dates to contradict
  the flag. Limited releases now count as theatrical, a future premiere counts
  as proof the film is not out, and cached rows that recorded no dates are
  re-fetched once.

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
- A burst of uncached poster requests — a cold catalog or tabbed grid asking
  for 50+ posters in a second — no longer fails en masse with `PoolTimeout`.
  Fresh renders now queue behind a per-worker admission cap
  (`POSTER_RENDER_CONCURRENCY`, default `8`); cache hits and requests coalesced
  onto an in-flight render are never held back. The upstream connection pool is
  sized from that cap, and a request waits up to 10 seconds for a connection
  rather than 5. `/stats` reports `renders_active`, `renders_queued` and
  `render_slots`.
- Cache warming no longer drains a free MDBList key's daily quota. MDBList's
  limit is 1,000 requests per key per day (more on paid tiers), not a burst
  limit, and the default `CACHE_WARM_MDBLIST_BUDGET` of 500 took half of it in
  one cycle — leaving live poster requests to 429 for the rest of the day.
  Every MDBList response reports the remaining quota, and the warmer now
  reads it: it stops spending a key once its remaining requests fall to
  `CACHE_WARM_MDBLIST_RESERVE` (default `300`), moving to `MDBLIST_API_KEY_2`
  when that key still has room, and never drags live traffic off a key that
  is merely at its reserve. A quota 429, which carries no `Retry-After`, now
  parks the key until MDBList's own reset time instead of retrying hourly
  against a key that is dead until midnight UTC. `/stats` reports each key's
  `daily_limit`, `daily_remaining` and `quota_reset_at`.
- The container no longer accumulates zombie `python3` processes under load.
  The Docker healthcheck ran through a shell, so a probe that overran its 5s
  timeout on a busy host left an orphaned `python3` that nothing reaped. The
  probe now runs without a shell, imports less, and gets 10s; `tini` is PID 1
  so any orphan is reaped regardless.

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
- Replaced the ten shipped presets with a new set — tinted minimalist,
  colour-matched bar/notch/sash, and rating-bar variants — with WebP
  screenshots.
- Fixed a rating or sash text colour that, once typed, came back after every
  container rebuild even after being cleared. The reset-on-load skipped hex
  text boxes and an imported URL that omitted the parameter left the old value
  in place; both now clear to the server default.

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
- Added the 78th Emmy (2026) winners and nominees to the award sash data.
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
