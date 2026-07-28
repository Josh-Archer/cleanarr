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
- `CLEANARR_DRY_RUN_REPORT_DIR` directory for per-user dry-run report artifacts (`cleanarr-dry-run-report.json` and `.md`). Default: `/logs/dry-run-reports`. Written at end of a **job-mode** dry-run only.
- `CLEANARR_NTFY_*` for run summaries
- `WEBHOOK_SECRET` / `WEBHOOK_SECRET_PREVIOUS` to protect the Plex webhook endpoint (`/plex/webhook`) and the proxy ingress for Plex events
- `JELLYFIN_WEBHOOK_SECRET` / `JELLYFIN_WEBHOOK_SECRET_PREVIOUS` to protect the Jellyfin webhook endpoint (`/jellyfin/webhook`) and the proxy ingress for Jellyfin events
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
- `CLEANARR_MULTI_USER_DELETE_POLICY` for household-safe deletion (see below)
- `CLEANARR_HOUSEHOLD_USERS` optional comma-separated Plex/Jellyfin usernames that form the household
- `CLEANARR_PRIMARY_USER` username used when policy is `primary_user_only`

## Household multi-user delete policy (issue #20)

Shared libraries must not delete an episode or movie just because **one** household member finished it while another is still mid-season.

### Policy values

| Value | Behavior | Safety |
| --- | --- | --- |
| `require_all_users` | **Default.** Every household member must satisfy the delete precondition (exact-item watched, or watched-ahead past the episode). | Safest for multi-user homes |
| `majority` | Strict majority (`floor(n/2)+1`) of household members must be satisfied. | Balanced |
| `primary_user_only` | Only `CLEANARR_PRIMARY_USER` must be satisfied; other members are ignored. | Least safe for shared libraries; use only when one account is the intentional owner |

Aliases such as `require-all-users`, `all`, `primary-only` are accepted. Unknown values fall back to `require_all_users`. If `primary_user_only` is selected without `CLEANARR_PRIMARY_USER`, Cleanarr logs a warning and falls back to `require_all_users`.

### How household members are resolved

For each media item, the household constituency is chosen in this order:

1. **`CLEANARR_HOUSEHOLD_USERS`** when set (explicit shared-library roster)
2. Else **Sonarr/Radarr user tags** on the series/episode/movie (ownership tags; `safe` / `kids` remain protected and never count as users)
3. Else **every account present in Plex/Jellyfin watch status** for that item (owner + managed users)

Example: Alice watched S1E3, Bob has not. With the default policy and no user tags, Cleanarr **keeps** the file. Watched-ahead inference uses the same policy so Alice finishing S1E10 cannot delete S1E1–S1E8 while Bob is still catching up.

### Operator guidance

- Leave the default (`require_all_users`) for household libraries.
- Set `CLEANARR_HOUSEHOLD_USERS=alice,bob` when guest or unused Plex accounts would otherwise block cleanup forever.
- Use Sonarr/Radarr per-user tags when only a subset of the household "owns" a title.
- Prefer `primary_user_only` only for single-decision-maker setups, and always set `CLEANARR_PRIMARY_USER`.

The webhook, scheduled job runtime, and SQS webhook consumer runtime use the same cleanup configuration surface so downstream operators only need one secret/config contract.

## Webhook authenticity (optional, fail-closed)

When any of the platform secrets above are set, Cleanarr **fails closed**: requests without a valid shared secret or HMAC signature receive `401 Unauthorized` and are not enqueued, forwarded, or processed. When secrets are unset, authenticity checks are disabled (protect the endpoint at ingress/network instead).

### Shared secret (header or query)

Send the secret with one of:

- Header `X-Cleanarr-Webhook-Token: <secret>`
- Header `X-Webhook-Token: <secret>`
- Query `?token=<secret>` (prefer headers; query tokens can appear in access logs)

`WEBHOOK_SECRET_PREVIOUS` / `JELLYFIN_WEBHOOK_SECRET_PREVIOUS` are accepted during rotation so callers can cut over without downtime.

### HMAC-SHA256 body signature

Alternatively (or in addition), sign the **raw request body** with the configured secret and send one of:

- `X-Cleanarr-Signature: sha256=<hex>`
- `X-Hub-Signature-256: sha256=<hex>`
- `X-Signature-256: sha256=<hex>` (bare hex also accepted)

Example (bash):

```bash
SECRET='your-shared-secret'
BODY='{"event":"media.scrobble"}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')
curl -X POST "https://cleanarr.example/plex/webhook" \
  -H "Content-Type: application/json" \
  -H "X-Cleanarr-Signature: sha256=${SIG}" \
  --data-binary "$BODY"
```

### Ingress / proxy notes

- Apply the same secrets on the **webhook app** and the **webhook proxy** (`cleanarr.webhook.proxy`). The proxy verifies before SQS publish or Lambda forward.
- Prefer putting authenticity at Cleanarr **and** at the edge (Cloudflare Access, mTLS, private network). Edge auth does not replace app-level verification when the path is internet-reachable.
- Plex Media Server webhooks do not natively attach HMAC headers; use a reverse-proxy/middleware that injects `X-Cleanarr-Webhook-Token`, or a signed forwarder. Jellyfin webhook plugins can typically set custom headers for the shared secret or signature.
- Rotation: set `*_PREVIOUS` to the old value, deploy callers with the new secret, then clear `*_PREVIOUS`.

Issue #629 staged mode contract:

- Direct webhook mode: leave `CLEANARR_WEBHOOK_QUEUE_MODE=direct` and run the webhook app as the ingress endpoint
- Webhook runtime: `CLEANARR_WEBHOOK_QUEUE_MODE=sqs` with `CLEANARR_WEBHOOK_QUEUE_ENQUEUING=true`
- SQS consumer runtime: `CLEANARR_WEBHOOK_QUEUE_MODE=sqs` with `CLEANARR_WEBHOOK_QUEUE_POLLING=true` (consumer runtime only)
- Scheduled/job runtimes (`apps/job/main.py`, `apps/job/lambda_handler.py`) do not consume queue messages and should not set `CLEANARR_WEBHOOK_QUEUE_POLLING`
- The dual-mode Lambda image (`apps/lambda`) processes SQS records when present;
  otherwise non-HTTP invokes run full-library cleanup (EventBridge/manual). Use
  `CLEANARR_WEBHOOK_QUEUE_POLLING=false` when SQS is delivered only via event
  source mappings.
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

## Dry-run report artifacts (job mode)

When `CLEANARR_DRY_RUN=true`, a completed **scheduled job** run writes structured report artifacts under `CLEANARR_DRY_RUN_REPORT_DIR`:

| File | Purpose |
| --- | --- |
| `cleanarr-dry-run-report.json` | Machine-readable per-user report (`schema_version: 1`) |
| `cleanarr-dry-run-report.md` | Human-readable Markdown summary of the same data |

Report contents:

- `summary.would_delete` / `summary.skipped` / `summary.skip_breakdown`
- `users.<profile>.would_delete[]` and `users.<profile>.skipped[]`
- Each skip includes `skip_category`: `safe`, `kids`, `policy`, `protected`, `unmatched`, or `error`
- Decisions without a known profile appear under `_unattributed`
- Sensitive values are redacted (same rules as decision JSONL)

### Webhook limitations

Dry-run **aggregate** report artifacts are a **job-mode** feature (full-library pass). Webhook and SQS consumer runtimes remain event-driven and do **not** write the per-user JSON/Markdown dry-run report files:

- They still append individual decisions to `CLEANARR_DECISION_REPORT_FILE` (JSONL) when configured.
- A single webhook event only evaluates one (or a small set of) titles, so a complete per-user library report is not produced.
- Operators who need a full “what would be deleted for each profile” artifact should run the job image with `CLEANARR_DRY_RUN=true` and collect files from `CLEANARR_DRY_RUN_REPORT_DIR` (for example via a mounted logs volume or `kubectl cp`).

