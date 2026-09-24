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
      - env 
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

Good to know:

- Saved settings live in `settings.json` in the cache volume and take priority over environment variables. Your compose file is never touched.
- `ADMIN_KEY` can only be set in the environment, and it's separate from `ACCESS_KEY`. Your clients see the access key in every poster URL, so keep the admin key to yourself.
- Put the instance behind HTTPS before using the dashboard over the internet.

Prefer environment variables? Everything the dashboard sets can be set that way too. See [CONFIGURATION.md](CONFIGURATION.md).

## Connecting your client

Open your instance's address in a browser to get the configurator. Pick a style, then press **Copy config**. The first time, it asks which client the URL is for (AIOMetadata, Nuvio, Bingecat and so on) and copies the right format. After that, a left-click copies for the same client and a right-click lets you pick another. The URL is built from whatever address you opened the configurator at, so open it from the address your client will use.

> **Exposing it safely.** Clients need to reach PostersPlus over **HTTPS**, e.g. behind [Caddy](https://caddyserver.com/) or [Traefik](https://traefik.io/), with `ACCESS_KEY` set. If you use AIOMetadata, you can instead turn on its image proxy and keep PostersPlus off the internet entirely: use `http://postersplus:8000` as the address so the two talk over Docker's internal network. That's a little slower but the most secure option.

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

## License

[GNU Affero General Public License v3.0](LICENSE). This project and any forks of it should remain open source.
