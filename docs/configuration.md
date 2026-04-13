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
- `CLEANARR_WEBHOOK_QUEUE_MODE` (`direct` or `sqs`) for staged webhook buffering
- `CLEANARR_WEBHOOK_QUEUE_URL` and `CLEANARR_WEBHOOK_QUEUE_REGION` for SQS wiring
- `CLEANARR_WEBHOOK_QUEUE_ENQUEUING` to enable producer behavior in webhook runtime
- `CLEANARR_WEBHOOK_QUEUE_POLLING` to enable consumer behavior in the SQS consumer runtime
- `CLEANARR_WEBHOOK_QUEUE_MAX_MESSAGES`, `CLEANARR_WEBHOOK_QUEUE_WAIT_SECONDS`, and `CLEANARR_WEBHOOK_QUEUE_VISIBILITY_TIMEOUT` for poll tuning
- `TARGET_PLEX_*` for cross-instance Plex sync
- `CLEANARR_USER_ALIASES_JSON` for username canonicalization in shared environments

The webhook, scheduled job runtime, and SQS webhook consumer runtime use the same cleanup configuration surface so downstream operators only need one secret/config contract.

Issue #629 staged mode contract:

- Webhook runtime: `CLEANARR_WEBHOOK_QUEUE_MODE=sqs` with `CLEANARR_WEBHOOK_QUEUE_ENQUEUING=true`
- SQS consumer runtime: `CLEANARR_WEBHOOK_QUEUE_MODE=sqs` with `CLEANARR_WEBHOOK_QUEUE_POLLING=true`
- Fallback mode: set `CLEANARR_WEBHOOK_QUEUE_MODE=direct` to bypass queueing and process immediately
