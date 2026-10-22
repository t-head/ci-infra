#!/usr/bin/env bash
set -uo pipefail

[ -n "${JOB_NAME:-}" ] || { echo "job_name is unavailable; skipping dmesg output"; exit 0; }
DMESG_DIR="/wl_nas/devops/dmesg/${GITHUB_RUN_ID:-0}-${GITHUB_RUN_ATTEMPT:-1}/${JOB_NAME}"

files=$(ls -1 "$DMESG_DIR" 2>/dev/null | grep -E '^worker-[0-9]+-.*\.log$' | sort -t- -k2,2n)
if [ -z "$files" ]; then
  echo "No dmesg logs found: ${DMESG_DIR}"
  exit 0
fi

echo "dmesg logs on NAS: ${DMESG_DIR}"
while IFS= read -r f; do echo "  ${DMESG_DIR}/${f}"; done <<< "$files"

# Kernel logs contain external content such as process names; disable workflow command processing.
token="dmesg-$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
while IFS= read -r f; do
  rank=${f#worker-}; node=${rank#*-}; rank=${rank%%-*}; node=${node%.log}
  echo "::group::dmesg worker-${rank} (${node})"
  echo "::stop-commands::${token}"
  awk 1 "${DMESG_DIR}/${f}"
  echo "::${token}::"
  echo "::endgroup::"
done <<< "$files"
