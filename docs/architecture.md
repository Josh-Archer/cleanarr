# Architecture

The public runtime repository is intentionally split into three layers:

- `cleanarr_runtime/`: shared library code used by both runtime harnesses
- `apps/job/`: thin cron/job entrypoint around `MediaCleanup`
- `apps/webhook/`: thin Flask entrypoint around the shared webhook app

Design constraints:

- No cluster-specific manifests, secrets, overlays, or infrastructure code live here.
- No private hostnames, domains, usernames, or local datasets are committed here.
- Runtime defaults are generic and env-driven so downstream repos can supply their own wiring.
- Downstream private repos own Kubernetes overlays, secret material, Cloud Run or ingress setup, and image pinning.
