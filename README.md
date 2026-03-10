# Cleanarr

`cleanarr` is a reusable Plex cleanup runtime with two deployable harnesses:

- `job`: a scheduled cleanup worker for Plex, Sonarr, Radarr, and optional Transmission maintenance
- `webhook`: a Plex webhook receiver that can sync watch state and optionally trigger deletion logic from Plex events

This repository is public-safe by design. It contains the runtime, images, tests, and generic examples. Private cluster overlays, secrets, and rollout orchestration belong in a downstream repo.

## What It Does

Cleanarr is built for setups where Plex is the source of truth for watched state, while Sonarr and Radarr remain the source of truth for media lifecycle.

At a high level it can:

- inspect Plex watch history for episodes and movies
- map Plex items back to Sonarr and Radarr records
- delete or unmonitor matching files when policy allows it
- skip protected content with `safe` and `kids` tags
- perform watched-ahead logic for TV episodes using real user history
- remove items from a Plex watchlist after cleanup
- perform optional Transmission maintenance for stale torrents, failed downloads, and repeated I/O errors
- receive Plex webhook events and optionally act on them in near real time
- send run summaries and health notifications to `ntfy`

## Runtime Modes

There are two supported runtime shapes:

### `job`

The scheduled job runs the full cleanup pass:

- connect to Plex
- load watched episodes and movies
- match content in Sonarr and Radarr
- apply deletion policy
- optionally do Transmission maintenance
- emit a summary line and optional `ntfy` notification

This is the safest default deployment mode and the one most operators should start with.

### `webhook`

The webhook app exposes a Plex webhook endpoint and health endpoint:

- `POST /plex/webhook`
- `GET /healthz`

It can:

- record incoming Plex events
- optionally trigger deletion handling for `media.scrobble`, `media.stop`, and removal-style events
- optionally sync watch state to another Plex server
- expose dependency health for probes and monitoring

The webhook is intentionally opt-in for destructive behavior. Running the webhook does not imply deletions are enabled.

## Architecture

The repository is split into three layers:

- `cleanarr/`
  Shared library code, configuration loading, cleanup logic, and the webhook app implementation.
- `apps/job/`
  Thin entrypoint for the scheduled job image.
- `apps/webhook/`
  Thin entrypoint for the webhook image.

Downstream repos are expected to own:

- Kubernetes manifests and overlays
- secret injection
- ingress, Cloud Run, or service exposure
- image pinning and rollout strategy

### Flow

```text
Plex watch state / webhook events
            |
            v
        cleanarr
            |
            +--> Sonarr episode matching and file removal
            +--> Radarr movie matching and file removal
            +--> Transmission maintenance
            +--> Plex watchlist cleanup
            +--> ntfy summaries / health notifications
```

## Images

- `ghcr.io/<owner>/cleanarr-cronjob`
- `ghcr.io/<owner>/cleanarr-webhook-app`

Release tags follow semver:

- `vMAJOR.MINOR.PATCH`
- `vMAJOR.MINOR`
- `sha-<commit>`

The Python package version in `pyproject.toml` is the release source of truth.

## Quick Start

1. Copy `.env.example` to `.env`
2. Set the required Plex, Sonarr, and Radarr credentials
3. Start in safe mode with `CLEANARR_DRY_RUN=true`
4. Run the job locally:

```bash
python -m pip install -e .[dev]
python apps/job/main.py
```

5. Or run the webhook locally:

```bash
python -m pip install -e .[dev]
python apps/webhook/main.py
```

## Configuration Flags

The full configuration contract is documented in [docs/configuration.md](./docs/configuration.md). The sections below call out the flags operators usually care about first.

### Core Service Flags

| Variable | Required | Purpose |
| --- | --- | --- |
| `CLEANARR_PLEX_BASEURL` | Yes | Plex base URL used for watch state, item lookups, and watchlist cleanup |
| `CLEANARR_PLEX_TOKEN` | Yes | Plex auth token |
| `CLEANARR_SONARR_BASEURL` | Yes | Sonarr API base URL |
| `CLEANARR_SONARR_APIKEY` | Yes | Sonarr API key |
| `CLEANARR_RADARR_BASEURL` | Yes | Radarr API base URL |
| `CLEANARR_RADARR_APIKEY` | Yes | Radarr API key |
| `CLEANARR_LOG_FILE` | No | Log file path for the runtime |
| `CLEANARR_DEBUG` | No | Enables verbose logging |

### Safety and Cleanup Behavior

| Variable | Default | What It Does |
| --- | --- | --- |
| `CLEANARR_DRY_RUN` | `false` | Disables destructive delete operations. Start here first. |
| `CLEANARR_DISABLE_TORRENT_CLEANUP` | `false` | Disables stale torrent and failed download cleanup. |
| `CLEANARR_REMOVE_FAILED_DOWNLOADS` | `false` | Removes failed Transmission downloads when enabled. |
| `CLEANARR_STALE_TORRENT_HOURS` | `8` | Age threshold for stale torrent cleanup. |
| `CLEANARR_TRANSMISSION_IO_ERROR_CLEANUP_ENABLED` | `false` | Enables repeated Transmission I/O error cleanup logic. |
| `CLEANARR_TRANSMISSION_IO_ERROR_THRESHOLD` | `3` | Number of repeated I/O errors before action is taken. |
| `CLEANARR_TRANSMISSION_IO_ERROR_STATE_FILE` | `/logs/transmission-io-error-state.json` | Persistent state used by I/O error cleanup tracking. |

### Transmission Flags

Transmission is optional unless you want torrent maintenance or torrent removal tied to cleanup.

| Variable | Required | Purpose |
| --- | --- | --- |
| `CLEANARR_TRANSMISSION_HOST` | No | Transmission host |
| `CLEANARR_TRANSMISSION_PORT` | No | Transmission RPC port |
| `CLEANARR_TRANSMISSION_USERNAME` | No | Transmission RPC username |
| `CLEANARR_TRANSMISSION_PASSWORD` | No | Transmission RPC password |

### Webhook Flags

| Variable | Default | What It Does |
| --- | --- | --- |
| `PLEX_WEBHOOK_PORT` | `8000` | Port used by the webhook app locally or in container runtime |
| `PLEX_WEBHOOK_ENABLE_DELETIONS` | `false` | Allows webhook events to trigger deletion handling |
| `WEBHOOK_SECRET` | unset | Shared secret accepted in `X-Cleanarr-Webhook-Token` or `X-Webhook-Token` |
| `WEBHOOK_SECRET_PREVIOUS` | unset | Previous secret accepted during token rotation |

### Cross-Plex Sync Flags

These only matter for the webhook app when syncing watch state to a second Plex instance.

| Variable | Default | What It Does |
| --- | --- | --- |
| `TARGET_PLEX_BASEURL` | unset | Target Plex base URL for sync |
| `TARGET_PLEX_TOKEN` | unset | Default target Plex token |
| `TARGET_PLEX_USER_TOKENS_JSON` | empty | Per-user token overrides as JSON |
| `PLEX_SYNC_REQUIRE_USER_MATCH` | `true` | Prevents cross-user sync if the token owner does not match the webhook user |
| `PLEX_SYNC_STRICT_MONOTONIC` | `true` | Prevents older watch/progress state from overwriting newer state |
| `PLEX_SYNC_PROGRESS_EVENTS` | `false` | Enables progress-event sync, not just fully watched events |
| `PLEX_SYNC_PROGRESS_MIN_ADVANCE_MS` | `15000` | Minimum progress delta before a sync update is emitted |
| `CLEANARR_USER_ALIASES_JSON` | empty | JSON map used to normalize usernames across systems |

### Notification and Observability Flags

| Variable | Default | What It Does |
| --- | --- | --- |
| `CLEANARR_NTFY_BASEURL` / `CLEANARR_NTFY_URL` | `https://ntfy.sh` | ntfy endpoint used by the cleanup job |
| `CLEANARR_NTFY_TOPIC` | unset | ntfy topic for cleanup summaries |
| `CLEANARR_NTFY_TOKEN` | unset | Optional bearer token for ntfy |
| `CLEANARR_NTFY_TAGS` | `warning,clapper` | Tags attached to ntfy cleanup messages |
| `CLEANARR_NTFY_PRIORITY` | `default` | ntfy message priority |
| `NTFY_ENABLE` | `false` | Enables webhook-side health notifications |
| `NTFY_TOPIC` | unset | Webhook ntfy topic |
| `NTFY_URL` | derived | Webhook ntfy endpoint |
| `NTFY_TOKEN` | unset | Optional webhook ntfy token |
| `NTFY_HEALTH_INTERVAL` | `60` | Seconds between webhook dependency health checks |
| `NTFY_COOLDOWN` | `3600` | Minimum seconds between repeated health notifications |
| `LOKI_URL` | unset | Sends webhook logs to Loki when configured |
| `CF_ACCESS_CLIENT_ID` | unset | Optional Cloudflare Access header for Loki or upstream API access |
| `CF_ACCESS_CLIENT_SECRET` | unset | Optional Cloudflare Access secret |

## Recommended Rollout

For a new deployment:

1. Set `CLEANARR_DRY_RUN=true`
2. Set `CLEANARR_DISABLE_TORRENT_CLEANUP=true` unless you explicitly want Transmission maintenance on day one
3. Run the `job` image first and inspect the summary logs
4. Verify protected items are being skipped as expected
5. Only then disable dry-run
6. Enable `PLEX_WEBHOOK_ENABLE_DELETIONS=true` only if you want event-driven cleanup in addition to the scheduled job

## Public / Private Boundary

Public repo:

- reusable runtime code
- job and webhook harnesses
- tests
- Dockerfiles
- CI and release workflows
- generic example manifests

Private downstream repo:

- environment-specific manifests and overlays
- private domains and service topology
- secrets and external secret wiring
- ingress, Cloud Run, or cluster exposure
- pinned image digests and rollout orchestration

## Repository Layout

```text
cleanarr/
  cleanarr/              shared runtime package
  apps/job/              cronjob entrypoint and Dockerfile
  apps/webhook/          webhook entrypoint and Dockerfile
  deploy/examples/       generic Kubernetes examples
  docs/                  release, security, architecture, config docs
  tests/                 unit and feature tests
```

## More Documentation

- [Configuration](./docs/configuration.md)
- [Architecture](./docs/architecture.md)
- [Release Process](./docs/release.md)
- [Architecture Review Gate](./docs/architecture-review.md)
- [Security Review Gate](./docs/security-review.md)
