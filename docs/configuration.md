# Configuration

Required job variables:

- `CLEANARR_PLEX_BASEURL`
- `CLEANARR_PLEX_TOKEN`
- `CLEANARR_SONARR_BASEURL`
- `CLEANARR_SONARR_APIKEY`
- `CLEANARR_RADARR_BASEURL`
- `CLEANARR_RADARR_APIKEY`

Optional variables:

- `CLEANARR_TRANSMISSION_*` for torrent cleanup
- `CLEANARR_DRY_RUN` to disable destructive actions
- `CLEANARR_NTFY_*` for run summaries
- `WEBHOOK_SECRET` to protect the webhook endpoint; send it via `X-Cleanarr-Webhook-Token` or `X-Webhook-Token`
- `PLEX_WEBHOOK_ENABLE_DELETIONS` to let the webhook perform deletions
- `TARGET_PLEX_*` for cross-instance Plex sync
- `CLEANARR_USER_ALIASES_JSON` for username canonicalization in shared environments

The webhook and job use the same cleanup configuration surface so downstream operators only need one secret/config contract.
