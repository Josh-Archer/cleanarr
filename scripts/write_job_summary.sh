#!/usr/bin/env bash
set -euo pipefail

summary_file="${GITHUB_STEP_SUMMARY:?GITHUB_STEP_SUMMARY is not set}"
job_title="${JOB_TITLE:-${GITHUB_JOB}}"
job_status="${JOB_STATUS:-unknown}"
event_name="${EVENT_NAME:-${GITHUB_EVENT_NAME}}"
ref="${REF:-${GITHUB_REF}}"
sha="${SHA:-${GITHUB_SHA}}"
run_number="${RUN_NUMBER:-${GITHUB_RUN_NUMBER}}"

cat <<EOF >> "${summary_file}"
## ${job_title} summary
- **Result**: ${job_status}
- **Triggered by**: ${event_name}
- **Ref**: ${ref}
- **SHA**: ${sha}
- **Run**: #${run_number}
EOF
