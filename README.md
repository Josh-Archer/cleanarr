# Cleanarr Runtime

`cleanarr-runtime` packages the reusable runtime for media cleanup automation:

- `job`: scheduled cleanup against Plex, Sonarr, Radarr, and Transmission
- `webhook`: Plex webhook receiver with optional event-driven deletion handling

This repository is intended to be public-safe. Cluster-specific manifests, secrets, overlays, and infrastructure wiring stay in a private downstream repository.

## Images

- `ghcr.io/<owner>/cleanarr-runtime-job`
- `ghcr.io/<owner>/cleanarr-runtime-webhook`

Stable releases use semver tags such as `v0.1.0`. Release publishing should happen from semver tags after the documented review gates are approved.
Public pull requests stay on GitHub-hosted runners. Pushes and releases can opt into self-hosted runners by setting `USE_SELF_HOSTED=true` and `SELF_HOSTED_RUNNER_LABEL=<label>`.

## Quick Start

1. Copy `.env.example` to `.env`.
2. Set the required Plex, Sonarr, and Radarr credentials.
3. Run the job locally:

```bash
python -m pip install -e .[dev]
python apps/job/main.py
```

4. Run the webhook locally:

```bash
python -m pip install -e .[dev]
python apps/webhook/main.py
```

## Public/Private Boundary

Public repo:

- reusable Python runtime
- webhook and cronjob harnesses
- tests, Dockerfiles, CI/release workflows
- generic example manifests and operator docs

Private downstream repo:

- Kubernetes overlays and environment-specific manifests
- secrets and external secret resources
- Cloud Run, OpenTofu, or other infrastructure wiring
- image pins and rollout orchestration

## Versioning

- `vMAJOR.MINOR.PATCH` for releases
- `vMAJOR.MINOR` convenience tags
- `sha-<commit>` immutable build tags

Breaking changes to environment variables, image names, or harness behavior require a major release. The package version is sourced from `pyproject.toml` and exposed at runtime via package metadata.

## Documentation

- [Configuration](./docs/configuration.md)
- [Release Process](./docs/release.md)
- [Architecture Review Gate](./docs/architecture-review.md)
- [Security Review Gate](./docs/security-review.md)
