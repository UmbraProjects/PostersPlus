# PostersPlus

A self-hosted poster service that puts ratings, award sashes, quality badges and title logos onto movie and TV artwork. It works with AIOMetadata, Nuvio, Bingecat, Plex, Jellyfin, and anything else that can pass an IMDb, TMDB, AniList or Kitsu ID.

Not self-hosting? Use one of the public instances hosted by [Elfhosted](https://postersplus.elfhosted.com), [Slokker](https://postersplus.slokker.cc) or [Kuu.](https://postersplus.stremio.ru) 

---

<p align="center">
  <a href="https://raw.githubusercontent.com/UmbraProjects/PostersPlus/refs/heads/dev/Showcase/showcase.webp">
    <img src="Showcase/showcase.webp" width="100%" alt="Nuvio HTPC presets"/>
  </a>
  Various presets shown in <a href="https://github.com/UmbraProjects/NuvioDesktop">Nuvio HTPC</a>. Click for full resolution.
</p>

## Features

- **Ratings** - one weighted score from Letterboxd, Trakt, Rotten Tomatoes, IMDb, Metacritic, MyAnimeList and more, in four display styles with custom colour palettes.
- **Award sashes** - Oscars, Globes, Emmys, festival prizes, notable studios and cast, trending, new seasons, release status and more. You set the order and can turn any of them off.
- **Quality badges** - 4K, Remux, Dolby Vision, HDR and so on, read from AIOStreams, most Stremio addons, QualiCache, or your own Plex or Jellyfin files.
- **Title logos** - over textless art, in the language you choose, with automatic checks for posters that already have a title printed on them.
- **Landscape posters** - a dedicated 16:9 layout for landscape and Continue Watching slots.
- **Anime support** - AniList and Kitsu IDs work directly.
- **Web configurator** - tune everything in the browser with a live preview, then copy a ready-made URL for your client.
- **Admin dashboard** - manage the server from `/admin` instead of your compose file.
- **Plex and Jellyfin sync** - push the posters straight into your library.

## Getting started

You'll need:

- Docker
- A free [TMDB API key](https://www.themoviedb.org/settings/api) (artwork, logos, metadata)
- A free [MDBList API key](https://mdblist.com/) (ratings, awards, age ratings)

Create a `compose.yaml` and pick one of these templates:

Seperate env file:

```yaml
services:
  postersplus:
    image: ghcr.io/umbraprojects/postersplus:dev
    ports:
      - "8000:8000"
    restart: unless-stopped
    volumes:
      - ./postersplus-cache:/app/cache
    env_file:
      - .env 
```

Environment inside the compose:

```yaml
services:
  postersplus:
    image: ghcr.io/umbraprojects/postersplus:dev
    ports:
      - "8000:8000"
    restart: unless-stopped
    volumes:
      - ./postersplus-cache:/app/cache
    environment:
      - ADMIN_KEY=a-long-random-string   # unlocks /admin, at least 12 characters
      - ACCESS_KEY=another-secret        # recommended if the instance is on the internet
```

Start it:

```bash
docker compose up -d
```

Then open `http://your-host:8000/admin` and carry on below.

<details>
<summary>Building from source</summary>

```bash
git clone https://github.com/UmbraProjects/PostersPlus.git
cd PostersPlus
cp .env.example .env   # set ADMIN_KEY
docker compose up -d --build
```

</details>

## Admin dashboard

The dashboard is where you set up and run your instance. Log in with your `ADMIN_KEY`, then:

1. Under **Settings → API keys**, add your TMDB and MDBList keys.
2. Optionally, pick a **Quality source** so posters get quality badges (see below).
3. Click **Save**, then **Restart now**. The page reloads when the server is back.

Every setting has its help text next to it, and a chip showing whether its value was **saved** here, comes from your **env**, or is the **default**. Rarely needed options sit behind each group's *Advanced* fold.

The **Overview** tab shows how the instance is doing: cache sizes, renders in progress, remaining MDBList quota, cache warming and the watchlist.

The **Artwork** tab lets you choose a title's poster or logo for everyone on the instance. Search for the title, then pick from every TMDB, Fanart and TVDB image, for portrait and landscape posters.

The **Presets** tab adds your own looks to the configurator's **Load preset** gallery. Paste a poster URL from the configurator, name it, and give it a picture (render a title or upload an image). Users see them under **Operator**, beside the **Core** presets that ship with Posters+ and **Mine**, the presets each user saves in their own browser. Keys and the title are stripped from the URL when a preset is saved.

The **Fonts** tab adds your own label fonts to the configurator's **Font** list, for fonts Posters+ can't ship. Upload a static `.ttf` or `.otf` (one weight; Bold reads best) and give it a name. A font without a ★ gets Inter's, since the rating line draws one. You're responsible for having a licence that allows the font to be used for rendering images on a server.

Good to know:

- Saved settings live in `settings.json` in the cache volume and take priority over environment variables. Your compose file is never touched.
- `ADMIN_KEY` can only be set in the environment, and it's separate from `ACCESS_KEY`. Your clients see the access key in every poster URL, so keep the admin key to yourself.
- Put the instance behind HTTPS before using the dashboard over the internet.

Prefer environment variables? Everything the dashboard sets can be set that way too. See [CONFIGURATION.md](CONFIGURATION.md).

## Connecting your client

Open your instance's address in a browser to get the configurator. Pick a style, then press **Copy config**. A left-click copies the URL for AIOMetadata, Nuvio, Bingecat and Xperience, which all take the same one. Right-click (or long press) to copy it for Discover+ or to share your settings; if you pick Discover+, a left-click keeps copying that until you pick otherwise. The URL is built from whatever address you opened the configurator at, so open it from the address your client will use.

**Share settings** removes keys and personal choices and replaces the selected title with generic client placeholders. The shared URL can go directly into AIOMetadata, Nuvio, Bingecat or Xperience. For Discover+, import it into the configurator and use **Copy config → Discover+**. Import already uses the local instance's domain; it does not need the source domain replaced.

Instances are private for sharing by default: shared URLs use `https://postersplus.cc`, so direct client requests go to that public instance and use its server-side configuration and resources. Public operators can explicitly set **Public share origin** (`PUBLIC_SHARE_ORIGIN`) to their canonical HTTPS origin, such as `https://posters.example.com`. Use an origin only, with no path, credentials, query or fragment, and only for an instance intended for public use without an access key. Blank or invalid values use the fallback. This setting does not change access controls; setting `PUBLIC_URL` alone does not opt in. Opening a public instance through a private address still shares only the configured public origin.

> **Exposing it safely.** Clients need to reach PostersPlus over **HTTPS**, e.g. behind [Caddy](https://caddyserver.com/) or [Traefik](https://traefik.io/), with `ACCESS_KEY` set. If you use AIOMetadata, you can instead turn on its image proxy and keep PostersPlus off the internet entirely: use `http://postersplus:8000` as the address so the two talk over Docker's internal network. That's a little slower but the most secure option.

> **Configurator behind its own login.** If you put the configurator behind Authelia, Pangolin or a similar login, turn on **Configurator protected externally** (`CONFIGURATOR_EXTERNAL_AUTH`). The configurator then opens without `?access_key=` and adds the key to previews and copied URLs for you, while poster requests still need it, since clients can't sign in through a browser page. The trade-off: anyone who gets onto the configurator can read the access key, so this is only as secure as that login — and the login has to cover every path the configurator uses, not just `/`: `/`, `/server-caps`, `/search`, `/resolve-imdb`, `/resolve-tmdb` and `/debug/fallback-gallery`. `/server-caps` returns the access key itself, so leaving it outside the login gives the key away. `/poster`, `/logo`, `/trending/…` and `/static/…` stay outside it.

Using Plex or Jellyfin? See [Plex and Jellyfin sync](CONFIGURATION.md#plex-and-jellyfin-sync).

## Quality badges

Quality badges are optional. Choose a source in the dashboard's **Quality source** group:

| Source | What you need |
|---|---|
| **AIOStreams** | Your own [AIOStreams](https://github.com/Viren070/AIOStreams) instance |
| **Scraper** | Any standalone Stremio stream addon, such as [Torrentio](https://torrentio.strem.fun) or [Comet](https://comet.elfhosted.com) |
| **QualiCache** | The hosted instance: URL `https://quality.myaio.xyz`, access key `HN7Aj1Z4CV95k0ZxJJ583nmU`. It checks quality in the background, so posters never wait on a scrape. [More](CONFIGURATION.md#quality-via-qualicache) |

Plex and Jellyfin users get quality from their own files through the sync scripts, so they don't need any of these.

## Documentation

- [CONFIGURATION.md](CONFIGURATION.md): every setting and environment variable, QualiCache, the watchlist marker, caching and cache warming, and Plex/Jellyfin sync.
- [URL_REFERENCE.md](URL_REFERENCE.md): poster URL parameters, landscape posters, anime IDs, how sashes and ratings are picked, and translations.
- [ADVANCED.md](ADVANCED.md): tuning and debugging options you shouldn't need.
- [CONTRIBUTING.md](CONTRIBUTING.md): working on PostersPlus itself.

## Support

Chat, feature requests and bug reports are on [Discord](https://discord.com/invite/wEgTPNXUMU). 

If you'd like to support development: [Ko-fi](https://ko-fi.com/umbraprojects).

## Attribution

PostersPlus is built on data and images from these services. None of them endorse or certify it.

<a href="https://www.themoviedb.org"><img src="static/tmdb-logo.svg" alt="TMDB" height="14"></a>

- **[TMDB](https://www.themoviedb.org)**: this product uses the TMDB API but is not endorsed or certified by TMDB. Posters, backdrops, logos, metadata and trending lists.
- **[TheTVDB](https://thetvdb.com)**: metadata and artwork provided by TheTVDB.
- **[MDBList](https://mdblist.com)**: ratings (including Letterboxd, Rotten Tomatoes, Metacritic and Trakt scores) and lists.
- **[IMDb](https://www.imdb.com)**: information courtesy of IMDb. Used with permission. IMDb ratings come from the [IMDb non-commercial datasets](https://developer.imdb.com/non-commercial-datasets/).
- **[AniList](https://anilist.co)** and **[Kitsu](https://kitsu.app)**: anime art, titles, genres and scores.
- **[Cinemeta and Metahub](https://www.stremio.com)** (Stremio): metadata and logo fallbacks.
- **[fanart.tv](https://fanart.tv)**: posters, logos and backgrounds.
- **[Trakt](https://trakt.tv)**, **[SIMKL](https://simkl.com)** and **[PublicMetaDB](https://publicmetadb.com)**: watchlists for the watchlist marker.
- **[Arctic Shift](https://arctic-shift.photon-reddit.com)**: Reddit archive used to detect digital releases.
- **[Wikidata](https://www.wikidata.org)**: festival award winners, under [CC0](https://creativecommons.org/publicdomain/zero/1.0/).
- **[RapidOCR](https://github.com/RapidAI/RapidOCR)**: the PP-OCRv5 text detection model from [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR), under the Apache License 2.0.
- **Anime ID mapping**: from [Fribb/anime-lists](https://github.com/Fribb/anime-lists), which is built from [anime-offline-database](https://github.com/cedya77/anime-offline-database) (a continuation of [manami-project](https://github.com/manami-project)'s original) and [Anime-Lists/anime-lists](https://github.com/Anime-Lists/anime-lists). anime-offline-database is made available under the [Open Database License v1.0](https://opendatacommons.org/licenses/odbl/1-0/), and its contents under the [Database Contents License v1.0](https://opendatacommons.org/licenses/dbcl/1-0/).

The bundled fonts are Almarai, Barlow Condensed, Bebas Neue, Creepster, Exo 2, Fira Sans, Inter, Manrope, Montserrat, Noto Serif, Open Sans, Oswald, Pacifico, Playfair Display, Plus Jakarta Sans, Roboto Condensed, Rubik and Space Grotesk under the [SIL Open Font License 1.1](https://openfontlicense.org), and Ubuntu under the [Ubuntu Font Licence 1.0](https://ubuntu.com/legal/font-licence). Each licence, with its copyright notice, is in [fonts/](fonts).

### Badge artwork

The Graphic Badges mode downloads its marks from Wikimedia Commons at runtime; none ship with PostersPlus. Dolby Vision, Dolby Atmos and the combined Vision • Atmos mark are built from [Dolby Vision 2021 logo.svg](https://commons.wikimedia.org/wiki/File:Dolby_Vision_2021_logo.svg) and [Dolby Cinema 2021 logo.svg](https://commons.wikimedia.org/wiki/File:Dolby_Cinema_2021_logo.svg) (public domain). The DTS:X mark is [DTS X B&W.png](https://commons.wikimedia.org/wiki/File:DTS_X_B%26W.png) by CinemaLover24680, [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/), cut to its "dts" letters and recoloured white. Dolby, Dolby Vision, Dolby Atmos and DTS:X are trademarks of their owners.

Rating badges are downloaded the same way, each pinned to the exact file reviewed. The IMDb, Rotten Tomatoes (tomato, lettered tomato, splat and both Popcornmeter buckets), Metacritic, Letterboxd, Trakt, MyAnimeList and AniList logos come from Wikimedia Commons, all public domain; the TMDB logo from [TMDB's logos page](https://www.themoviedb.org/about/logos-attribution); the Kitsu mark from [Simple Icons](https://simpleicons.org/) (CC0); and Roger Ebert's thumbs-up is [Material Symbols](https://github.com/google/material-design-icons)' `thumb_up` (Apache 2.0). Most are set on round plates in their brand colours. All are trademarks of their owners.

## License

[GNU Affero General Public License v3.0](LICENSE). This project and any forks of it should remain open source.
