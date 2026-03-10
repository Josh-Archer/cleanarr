# Release Process

1. Run CI and ensure both Dockerfiles build.
2. Complete the architecture and security review gates.
3. Set repository variables `ARCHITECTURE_REVIEW_APPROVED=true` and `SECURITY_REVIEW_APPROVED=true`.
4. Set `USE_SELF_HOSTED=true` once the repository has access to the shared cluster runner set.
5. Tag a release with `vMAJOR.MINOR.PATCH` that matches `pyproject.toml`.
6. Let `release.yml` publish:
   - `vMAJOR.MINOR.PATCH`
   - `vMAJOR.MINOR`
   - `sha-<commit>`
7. Verify the published images exist in GHCR and the workflow completed successfully.
8. Only then update downstream manifests to the exact semver tag or digest.

Do not consume `latest` in downstream clusters.
