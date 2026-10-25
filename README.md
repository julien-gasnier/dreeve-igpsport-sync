# dreeve-igpsport-sync

`dreeve-igpsport-sync` automatically downloads your new [iGPSPORT](https://www.igpsport.com/) activities and drops them into the `watch/` directory of [Dreeve](https://github.com/robiningelbrecht/dreeve) when running in `IMPORT_MODE=files`. Dreeve will import them automatically.

> **Note:** This tool uses iGPSPORT's unofficial API, which may change without notice.

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/)
- A working Dreeve instance with `IMPORT_MODE=files` and a shared `watch/` directory
- Your iGPSPORT credentials (email/password) or a JWT token

## Installation (step by step)

### 1) Clone this extension into your Dreeve installation

From the root directory of your Dreeve installation (the one containing `docker-compose.yml`):

```bash
git clone https://github.com/julien-gasnier/dreeve-igpsport-sync.git
```

### 2) Configure environment variables

Copy the example file and fill in your credentials:

```bash
cp dreeve-igpsport-sync/.env.example .env.dreeve-igpsport-sync
# Edit .env.dreeve-igpsport-sync with your credentials
```

You can also add the variables directly to your main Dreeve `.env` file.

### 3) Add the service to `docker-compose.yml`

Add this service to your existing `docker-compose.yml`:

```yaml
  dreeve-igpsport-sync:
    build:
      context: ./dreeve-igpsport-sync
    container_name: dreeve-igpsport-sync
    restart: unless-stopped
    volumes:
      - ./watch:/watch
      - ./dreeve-igpsport-sync/state:/state
    env_file: ./.env.dreeve-igpsport-sync
    environment:
      - WATCH_DIR=/watch
      - STATE_DIR=/state
    networks:
      - dreeve-network
```

If you'd rather use your main `.env` file, replace `env_file: ./.env.dreeve-igpsport-sync` with `env_file: ./.env`.

### 4) Start the service

```bash
docker compose up -d --build dreeve-igpsport-sync
```

### 5) Verify it's working

Check login and list your recent activities:

```bash
docker compose run --rm dreeve-igpsport-sync --check
```

Follow logs:

```bash
docker compose logs -f dreeve-igpsport-sync
```

Dry run first (recommended):

```bash
docker compose run --rm dreeve-igpsport-sync --once --dry-run
```

## Duplicate prevention

- **File already in watch/**: skips if `igpsport_<rideId>.*` already exists in `watch/`
- **Local state**: tracks successfully downloaded activities in `state/synced.json`
- **Explicit exclusion**: add ride IDs to `IGPSPORT_IGNORED_RIDE_IDS` to skip them permanently

## Configuration

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `IGPSPORT_USERNAME` | Yes* | - | Your iGPSPORT account email |
| `IGPSPORT_PASSWORD` | Yes* | - | Your iGPSPORT account password |
| `IGPSPORT_TOKEN` | Yes* | - | JWT token (alternative to email/password) |
| `IGPSPORT_REGION` | No | `global` | Region (`global` or `cn`) |
| `MAX_PAGES` | No | `3` | Number of pages to fetch (20 activities per page) |
| `SYNC_INTERVAL_SECONDS` | No | `900` | Sync interval in seconds |
| `IGPSPORT_IGNORED_RIDE_IDS` | No | - | Comma-separated ride IDs to skip permanently |
| `WATCH_DIR` | No | `/watch` | Target directory for downloaded files |
| `STATE_DIR` | No | `/state` | Directory for local sync state |

\* Either `IGPSPORT_USERNAME` + `IGPSPORT_PASSWORD`, or `IGPSPORT_TOKEN` must be set.

## Tips

- Files dropped in `watch/` are imported by Dreeve every ~5 minutes. **Do not delete them manually** — Dreeve consumes them.
- Run with `--dry-run` first to see what would be downloaded.
- If an activity has an incorrect date, add its ID to `IGPSPORT_IGNORED_RIDE_IDS`.

## License

MIT
