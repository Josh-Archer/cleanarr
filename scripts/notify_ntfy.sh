#!/usr/bin/env bash
set -euo pipefail

NTFY_BASE_URL="${NTFY_BASE_URL:-https://ntfy.sh}"
NTFY_TOPIC="${NTFY_TOPIC:-}"
NTFY_TOKEN="${NTFY_TOKEN:-}"
NTFY_TITLE="${NTFY_TITLE:-GitHub Actions}"
NTFY_PRIORITY="${NTFY_PRIORITY:-default}"
NTFY_TAGS="${NTFY_TAGS:-github}"
NTFY_MESSAGE="${NTFY_MESSAGE:-}"

if [[ -z "${NTFY_TOKEN}" || -z "${NTFY_TOPIC}" || -z "${NTFY_MESSAGE}" ]]; then
  echo "ntfy inputs incomplete; skipping notification."
  exit 0
fi

NTFY_URL="${NTFY_BASE_URL%/}/${NTFY_TOPIC}"

if ! curl -sS -X POST "${NTFY_URL}" \
  -H "Authorization: Bearer ${NTFY_TOKEN}" \
  -H "Title: ${NTFY_TITLE}" \
  -H "Priority: ${NTFY_PRIORITY}" \
  -H "Tags: ${NTFY_TAGS}" \
  --data-binary "${NTFY_MESSAGE}" >/dev/null; then
  echo "Failed to send ntfy notification to ${NTFY_TOPIC}."
fi

