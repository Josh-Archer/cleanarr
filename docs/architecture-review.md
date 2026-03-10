# Architecture Review Gate

Review before first public release:

- Shared cleanup logic lives in `cleanarr_runtime/`.
- `apps/job` and `apps/webhook` are harnesses only.
- No cluster-specific manifests or infra automation live in this repo.
- Public interfaces are documented: env vars, image names, entrypoints.
- Downstream integration uses images, not source-copy or ConfigMap overrides.
- There is only one tracked package layout and no duplicate generated tree.
