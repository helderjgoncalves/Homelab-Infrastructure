# media-automation

Plex Watchlist (movies, latest TV season) or Seerr (specific TV seasons) →
Radarr/Sonarr → qBittorrent → hardlink into the Plex library.

| Service | Port (loopback) | Role |
| --- | --- | --- |
| qBittorrent | `9080` (torrent `51513`) | Downloads into `/data/torrents/<category>` |
| Prowlarr | `9696` | Indexers, pushed to Radarr and Sonarr |
| Radarr | `7878` | Movies → `/data/media/movies` |
| Sonarr | `8989` | TV → `/data/media/tv` |
| Maintainerr | `6246` | Deletes watched media by rule, via Radarr/Sonarr |
| Seerr | `5055` | Request portal: pick TV seasons; local logins for managed users |

All services share the `media` network and reach each other by container name.
Plex runs natively on the NAS and picks up new files by watching its folders.

## Storage

```
$DATA_DIR/
├── torrents/{movies,tv,music}
└── media/{movies,tv,music}
```

`torrents/` and `media/` must be on the same filesystem, and Radarr, Sonarr
and qBittorrent must all see them through the single `/data` mount, or imports
silently fall back to copies.

qBittorrent also keeps its old mounts (`/downloads`, `/movies`, `/tv`,
`/music`) via the gitignored `docker-compose.override.yml`, so torrents added
before this layout keep seeding. Remove each one once nothing uses it.

## Migrating from the old `qbittorrent/` stack

Run on the NAS. Nothing here deletes data.

```sh
DOCKER=/share/CACHEDEV2_DATA/.qpkg/container-station/bin/docker
STACKS=/share/CACHEDEV2_DATA/SSDShared
DATA=/share/external/DEV3302_1/data

# 1. Create the data root up front (Docker would otherwise create it as root).
mkdir -p $DATA/torrents/{movies,tv,music} $DATA/media/{movies,tv,music}
chown -R 1000:100 $DATA

# 2. Stop the old stack, then copy its config (settings + torrent list).
$DOCKER exec dockge docker compose -f $STACKS/qbittorrent/docker-compose.yml down
mkdir -p $STACKS/media-automation/appdata
cp -a $STACKS/qbittorrent/appdata $STACKS/media-automation/appdata/qbittorrent

# 3. Start the stack.
$DOCKER exec dockge docker compose -f $STACKS/media-automation/docker-compose.yml up -d
```

Once qBittorrent is confirmed healthy with all torrents present, the old
`qbittorrent/` folder can be removed.

## First-time app configuration

- **qBittorrent:** categories `movies`, `tv` (later `music`) saving to
  `/data/torrents/<category>`; set a seeding ratio/time limit.
- **Prowlarr:** add indexers; Settings → Apps → add Radarr (`http://radarr:7878`)
  and Sonarr (`http://sonarr:8989`) with their API keys.
- **Radarr / Sonarr:**
  - Download client: qBittorrent at `qbittorrent:9080`, category `movies` / `tv`,
    "Remove Completed" on.
  - Root folder: `/data/media/movies` / `/data/media/tv`.
  - Media Management: hardlinks on (default), "Unmonitor deleted" on.
  - Import lists: **Plex Watchlist** (your own, Ultra-HD profile in Radarr) and
    **Plex Watchlist RSS** → friends' feed (HD-1080p profile). Clean Library Level: Disabled.
- **Maintainerr:** connect Plex (`http://<NAS_IP>:32400`), Radarr
  (`http://radarr:7878`) and Sonarr (`http://sonarr:8989`); rules delete watched
  titles that are on no Watchlist, after a "Leaving Soon" grace period, and
  remove them from Radarr/Sonarr so a later Watchlist add downloads them again.
- **Seerr:** sign in with the Plex owner account; add Radarr (`radarr:7878`,
  Ultra-HD/HD-1080p) and Sonarr (`sonarr:8989`, root `/data/media/tv`); create
  local users for managed Plex profiles.
- **Plex:** point the Movies and TV libraries at `$DATA_DIR/media/{movies,tv}`,
  keep "Scan my library automatically" on.
