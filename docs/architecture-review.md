# Architecture Review Gate

Review before first public release:

- Shared cleanup logic lives in `cleanarr_runtime/`.
- `apps/job` and `apps/webhook` are harnesses only.
- The public Python API lives in `cleanarr_runtime/` only; there is no parallel legacy shim surface.
- No cluster-specific manifests or infra automation live in this repo.
- Public interfaces are documented: env vars, image names, entrypoints.
- Downstream integration uses images, not source-copy or ConfigMap overrides.
- Versioning is single-sourced from `pyproject.toml` and enforced by release automation.
