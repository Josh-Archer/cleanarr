# Release Process

1. Run CI and ensure both Dockerfiles build.
2. Complete the architecture and security review gates.
3. Set repository variables `ARCHITECTURE_REVIEW_TAG=vMAJOR.MINOR.PATCH` and `SECURITY_REVIEW_TAG=vMAJOR.MINOR.PATCH` for the exact tag you intend to release.
4. If you want release jobs on self-hosted runners, set `USE_SELF_HOSTED=true` and `SELF_HOSTED_RUNNER_LABEL=<your-runner-label>`.
5. Tag a release with `vMAJOR.MINOR.PATCH` that matches `pyproject.toml`.
6. Let `release.yml` publish:
   - `vMAJOR.MINOR.PATCH`
   - `vMAJOR.MINOR`
   - `sha-<commit>`
7. Verify the published images exist in GHCR and the workflow completed successfully.
8. Only then update downstream manifests to the exact semver tag or digest.

Do not consume `latest` in downstream clusters.
