#!/usr/bin/env bash
# Mirror a digest-pinned base image to GHCR so builds do not depend on
# anonymous public.ecr.aws pulls (429 "toomanyrequests: Data limit exceeded").
#
# Usage: mirror_base_image.sh <source@sha256:digest> <ghcr-repository>
# Prints the digest-pinned mirror reference on stdout. If the mirror cannot be
# populated (e.g. read-only token on fork PRs) the source reference is printed.
# Retries with exponential backoff; MIRROR_MAX_ATTEMPTS / MIRROR_BASE_DELAY tune it.
set -euo pipefail

src="${1:?source image@digest required}"
dest_repo="${2:?destination repository required}"
max_attempts="${MIRROR_MAX_ATTEMPTS:-6}"
delay="${MIRROR_BASE_DELAY:-5}"

case "${src}" in
  *@sha256:*) ;;
  *) echo "source must be pinned by digest: ${src}" >&2; exit 2 ;;
esac

digest="${src##*@}"
dest_repo="$(printf '%s' "${dest_repo}" | tr '[:upper:]' '[:lower:]')"
mirror="${dest_repo}@${digest}"

retry() {
  local attempt=1 wait="${delay}"
  while true; do
    if "$@" >&2; then
      return 0
    fi
    if [ "${attempt}" -ge "${max_attempts}" ]; then
      return 1
    fi
    echo "attempt ${attempt}/${max_attempts} failed (likely 429); retrying in ${wait}s" >&2
    sleep "${wait}"
    attempt=$((attempt + 1))
    wait=$((wait * 2))
  done
}

if docker buildx imagetools inspect "${mirror}" >/dev/null 2>&1; then
  echo "${mirror}"
  exit 0
fi

# Copy the manifest list as-is, which preserves the digest.
if retry docker buildx imagetools create --tag "${dest_repo}:${digest#sha256:}" "${src}" \
  && docker buildx imagetools inspect "${mirror}" >/dev/null 2>&1; then
  echo "${mirror}"
else
  echo "could not mirror ${src}; falling back to source" >&2
  retry docker buildx imagetools inspect "${src}" || true
  echo "${src}"
fi
