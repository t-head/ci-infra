#!/usr/bin/env bash
set -euo pipefail

POLL_INTERVAL=30
JOB_NAME="${JOB_NAME:?}"
NAMESPACE="${INPUT_NAMESPACE:-default}"
NNODES="${INPUT_NNODES:-0}"
[ "$NNODES" -eq 0 ] && NNODES=1
START_TIME="${JOB_START_TIME:?}"
DEADLINE="${JOB_DEADLINE:?}"
TASK_FILE="/wl_nas/devops/ppu-dashboard/tasks/${JOB_NAME}.json"
POD_W0="${JOB_NAME}-worker-0"
FINAL_STATUS="failed"
LOG_PID=""
WAIT_PID=""
DRAIN_LOGS=false
LAUNCH_MODE="${INPUT_LAUNCH_MODE:-each-node}"
MASTER_MULTI=false
[ "$LAUNCH_MODE" != master ] || [ "$NNODES" -le 1 ] || MASTER_MULTI=true
SHUTDOWN_DEADLINE=$DEADLINE
[ "$MASTER_MULTI" != true ] || SHUTDOWN_DEADLINE=$((DEADLINE + 10))
READY_WORKERS=" "
NOT_READY_SINCE=()

set_output() {
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "$1=$2" >> "$GITHUB_OUTPUT"
  else
    echo "[output] $1=$2"
  fi
}
set_output "job_status" "$FINAL_STATUS"

print_failed_worker_logs() {
  local pod
  while IFS= read -r pod; do
    [ -n "$pod" ] || continue
    echo "::group::Failed worker log: $pod"
    if ! timeout --signal=TERM --kill-after=2s 10s \
      kubectl logs "$pod" -c worker -n "$NAMESPACE" --timestamps=true; then
      echo "Unable to read the failed worker log before cleanup: $pod"
    fi
    echo "::endgroup::"
  done < <(printf '%s' "$POD_SNAPSHOT" | jq -r '
    .items[] |
    select(.status.phase == "Failed" or
      any(.status.initContainerStatuses[]?; (.state.terminated.exitCode // 0) != 0) or
      any(.status.containerStatuses[]?; (.state.terminated.exitCode // 0) != 0)) |
    .metadata.name' | sort -u)
}

_on_exit() {
  local rc=$?
  trap - EXIT TERM INT
  if [ -n "$WAIT_PID" ]; then
    kill "$WAIT_PID" 2>/dev/null || true
    wait "$WAIT_PID" 2>/dev/null || true
  fi
  if [ -n "$LOG_PID" ]; then
    if [ "$DRAIN_LOGS" != true ]; then
      kill "$LOG_PID" 2>/dev/null || true
    fi
    wait "$LOG_PID" 2>/dev/null || true
  fi
  JOB_STATUS="$FINAL_STATUS" JOB_START_TIME="$START_TIME" \
    bash "$(dirname "${BASH_SOURCE[0]}")/report-job.sh" || true
  exit "$rc"
}
_on_signal() {
  echo "Workload monitoring cancelled"
  FINAL_STATUS="cancelled"
  DRAIN_LOGS=false
  set_output "job_status" "$FINAL_STATUS"
  exit 130
}
trap '_on_exit' EXIT
trap '_on_signal' TERM INT

if [ "$MASTER_MULTI" = true ]; then
  echo "Streaming worker-0 logs; supervising ${NNODES} nodes until master completes"
else
  echo "Streaming worker-0 logs; waiting for all ${NNODES} worker pods to finish"
fi
(
  trap - EXIT
  _log_child=""
  trap 'kill "$_log_child" 2>/dev/null; wait "$_log_child" 2>/dev/null; exit 0' TERM INT
  while true; do
    _remaining=$(( DEADLINE - $(date +%s) ))
    [ "$_remaining" -le 0 ] && break
    timeout --signal=TERM --kill-after=5s "${_remaining}s" \
      kubectl logs "$POD_W0" -c worker -n "$NAMESPACE" -f --pod-running-timeout=5s 2>/dev/null &
    _log_child=$!
    wait "$_log_child" && break
    _phase=$(timeout --signal=KILL 10s kubectl get pod "$POD_W0" -n "$NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null || true)
    case "$_phase" in
      Succeeded|Failed)
        timeout --signal=TERM --kill-after=5s 10s kubectl logs "$POD_W0" -c worker -n "$NAMESPACE" 2>/dev/null &
        _log_child=$!
        wait "$_log_child" || true
        break
        ;;
    esac
    sleep 3 &
    _log_child=$!
    wait "$_log_child" || true
  done
) > >(sed -u 's/^/[worker-0] /') &
LOG_PID=$!

while true; do
  NOW=$(date +%s)
  ELAPSED=$(( NOW - START_TIME ))
  REQUEST_SECONDS=$(( DEADLINE - NOW ))
  if [ "$REQUEST_SECONDS" -le 0 ]; then
    FINAL_STATUS="timeout"
    REQUEST_SECONDS=$((SHUTDOWN_DEADLINE - NOW))
    [ "$REQUEST_SECONDS" -gt 0 ] || REQUEST_SECONDS=10
  fi
  [ "$REQUEST_SECONDS" -gt 10 ] && REQUEST_SECONDS=10
  if POD_SNAPSHOT=$(timeout --signal=KILL "${REQUEST_SECONDS}s" \
    kubectl get pods -n "$NAMESPACE" -l "ppu-job=$JOB_NAME" -o json); then
    NOW=$(date +%s)
  else
    rc=$?
    [ "$(date +%s)" -lt "$DEADLINE" ] || FINAL_STATUS="timeout"
    exit "$rc"
  fi
  if [ "$MASTER_MULTI" = true ]; then
    POD_SNAPSHOT=$(printf '%s' "$POD_SNAPSHOT" | jq --argjson deadline "$DEADLINE" --arg prefix "${JOB_NAME}-worker-" '
      .items |= map(. as $pod |
        ((.status.containerStatuses[0].state.terminated.message // "" | fromjson?) // {}) as $message |
        . + {_cluster_timeout: (.status.reason == "DeadlineExceeded" or
          (($message | type) == "object" and $message.source == "cluster-master-runtime" and
           $message.outcome == "timeout" and $message.deadline == $deadline and
           $message.rank == (($pod.metadata.name | ltrimstr($prefix) | tonumber?) // -1))),
          _cluster_command_finished_at:
            (if ($message | type) == "object" and $message.source == "cluster-master-runtime" and
                $message.outcome == "command" and $message.deadline == $deadline and $message.rank == 0 and
                $pod.metadata.name == ($prefix + "0") and
                ($message.command_finished_at | type) == "string" and
                ($message.command_finished_at | test("^[0-9]{1,10}\\.[0-9]{9}$")) and
                $message.exit_code == $pod.status.containerStatuses[0].state.terminated.exitCode and
                ($message.exit_code | type) == "number" and $message.exit_code >= 0 and $message.exit_code <= 255 and
                $message.exit_code == ($message.exit_code | floor)
             then $message.command_finished_at else "" end)})')
  fi
  SUCCEEDED=$(printf '%s' "$POD_SNAPSHOT" | jq '[.items[] | select(.status.phase == "Succeeded")] | length')
  FAILED=$(printf '%s' "$POD_SNAPSHOT" | jq '[.items[] | select(.status.phase == "Failed" and ._cluster_timeout != true)] | length')
  TIMED_OUT=$(printf '%s' "$POD_SNAPSHOT" | jq '[.items[] | select(.status.phase == "Failed" and ._cluster_timeout == true)] | length')
  RUNNING=$(printf '%s' "$POD_SNAPSHOT" | jq '[.items[] | select(.status.phase == "Running")] | length')
  STARTING=$(printf '%s' "$POD_SNAPSHOT" | jq '[.items[] | select(.status.phase == "Pending")] | length')

  if [ -f "$TASK_FILE" ] && { [ "$RUNNING" -gt 0 ] || [ "$SUCCEEDED" -gt 0 ]; }; then
    (
      flock -w 5 9 || exit 0
      if jq -e '(.status == "pending" or .status == "scheduled") and ((.end_time // "") == "")' "$TASK_FILE" >/dev/null; then
        jq '.status = "running"' "$TASK_FILE" > "${TASK_FILE}.tmp" && mv "${TASK_FILE}.tmp" "$TASK_FILE"
      fi
    ) 9> "${TASK_FILE}.lock"
  fi

  MASTER_COMPLETED=0
  WORKER_COMPLETED=0
  if [ "$MASTER_MULTI" = true ]; then
    MASTER_COMPLETED=$(printf '%s' "$POD_SNAPSHOT" | jq --arg master "$POD_W0" '[.items[] | select(.metadata.name == $master and .status.phase == "Succeeded")] | length')
    WORKER_COMPLETED=$(printf '%s' "$POD_SNAPSHOT" | jq --arg master "$POD_W0" '[.items[] | select(.metadata.name != $master and .status.phase == "Succeeded")] | length')
    POD_COUNT=$(printf '%s' "$POD_SNAPSHOT" | jq '.items | length')
    if [ "$POD_COUNT" -ne "$NNODES" ]; then
      echo "::error::Master allocation lost a worker (expected=$NNODES actual=$POD_COUNT)"
      exit 0
    fi
    ABNORMAL=$(printf '%s' "$POD_SNAPSHOT" | jq '[.items[] | select(._cluster_timeout != true and (.metadata.deletionTimestamp != null or .status.phase == "Unknown" or (.status.phase != "Failed" and (any(.status.initContainerStatuses[]?; (.state.terminated.exitCode // 0) != 0) or any(.status.containerStatuses[]?; (.state.terminated.exitCode // 0) != 0)))))] | length')
    if [ "$ABNORMAL" -gt 0 ]; then
      echo "::error::Master allocation contains a deleting, unknown or initialization-failed pod"
      print_failed_worker_logs
      FINAL_STATUS="failed"
      exit 0
    fi
    while IFS='|' read -r name ready; do
      rank=${name##*-}
      if [ "$ready" = True ]; then
        case "$READY_WORKERS" in *" $name "*) ;; *) READY_WORKERS+="$name " ;; esac
        unset 'NOT_READY_SINCE[rank]'
      else
        case "$READY_WORKERS" in
          *" $name "*)
            [ -n "${NOT_READY_SINCE[$rank]:-}" ] || NOT_READY_SINCE[$rank]=$NOW
            if [ $((NOW - NOT_READY_SINCE[rank])) -ge 30 ]; then
              echo "::error::A previously ready worker remained unavailable for 30 seconds"
              exit 0
            fi
            ;;
        esac
      fi
    done < <(printf '%s' "$POD_SNAPSHOT" | jq -r --arg master "$POD_W0" '.items[] | select(.metadata.name != $master and .status.phase == "Running") | [.metadata.name, ([.status.conditions[]? | select(.type == "Ready") | .status] | first // "False")] | join("|")')
  fi

  if [ "$NOW" -ge "$DEADLINE" ]; then
    FINAL_STATUS="timeout"
    SUCCEEDED=0
    FAILED=0
    MASTER_COMPLETED=0
    WORKER_COMPLETED=0
    while IFS='|' read -r pod_name phase finished_at deadline_exit command_finished_at; do
      [ "$deadline_exit" != true ] || continue
      [ -n "$finished_at" ] || continue
      finished_epoch=$(date -d "$finished_at" +%s)
      if [ -n "$command_finished_at" ]; then
        command_seconds=${command_finished_at%%.*}
        command_fraction=${command_finished_at#*.}
        [ "$command_seconds" -le "$DEADLINE" ] && [ "$command_seconds" -le "$finished_epoch" ] || continue
        [ "$finished_epoch" -le "$SHUTDOWN_DEADLINE" ] || continue
        if [ "$finished_epoch" -eq "$SHUTDOWN_DEADLINE" ] && [[ "$finished_at" =~ \.([0-9]+) ]] \
          && [[ "${BASH_REMATCH[1]}" == *[1-9]* ]]; then continue; fi
        if [ "$command_seconds" -eq "$DEADLINE" ] && [[ "$command_fraction" == *[1-9]* ]]; then continue; fi
      else
        [ "$finished_epoch" -le "$DEADLINE" ] || continue
        if [ "$finished_epoch" -eq "$DEADLINE" ] && [[ "$finished_at" =~ \.([0-9]+) ]] \
          && [[ "${BASH_REMATCH[1]}" == *[1-9]* ]]; then continue; fi
      fi
      case "$phase" in
        Succeeded)
          SUCCEEDED=$(( SUCCEEDED + 1 ))
          if [ "$pod_name" = "$POD_W0" ]; then
            MASTER_COMPLETED=1
          else
            WORKER_COMPLETED=$(( WORKER_COMPLETED + 1 ))
          fi
          ;;
        Failed) FAILED=$(( FAILED + 1 )) ;;
      esac
    done < <(printf '%s' "$POD_SNAPSHOT" | jq -r '.items[] | [.metadata.name, .status.phase, (.status.containerStatuses[0].state.terminated.finishedAt // ""), (._cluster_timeout // false), (._cluster_command_finished_at // "")] | join("|")')
  fi

  if [ "$MASTER_MULTI" = true ] && [ "$WORKER_COMPLETED" -gt 0 ]; then
    echo "::error::A worker SSH service exited before allocation cleanup"
    FINAL_STATUS="failed"
    exit 0
  fi
  COMPLETE=false
  if [ "$MASTER_MULTI" = true ]; then
    [ "$MASTER_COMPLETED" -eq 0 ] || COMPLETE=true
  elif [ "$SUCCEEDED" -eq "$NNODES" ]; then
    COMPLETE=true
  fi
  if [ "$COMPLETE" = true ] || [ "$FAILED" -gt 0 ]; then
    W0_PHASE=$(printf '%s' "$POD_SNAPSHOT" | jq -r --arg pod "$POD_W0" '.items[] | select(.metadata.name == $pod) | .status.phase')
    case "$W0_PHASE" in Succeeded|Failed) DRAIN_LOGS=true ;; esac
    if [ "$FAILED" -gt 0 ]; then
      echo "::error::Worker execution failed (failed=$FAILED)"
      print_failed_worker_logs
      FINAL_STATUS="failed"
      exit 0
    fi
    FINAL_STATUS="succeeded"
    if [ "$MASTER_MULTI" = true ]; then
      echo "Master completed successfully; worker services will be cleaned up"
    else
      echo "All worker pods succeeded"
    fi
    exit 0
  fi

  if [ "$MASTER_MULTI" = true ] && [ "$NOW" -ge "$DEADLINE" ] && [ "$NOW" -lt "$SHUTDOWN_DEADLINE" ] \
    && printf '%s' "$POD_SNAPSHOT" | jq -e --arg master "$POD_W0" 'any(.items[]; .metadata.name == $master and .status.phase == "Running")' >/dev/null; then
    WAIT_SECONDS=$((SHUTDOWN_DEADLINE - NOW))
    [ "$WAIT_SECONDS" -le 1 ] || WAIT_SECONDS=1
    sleep "$WAIT_SECONDS" &
    WAIT_PID=$!
    wait "$WAIT_PID"
    WAIT_PID=""
    continue
  fi

  if [ "$TIMED_OUT" -gt 0 ]; then
    FINAL_STATUS="timeout"
    echo "::error::Master allocation reached its framework deadline"
    print_failed_worker_logs
    exit 0
  fi

  if [ "$NOW" -ge "$DEADLINE" ]; then
    echo "::error::Workload exceeded the $(( DEADLINE - START_TIME ))s scheduling and execution budget"
    exit 0
  fi

  echo "${ELAPSED}s elapsed - starting=$STARTING running=$RUNNING succeeded=$SUCCEEDED failed=$FAILED"
  if [ "$STARTING" -gt 0 ]; then
    printf '%s' "$POD_SNAPSHOT" | jq -r '
      .items[] | select(.status.phase == "Pending") |
      "  \(.metadata.name): \(.status.containerStatuses[0].state.waiting.reason // "starting") \(.status.containerStatuses[0].state.waiting.message // "")"'
  fi
  WAIT_SECONDS=$(( DEADLINE - $(date +%s) ))
  [ "$WAIT_SECONDS" -gt "$POLL_INTERVAL" ] && WAIT_SECONDS=$POLL_INTERVAL
  if [ "$WAIT_SECONDS" -gt 0 ]; then
    sleep "$WAIT_SECONDS" &
    WAIT_PID=$!
    wait "$WAIT_PID"
    WAIT_PID=""
  fi
done
