#!/usr/bin/env bash
set -euo pipefail

NTFY_BASE_URL="${NTFY_BASE_URL:-https://ntfy.sh}"
NTFY_TOPIC="${NTFY_TOPIC:-}"
NTFY_TOKEN="${NTFY_TOKEN:-}"
NTFY_TITLE="${NTFY_TITLE:-GitHub Actions}"
NTFY_PRIORITY="${NTFY_PRIORITY:-default}"
NTFY_TAGS="${NTFY_TAGS:-github}"
NTFY_MESSAGE="${NTFY_MESSAGE:-}"

if [[ -z "${NTFY_TOPIC}" ]] || [[ -z "${NTFY_MESSAGE}" ]]; then
  echo "ntfy topic or message not set; skipping notification."
  exit 0
fi

headers=(-H "Title: ${NTFY_TITLE}" -H "Priority: ${NTFY_PRIORITY}" -H "Tags: ${NTFY_TAGS}")
if [[ -n "${NTFY_TOKEN}" ]]; then
  headers+=(-H "Authorization: Bearer ${NTFY_TOKEN}")
fi

curl -fsS -X POST "${NTFY_BASE_URL%/}/${NTFY_TOPIC}" "${headers[@]}" --data-binary "${NTFY_MESSAGE}" >/dev/null
