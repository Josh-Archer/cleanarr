# Architecture

The public runtime repository is intentionally split into three layers:

- `cleanarr/`: shared library code used by both runtime harnesses
- `apps/job/`: thin cron/job entrypoint around `MediaCleanup`
- `apps/webhook/`: thin Flask entrypoint around the shared webhook app
- `apps/lambda/`: AWS Lambda container for SQS webhook consumption and
  EventBridge/manual full-library cleanup (dual-mode single function)

Design constraints:

- No cluster-specific manifests, secrets, overlays, or infrastructure code live here.
- No private hostnames, domains, usernames, or local datasets are committed here.
- Runtime defaults are generic and env-driven so downstream repos can supply their own wiring.
- Downstream private repos own Kubernetes overlays, secret material, Cloud Run or ingress setup, and image pinning.
- Proxy and webhook runtime behavior stay in this repository; downstream repos own IAM roles, queue resources, manifests, and rollout policy.
- Multi-user delete policy defaults to `require_all_users` so shared-library watch state cannot delete media while another household member is still mid-season (exact-item and watched-ahead paths share the same policy). See `docs/configuration.md`.

Dry-run reporting:

- Job mode (`MediaCleanup.run`) can emit per-user dry-run JSON/Markdown artifacts when `CLEANARR_DRY_RUN=true`.
- Webhook / SQS consumer paths remain event-scoped and only use decision JSONL; they do not produce the aggregate library dry-run report.

Queue decoupling (issue #8):

- In `direct` mode, webhook events are processed immediately by the webhook runtime.
- In `sqs` mode, webhook runtime enqueues actionable events and returns quickly.
- SQS webhook consumer runtime polls SQS and executes event actions (deletion + sync) out of band.
- Scheduled runtimes (`apps/job/main.py` and `apps/job/lambda_handler.py`) intentionally do not read SQS or queue messages.
- The Lambda container image (`apps/lambda`) is dual-mode for downstream single-function deploys:
  SQS event records process the queue; non-HTTP, non-SQS invokes run full-library
  cleanup (EventBridge/manual). Dedicated scheduled job images remain available.
- The in-cluster proxy publishes directly to SQS when a queue URL is configured; Lambda URL forwarding remains a compatibility sink only.
- Downstream infrastructure can switch back to `direct` mode for automatic fallback when budget alarms trigger.
- Direct Plex webhook handling remains a first-class runtime mode and is not replaced by the proxy path.

- Proxy and webhook runtime behavior stay in this repository; downstream repos own IAM roles, queue resources, manifests, and rollout policy.
- Optional Lidarr music cleanup is feature-flagged (`CLEANARR_LIDARR_ENABLE`, default off). Plex owns play/listened signals; Lidarr owns managed track files, tags, and deletes.
