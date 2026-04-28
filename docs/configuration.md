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
- `WEBHOOK_SECRET` to protect the Plex webhook endpoint; send it via `X-Cleanarr-Webhook-Token` or `X-Webhook-Token`
- `JELLYFIN_WEBHOOK_SECRET` to protect the Jellyfin webhook endpoint (/jellyfin/webhook)
- `PLEX_WEBHOOK_ENABLE_DELETIONS` to let the webhook perform deletions
- `CLEANARR_WEBHOOK_QUEUE_MODE` (`direct` or `sqs`) for staged webhook buffering
- `CLEANARR_WEBHOOK_QUEUE_URL` and `CLEANARR_WEBHOOK_QUEUE_REGION` for SQS wiring
- `CLEANARR_WEBHOOK_QUEUE_ENQUEUING` to enable producer behavior in webhook runtime
- `CLEANARR_WEBHOOK_QUEUE_POLLING` to enable consumer behavior only in the SQS consumer runtime (`apps/lambda/main.py`)
- `CLEANARR_WEBHOOK_QUEUE_MAX_MESSAGES`, `CLEANARR_WEBHOOK_QUEUE_WAIT_SECONDS`, and `CLEANARR_WEBHOOK_QUEUE_VISIBILITY_TIMEOUT` for poll tuning
- `CLEANARR_WEBHOOK_FORWARD_URL` to keep the proxy harness compatible with the Lambda URL sink during rollout or fallback
- `CLEANARR_DECISION_REPORT_FILE` to persist machine-readable webhook and cleanup decisions as JSONL
- `TARGET_PLEX_*` for cross-instance Plex sync
- `CLEANARR_USER_ALIASES_JSON` for multi-platform username canonicalization. Supports legacy flat mapping or multi-platform objects:
  ```json
  {
    "josh": {"plex": "josharcher354", "jellyfin": "gawly"},
    "erin": {"plex": "erinarcher", "jellyfin": "erin"}
  }
  ```

The webhook, scheduled job runtime, and SQS webhook consumer runtime use the same cleanup configuration surface so downstream operators only need one secret/config contract.

Issue #629 staged mode contract:

- Direct webhook mode: leave `CLEANARR_WEBHOOK_QUEUE_MODE=direct` and run the webhook app as the ingress endpoint
- Webhook runtime: `CLEANARR_WEBHOOK_QUEUE_MODE=sqs` with `CLEANARR_WEBHOOK_QUEUE_ENQUEUING=true`
- SQS consumer runtime: `CLEANARR_WEBHOOK_QUEUE_MODE=sqs` with `CLEANARR_WEBHOOK_QUEUE_POLLING=true` (consumer runtime only)
- Scheduled/job runtimes (`apps/job/main.py`, `apps/job/lambda_handler.py`) do not consume queue messages and should not set `CLEANARR_WEBHOOK_QUEUE_POLLING`
- Fallback mode: set `CLEANARR_WEBHOOK_QUEUE_MODE=direct` to bypass queueing and process immediately
- Proxy runtime: set `CLEANARR_WEBHOOK_QUEUE_URL` for direct SQS publishing; keep `CLEANARR_WEBHOOK_FORWARD_URL` only if you still need Lambda URL compatibility during rollout

## AWS Lambda SQS consumer contract

For Lambda consumers driven by SQS event source mappings:

- Use image `ghcr.io/<owner>/cleanarr-lambda` in CI/packaging and deploy from the `ecr_release_tag_ref` field in `release-metadata.json`.
- Set queue mode to `sqs`:
  - `CLEANARR_WEBHOOK_QUEUE_MODE=sqs`
  - `CLEANARR_WEBHOOK_QUEUE_POLLING=false`
  - `CLEANARR_WEBHOOK_QUEUE_ENQUEUING=false`
  - `CLEANARR_WEBHOOK_QUEUE_URL=<SQS queue URL>`
- Keep deletion behavior explicit:
  - `PLEX_WEBHOOK_ENABLE_DELETIONS=true` only when destructive actions are expected
  - `CLEANARR_DRY_RUN=false` only when the target environment is approved for deletions
- For staged/proxy ingress (not direct SQS mapping), keep a separate producer with `CLEANARR_WEBHOOK_QUEUE_ENQUEUING=true`.

Repository boundary:

- Keep webhook and proxy runtime code in `cleanarr`
- Keep cluster manifests, Terraform IAM, queue provisioning, and release promotion in the downstream environment repo

