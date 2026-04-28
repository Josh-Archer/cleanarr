# Architecture

The public runtime repository is intentionally split into three layers:

- `cleanarr/`: shared library code used by both runtime harnesses
- `apps/job/`: thin cron/job entrypoint around `MediaCleanup`
- `apps/webhook/`: thin Flask entrypoint around the shared webhook app

Design constraints:

- No cluster-specific manifests, secrets, overlays, or infrastructure code live here.
- No private hostnames, domains, usernames, or local datasets are committed here.
- Runtime defaults are generic and env-driven so downstream repos can supply their own wiring.
- Downstream private repos own Kubernetes overlays, secret material, Cloud Run or ingress setup, and image pinning.
- Proxy and webhook runtime behavior stay in this repository; downstream repos own IAM roles, queue resources, manifests, and rollout policy.

Queue decoupling (issue #8):

- In `direct` mode, webhook events are processed immediately by the webhook runtime.
- In `sqs` mode, webhook runtime enqueues actionable events and returns quickly.
- SQS webhook consumer runtime polls SQS and executes event actions (deletion + sync) out of band.
- Scheduled runtimes (`apps/job/main.py` and `apps/job/lambda_handler.py`) intentionally do not read SQS or queue messages.
- The in-cluster proxy publishes directly to SQS when a queue URL is configured; Lambda URL forwarding remains a compatibility sink only.
- Downstream infrastructure can switch back to `direct` mode for automatic fallback when budget alarms trigger.
- Direct Plex webhook handling remains a first-class runtime mode and is not replaced by the proxy path.
