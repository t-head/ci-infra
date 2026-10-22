#!/usr/bin/env bash
set -uo pipefail

log_info()  { echo "[cleanup] $*"; }
log_warn()  { echo "[cleanup] [warn] $*"; }
log_error() { echo "::error::[cleanup] $*"; }

NAMESPACE="${INPUT_NAMESPACE:-default}"
JOB_NAME="${JOB_NAME:-}"
# cleanup_policy has been removed: always perform full cleanup.
NNODES="${INPUT_NNODES:-0}"
SOURCE_STAGE_DIR="${SOURCE_STAGE_DIR:-}"

# Resolve job name outputs job_name before any resources are created; an empty value means none exist yet.
if [ -z "$JOB_NAME" ]; then
  log_warn "job_name is unavailable; skipping cleanup"
  exit 0
fi

# Always remove the source tarball staged on NAS, regardless of cleanup_policy, since it is temporary transfer storage.
if [ -n "${SOURCE_STAGE_DIR:-}" ] && [ -f "$SOURCE_STAGE_DIR" ]; then
  log_info "Removing source tarball staged on NAS: $SOURCE_STAGE_DIR"
  rm -f "$SOURCE_STAGE_DIR"
  _parent_dir="$(dirname "$SOURCE_STAGE_DIR")"
  if [ -d "$_parent_dir" ] && [ -z "$(ls -A "$_parent_dir" 2>/dev/null)" ]; then
    rmdir "$_parent_dir" 2>/dev/null || true
  fi
fi

log_info "Performing full cleanup: $JOB_NAME (namespace=$NAMESPACE)"

# --- Update the task record ---
TASK_DIR="/wl_nas/devops/ppu-dashboard/tasks"
TASK_FILE="${TASK_DIR}/${JOB_NAME}.json"
TASK_STATUS="${INPUT_JOB_STATUS:-}"
case "$TASK_STATUS" in
  ""|unknown|pending|running|scheduled)
    if [ -f "$TASK_FILE" ]; then
      TASK_STATUS="$(jq -r '.status // empty' "$TASK_FILE")"
    fi
    ;;
esac
case "$TASK_STATUS" in
  ""|unknown|pending|running|scheduled)
    TASK_STATUS="failed"
    [ "${INPUT_WORKFLOW_STATUS:-}" = "cancelled" ] && TASK_STATUS="cancelled"
    ;;
esac
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "job_status=$TASK_STATUS" >> "$GITHUB_OUTPUT"
fi
if [ -f "$TASK_FILE" ] && command -v jq >/dev/null 2>&1; then
  TASK_END_TIME="$(date +%Y-%m-%dT%H:%M:%S%z)"
  jq --arg end_time "$TASK_END_TIME" --arg status "$TASK_STATUS" \
    '.end_time = $end_time | .status = $status' \
    "$TASK_FILE" > "${TASK_FILE}.tmp" && mv "${TASK_FILE}.tmp" "$TASK_FILE"
  log_info "Task record updated: ${TASK_FILE} (status=${TASK_STATUS})"
fi

POD_SELECTOR="ppu-job=$JOB_NAME"

# Apply a single time limit to cluster cleanup and keep a final force-delete allowance.
CLEANUP_TIMEOUT=60
FORCE_RESERVE=10
CLEANUP_START=$(date +%s)
CLEANUP_DEADLINE=$((CLEANUP_START + CLEANUP_TIMEOUT))
CLEANUP_CONFIRMED=true
PODS_CONFIRMED=false

kube_request() {
  local reserve=$1 remaining request_seconds
  shift
  remaining=$((CLEANUP_DEADLINE - $(date +%s) - reserve))
  [ "$remaining" -gt 0 ] || return 1
  request_seconds=$remaining
  [ "$request_seconds" -le 10 ] || request_seconds=10
  timeout --signal=KILL "${request_seconds}s" \
    kubectl "$@" --request-timeout="${request_seconds}s"
}

cleanup_sleep() {
  local seconds=$1 reserve=$2 remaining
  remaining=$((CLEANUP_DEADLINE - $(date +%s) - reserve))
  [ "$remaining" -gt 0 ] || return 1
  [ "$seconds" -le "$remaining" ] || seconds=$remaining
  sleep "$seconds"
}

# Retry deletion to handle transient failures such as etcd NOSPACE.
kube_delete_retry() {
  local max_retries=3 delay=5 attempt
  for ((attempt=1; attempt<=max_retries; attempt++)); do
    if kube_request "$FORCE_RESERVE" "$@" 2>/dev/null; then return 0; fi
    if [ "$attempt" -lt "$max_retries" ]; then
      log_warn "Deletion failed (attempt ${attempt}); retrying in at most ${delay}s..."
      cleanup_sleep "$delay" "$FORCE_RESERVE" || break
    fi
  done
  log_warn "Deletion remains unconfirmed after the available attempts or time budget."
  return 1
}

log_info "Deleting pods: $JOB_NAME (namespace=$NAMESPACE)"
kube_delete_retry delete pods -n "$NAMESPACE" -l "$POD_SELECTOR" \
  --wait=false --ignore-not-found=true || CLEANUP_CONFIRMED=false

# No Service or PodGroup exists for standalone workloads.
if [ "$NNODES" != "0" ]; then
  log_info "Deleting service: $JOB_NAME"
  kube_delete_retry delete svc "$JOB_NAME" -n "$NAMESPACE" \
    --wait=false --ignore-not-found=true || CLEANUP_CONFIRMED=false

  log_info "Deleting podgroup: $JOB_NAME"
  kube_delete_retry delete podgroups.scheduling.x-k8s.io "$JOB_NAME" -n "$NAMESPACE" \
    --wait=false --ignore-not-found=true || CLEANUP_CONFIRMED=false
else
  log_info "Single-card/multi-card mode (nnodes=0): no service/podgroup to clean up; skipping."
fi

log_info "Waiting for associated pods to terminate..."
FORCE_ATTEMPTED=false
while true; do
  REMAINING=$((CLEANUP_DEADLINE - $(date +%s)))
  [ "$REMAINING" -gt 0 ] || break

  if [ "$REMAINING" -le "$FORCE_RESERVE" ] && [ "$FORCE_ATTEMPTED" = false ]; then
    FORCE_ATTEMPTED=true
    log_warn "Pod removal is not confirmed; issuing a bounded force-delete request."
    if ! kube_request 0 delete pods -n "$NAMESPACE" -l "$POD_SELECTOR" \
      --force --grace-period=0 --wait=false --ignore-not-found=true 2>/dev/null; then
      CLEANUP_CONFIRMED=false
      log_warn "The force-delete request failed or reached the cleanup deadline."
    fi
    continue
  fi

  REQUEST_RESERVE=$FORCE_RESERVE
  [ "$FORCE_ATTEMPTED" = false ] || REQUEST_RESERVE=0
  if POD_LIST=$(kube_request "$REQUEST_RESERVE" get pods -n "$NAMESPACE" \
    -l "$POD_SELECTOR" --no-headers 2>/dev/null); then
    POD_COUNT=0
    while IFS= read -r pod_line; do
      if [ -n "$pod_line" ]; then POD_COUNT=$((POD_COUNT + 1)); fi
    done <<< "$POD_LIST"
    if [ "$POD_COUNT" -eq 0 ]; then
      PODS_CONFIRMED=true
      break
    fi
    ELAPSED=$(( $(date +%s) - CLEANUP_START ))
    echo "  Waiting for ${POD_COUNT} pods to terminate... (${ELAPSED}s/${CLEANUP_TIMEOUT}s)"
  else
    log_warn "Could not query pods; cleanup is unverified and will be checked again within the time limit."
  fi
  cleanup_sleep 5 "$REQUEST_RESERVE" || continue
done

if [ -d "/tmp/training-manifests" ]; then
  log_info "Removing temporary manifest files"
  rm -rf /tmp/training-manifests
fi

if [ "$CLEANUP_CONFIRMED" = true ] && [ "$PODS_CONFIRMED" = true ]; then
  log_info "All associated pods have terminated."
  log_info "Cleanup complete: $JOB_NAME"
else
  log_warn "Cleanup ended without confirmed cluster resource removal: $JOB_NAME. Finalizers may still retain pods; they were left untouched."
fi
