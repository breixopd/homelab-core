# Media Stack Guide

Complete guide for the automated media automation stack: Sonarr, Radarr, Prowlarr, Bazarr, qBittorrent, Seerr, Recyclarr, FlareSolverr, Jellyfin, Plex, Navidrome, and Music Sync.

## Services Overview

| Service      | Purpose                                             | Default Port | Profile     |
| ------------ | --------------------------------------------------- | ------------ | ----------- |
| qBittorrent  | BitTorrent download client                          | 8080         | media       |
| Prowlarr     | Indexer manager                                     | 9696         | media       |
| Sonarr       | TV show automation                                  | 8989         | media       |
| Radarr       | Movie automation                                    | 7878         | media       |
| Bazarr       | Subtitle automation                                 | 6767         | media       |
| Seerr        | Media request UI                                    | 5055         | media       |
| Recyclarr    | TRaSH Guides sync                                   | —            | media       |
| FlareSolverr | Cloudflare bypass proxy                             | 8191         | media       |
| Jellyfin     | Media streaming                                     | 8096         | media       |
| Plex         | Alternative streaming                               | 32400        | media-plex  |
| Navidrome    | Music streaming                                     | 4533         | media       |
| Music Sync   | Import selected Spotify and YouTube Music libraries | —            | media       |
| rclone       | Remote storage VFS mount                            | —            | media-cache |
| Media Cache  | Promotion/demotion cache tier for remote media      | 8686         | media-cache |
| Tdarr        | Automated transcode/remux (CPU)                     | 8265         | media-tdarr |
| Gluetun      | VPN tunnel (sidecar)                                | —            | media-vpn   |

## How Services Connect

The installer's `category_setup()` function automatically wires all services together after first deploy:

1. **Prowlarr → Sonarr + Radarr**: Full-sync app connections so indexers propagate automatically
2. **Sonarr/Radarr → qBittorrent**: Download client configured with proper categories
3. **Bazarr → Sonarr + Radarr**: Subtitle automation linked via API keys
4. **Prowlarr → FlareSolverr**: Registered as indexer proxy for Cloudflare-protected sites
5. **Recyclarr → Sonarr + Radarr**: Quality profiles synced from TRaSH Guides

### Public Indexers (Auto-Added to Prowlarr)

Seven public torrent indexers are configured automatically:

- 1337x, YTS, EZTV, The Pirate Bay, LimeTorrents, TorrentGalaxy, Kickass Torrents

## TRaSH Guides Integration

### What Gets Applied

| Setting          | Service | Value                                                                                                                                                                                                                                   |
| ---------------- | ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Episode naming   | Sonarr  | `{Series TitleYear} - S{season:00}E{episode:00} - {Episode CleanTitle} [{Custom Formats}{Quality Full}]{[MediaInfo VideoDynamicRangeType]}{[Mediainfo AudioCodec}{ Mediainfo AudioChannels]}{MediaInfo AudioLanguages}{-Release Group}` |
| Movie naming     | Radarr  | `{Movie CleanTitle} {(Release Year)} [imdbid-{ImdbId}] - {Edition Tags }{[Custom Formats]}{[Quality Full]}{[MediaInfo 3D]}{[MediaInfo VideoDynamicRangeType]}...`                                                                       |
| Propers/Repacks  | Both    | `doNotPrefer` (when using Custom Formats)                                                                                                                                                                                               |
| Quality profiles | Sonarr  | WEB-1080p via `web-1080p-v4` template                                                                                                                                                                                                   |
| Quality profiles | Radarr  | HD Bluray + WEB via `hd-bluray-web` template                                                                                                                                                                                            |

### Recyclarr

Recyclarr syncs TRaSH Guides quality profiles on a cron schedule (default: daily at 03:00). It creates its own config on first run and connects to Sonarr/Radarr via API keys passed through environment variables.

No manual configuration is needed — recyclarr handles everything automatically using the recommended TRaSH Guides profiles.

## VPN Configuration

VPN details are in the [VPN (Gluetun)](#vpn-gluetun) section below.

## GPU Transcoding

Jellyfin supports hardware transcoding via Docker profiles:

| Profile        | GPU       | Devices                                            |
| -------------- | --------- | -------------------------------------------------- |
| `media-nvidia` | NVIDIA    | `/dev/nvidia*` (requires nvidia-container-toolkit) |
| `media-vaapi`  | Intel/AMD | `/dev/dri` (VA-API)                                |

Enable by adding the profile and setting `GPU_TYPE` accordingly.

## Music Sync (Spotify/YTMusic)

The Music Sync service imports selected Spotify and YouTube Music likes/playlists into the shared music library used by Navidrome.

### Setup

1. **Spotify**: Set `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` from the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
2. **YouTube Music**: Run the OAuth flow in the `music-sync` container to generate credentials
3. Playlists sync automatically on the configured schedule

Manage sync jobs, view status, and trigger manual syncs from the **Music Sync** area in the **Homelab UI**.

## Remote Storage & Caching

The media cache system offloads your library to cheap remote storage while keeping recently-watched content on fast local disk.

### How It Works

```
[Remote Storage] ←→ [rclone VFS Cache (500G)] ←→ [Jellyfin/Plex]
                        ↑
                 [Media Cache Manager]
                   - Playback webhooks
                   - Promotion warmup
                   - Cold-media demotion
```

**Download flow:**

1. Sonarr/Radarr tell qBittorrent to download a new episode/movie
2. qBittorrent downloads to local disk (`/media/downloads/`)
3. Sonarr/Radarr import the file to `/media/library/tv/` or `/media/library/movies/`
4. The file is now on local disk in the rclone VFS cache — immediately available
5. The media cache manager tracks it with a “last watched” timestamp

**Playback flow (content already cached):**

1. User clicks play in Jellyfin/Plex
2. Media server reads from `/media/library/` — served from local VFS cache
3. Jellyfin/Plex sends a playback webhook to the cache manager
4. Cache manager marks the file as recently watched (resets eviction timer)
5. For TV: remaining season episodes are promoted in background

**Playback flow (content on remote only):**

1. User clicks play — file is on remote storage only
2. **Priority warmup**: cache manager reads first 100MB immediately (~1.6s at 500Mbps)
3. Media server starts buffering — playback begins within a few seconds
4. Cache manager continues downloading the rest in background
5. For TV: remaining season episodes queue for background prefetch

**Eviction (every 6 hours):**

1. Content unwatched for 15 days (`COLD_AFTER_DAYS`) is evicted from local cache
2. The file remains accessible — rclone serves it from remote on next access
3. Watching anything resets the eviction timer to 0
4. Pinned files are never evicted

### Bandwidth & Concurrency

By default the cache service auto-estimates effective uplink from recent promotion reads. If there is no observed history yet, it falls back to detected link speed and then a conservative default.

At an effective 500Mbps uplink:

- ~24 seconds to fully cache a 1.5GB episode
- ~2 minutes to fully cache an 8GB 4K movie
- 20 concurrent 4K streams or 50 concurrent 1080p streams
- First-play latency: ~2 seconds (100MB priority read)

### Configuration

| Setting                 | Default       | Description                                                                       |
| ----------------------- | ------------- | --------------------------------------------------------------------------------- |
| `config.media.cache`    | `true`        | Enable playback-aware local cache tiering                                         |
| `RCLONE_CACHE_MAX_SIZE` | `500G`        | Local cache size (LRU eviction)                                                   |
| `COLD_AFTER_DAYS`       | `15`          | Days before unwatched content evicts                                              |
| `UPLINK_MBPS`           | `0`           | `0` = auto-detect from observed throughput; set a value to pin estimates manually |
| `RCLONE_REMOTE`         | `media-union` | rclone remote name                                                                |

### Supported Storage Backends

rclone supports 70+ cloud storage providers. Common ones for media:

| Type       | Provider                                              | Best For                                    |
| ---------- | ----------------------------------------------------- | ------------------------------------------- |
| `s3`       | AWS S3, Wasabi, DigitalOcean Spaces, SeaweedFS, MinIO | General-purpose, cheapest at scale          |
| `b2`       | Backblaze B2                                          | Cheapest egress-free option with Cloudflare |
| `sftp`     | Any SSH server, Hetzner Storage Box                   | Self-hosted, no vendor lock-in              |
| `webdav`   | Nextcloud, ownCloud, HiDrive                          | If you already have one                     |
| `drive`    | Google Drive                                          | Free 15GB, cheap 2TB plan                   |
| `dropbox`  | Dropbox                                               | If you already have one                     |
| `onedrive` | Microsoft OneDrive                                    | If you already have one                     |

### Adding Storage Backends

Add backends from the **Media Cache** area in the **Homelab UI**, or via rclone CLI:

```bash
docker compose exec rclone rclone config
```

**New backends are automatically joined to the storage pool.** When you add a backend via the API or Homelab UI, the `media-union` union remote is rebuilt to include it. No manual pool configuration needed.

If you add an external host from the **External Hosts** page and select **Media Cache**, the toolkit now treats that host as an SFTP-backed storage pool member. The host is saved with its media path, registered as an rclone backend, and reconciled again when the Cache page loads.

To remove a backend:

- Use the Homelab UI, or:

```bash
docker compose exec rclone rclone config delete <remote-name>
# Then rebuild the pool:
curl -X POST http://media-cache:8686/api/backends/rebuild-pool
```

### Monitoring

A dedicated **Media Cache** Grafana dashboard is provisioned automatically at `grafana.<domain>`. It shows:

- Cache usage (bytes, percentage, trend over time)
- Webhook activity rate (playback events per minute)
- Prefetch operations (started vs completed)
- Active prefetch queue depth
- Eviction counter
- Effective uplink source, observed throughput, eviction timer, and pinned files

Data is scraped every 30 seconds via Prometheus from the media-cache `/metrics` endpoint.

### Webhook URLs

Configured automatically during setup. Manual setup if needed:

- **Jellyfin**: Settings → Notifications → Webhook Plugin → `http://media-cache:8686/webhook/jellyfin`
- **Plex**: Settings → Webhooks → `http://media-cache:8686/webhook/plex`

## Tdarr (automated transcode)

Tdarr runs on the media VM (CPU-only by default) and processes **library** paths shared with Sonarr/Radarr:

- Movies: `/data/movies`
- TV: `/data/tv`
- Transcode scratch: `/data/tdarr-cache` → container `/temp` (always local disk)

Enable it from the Tdarr service page or with
`service_settings.tdarr.enabled: true`. Hooks create libraries via the Tdarr
API after deploy. CPU workers set to `0` and GPU workers set to `-1` are sized
automatically from the selected machine's detected capabilities.

**With media cache enabled:** call `POST http://media-cache:8686/api/pin` with `{"path":"/data/..."}` before long transcodes so eviction does not delete in-progress files; `POST /api/unpin` when done. Tdarr Flows can automate this HTTP step.

**Without media cache:** Tdarr reads/writes the same local `${INSTALL_ROOT}/media` tree as the \*arr apps.

UI: `https://tdarr.<your-domain>`

## Selective installation

Toggle categories and optional services in **`config.yaml`**. Service-owned Compose
applications declare the profiles selected by generation. Regenerate and redeploy:

```bash
homelab-toolkit generate
docker compose --env-file generated/media/.env up -d
```

Use `homelab-toolkit services deploy <name>` for one service, or edit the
declarative service selections in `config.yaml` for a persistent subset.

## VPN (Gluetun)

Torrent traffic routes through Gluetun on the **media** VM.

| Provider  | `VPN_PROVIDER` | Auth                            |
| --------- | -------------- | ------------------------------- |
| NordVPN   | `nordvpn`      | User/password or token          |
| ProtonVPN | `protonvpn`    | User/password or WireGuard      |
| Custom    | `custom`       | WireGuard key or OpenVPN config |

```ini
VPN_ENABLED=true
VPN_PROVIDER=protonvpn
VPN_TYPE=wireguard
```

```bash
homelab-toolkit generate
docker compose --env-file generated/media/.env --profile media --profile media-vpn up -d
docker compose exec gluetun wget -qO- https://ipinfo.io/ip   # should show VPN egress IP
```

qBittorrent uses `network_mode: service:gluetun` — only torrent traffic uses the tunnel.
