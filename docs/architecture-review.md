# Architecture Review Gate

Review before first public release:

- Shared cleanup logic lives in `cleanarr/`.
- `apps/job` and `apps/webhook` are harnesses only.
- The public Python API lives in `cleanarr/` only; there is no parallel legacy shim surface.
- No cluster-specific manifests or infra automation live in this repo.
- Public interfaces are documented: env vars, image names, entrypoints.
- Staged queue-mode interfaces are documented (`CLEANARR_WEBHOOK_QUEUE_*`) with clear producer/consumer behavior.
- Downstream integration uses images, not source-copy or ConfigMap overrides.
- Versioning is single-sourced from `pyproject.toml` and enforced by release automation.
- Queue-mode runtime boundaries are explicit: webhook queue consumers remain in `apps/lambda`, while `apps/job` remains scheduled-cleanup-only.
