#!/usr/bin/env bash
set -euo pipefail

POLL_INTERVAL=30
MANIFEST_DIR="/tmp/training-manifests"

mkdir -p "$MANIFEST_DIR"

log_info()  { echo "$*"; }
log_warn()  { echo "[warn] $*"; }
log_error() { echo "::error::$*"; }

set_output() {
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "$1=$2" >> "$GITHUB_OUTPUT"
  else
    echo "[output] $1=$2"
  fi
}

# --- Initialize default outputs so cleanup can read job_status even if the script exits unexpectedly ---
set_output "job_status" "failed"
set_output "duration_seconds" "0"
set_output "queue_duration_seconds" "0"
set_output "unscheduled_pods" "0"
set_output "pod_queue" ""
set_output "queue_reason" ""
set_output "occupied_ppu_card_hours" "0.0000"
set_output "execution_ppu_card_hours" "0.0000"

FINAL_STATUS="failed"
_on_exit() {
  local rc=$?
  trap - EXIT TERM INT
  if [ "$FINAL_STATUS" = "failed" ] && [ -n "${DEADLINE:-}" ] && [ "$(date +%s)" -ge "$DEADLINE" ]; then
    FINAL_STATUS="timeout"
    set_output "job_status" "$FINAL_STATUS"
  fi
  if [ -n "${WAIT_PID:-}" ]; then
    kill "$WAIT_PID" 2>/dev/null || true
    wait "$WAIT_PID" 2>/dev/null || true
  fi
  if [ "$FINAL_STATUS" != "scheduled" ] && [ -n "${START_TIME:-}" ]; then
    JOB_STATUS="$FINAL_STATUS" JOB_START_TIME="$START_TIME" \
      bash "$(dirname "${BASH_SOURCE[0]}")/report-job.sh" || true
  fi
  exit "$rc"
}
_on_signal() {
  log_warn "Scheduling cancelled"
  FINAL_STATUS="cancelled"
  set_output "job_status" "$FINAL_STATUS"
  exit 130
}
trap '_on_exit' EXIT
trap '_on_signal' TERM INT

# Escape special characters for safe embedding in a YAML double-quoted value: "...".
yaml_escape_value() {
  local val="$1"
  val="${val//\\/\\\\}"       # \ → \\
  val="${val//\"/\\\"}"       # " → \\"
  val="${val//$'\n'/\\n}"     # newline → \n
  val="${val//$'\t'/\\t}"     # tab → \t
  printf '%s' "$val"
}

NNODES="${INPUT_NNODES:-0}"
NPROC="${INPUT_NPROC_PER_NODE:-1}"

# nnodes=0 selects single-node mode: one pod, no gang scheduling or service.
SINGLE_MODE=false
if [ "$NNODES" -eq 0 ]; then
  SINGLE_MODE=true
  NNODES=1
fi
COMMAND="${INPUT_COMMAND:-}"
IMAGE="${INPUT_IMAGE:-}"
NAMESPACE="${INPUT_NAMESPACE:-default}"
TIMEOUT_MINUTES="${INPUT_TIMEOUT_MINUTES:-60}"
HOST_VOLUMES="${INPUT_HOST_VOLUMES:-}"
EXTRA_ENV="${INPUT_EXTRA_ENV:-}"
MASTER_PORT="${INPUT_MASTER_PORT:-29500}"
CLEANUP_POLICY="${INPUT_CLEANUP_POLICY:-always}"
NODE_SELECTOR="${INPUT_NODE_SELECTOR:-}"
SOURCE_STAGE_DIR="${SOURCE_STAGE_DIR:-}"
SOURCE_MOUNT_PATH="${INPUT_SOURCE_DIR:-/workspace/source}"
CONTAINER_OPTIONS="${CONTAINER_OPTIONS:-}"
DMESG_POLICY="${INPUT_DMESG_POLICY:-never}"

case "$DMESG_POLICY" in
  never|on_failure|always) ;;
  *)
    log_error "dmesg_policy must be never/on_failure/always; got '${DMESG_POLICY}'"
    exit 1
    ;;
esac

# Default NAS mounts, shared by the pod spec and host_volumes deduplication.
# Entry format: "volumeName|containerMountPath|hostPath"
DEFAULT_NAS_VOLUMES=(
  "nas-aisw|/nas_aisw|/nas_aisw"
  "wl-nas|/mnt/wl_nas|/wl_nas"
  "wl-nas-direct|/wl_nas|/wl_nas"
)

TIMEOUT=$(( TIMEOUT_MINUTES * 60 ))

if [ -z "$IMAGE" ]; then
  log_error "Input 'image' is required but empty."
  exit 1
fi
if [ -z "$COMMAND" ]; then
  log_error "Input 'command' is required but empty."
  exit 1
fi

RUN_ID="${GITHUB_RUN_ID:-0}"
RUN_ATTEMPT="${GITHUB_RUN_ATTEMPT:-1}"
JOB_NAME="${JOB_NAME:-}"
if [ -z "$JOB_NAME" ]; then
  log_error "JOB_NAME is empty (expected from the Resolve job name step)"
  exit 1
fi
set_output "job_name" "$JOB_NAME"
START_TIME=$(date +%s)
DEADLINE=$(( START_TIME + TIMEOUT ))
set_output "start_time" "$START_TIME"
set_output "deadline" "$DEADLINE"

kube_request() {
  local remaining=$(( DEADLINE - $(date +%s) ))
  [ "$remaining" -gt 0 ] || return 124
  [ "$remaining" -gt 10 ] && remaining=10
  # This kubectl's in-cluster credentials stop resolving when a nonzero request timeout is set.
  timeout --signal=KILL "${remaining}s" kubectl "$@"
}

log_info "Job name:   $JOB_NAME"
log_info "Namespace:  $NAMESPACE"
if [ "$SINGLE_MODE" = true ]; then
  if [ "$NPROC" -gt 1 ]; then
    log_info "Mode:       Multi-card (${NPROC} PPU)"
  else
    log_info "Mode:       Single-card (${NPROC} PPU)"
  fi
else
  log_info "Scale:      ${NNODES} node(s) × ${NPROC} PPU/node"
fi
log_info "Timeout:    ${TIMEOUT_MINUTES}min (${TIMEOUT}s)"

if [ "$SINGLE_MODE" = false ]; then
  if ! kube_request get crd podgroups.scheduling.x-k8s.io >/dev/null 2>&1; then
    log_error "PodGroup CRD (podgroups.scheduling.x-k8s.io) not found. Gang scheduling is unavailable."
    exit 1
  fi
fi

EXTRA_ENV_YAML=""
if [ -n "$EXTRA_ENV" ]; then
  IFS=',' read -ra _env_pairs <<< "$EXTRA_ENV"
  for _pair in "${_env_pairs[@]}"; do
    [ -z "$_pair" ] && continue
    _key="${_pair%%=*}"
    _val="${_pair#*=}"
    _key=$(echo "$_key" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
    [ -z "$_key" ] && continue
    _val=$(yaml_escape_value "$_val")
    EXTRA_ENV_YAML+="        - name: ${_key}"$'\n'
    EXTRA_ENV_YAML+="          value: \"${_val}\""$'\n'
  done
  EXTRA_ENV_YAML="${EXTRA_ENV_YAML%$'\n'}"
fi

# --- Inherit proxy variables from the current environment and inject them into worker pods ---
# ARC Runner receives proxy variables through the hook-extension-cpu ConfigMap.
# Forward nonempty proxy variables to worker pods to avoid WAF 403 responses from direct external connections.
# Place them before EXTRA_ENV_YAML so users can override proxy settings through extra_env.
PROXY_ENV_YAML=""
for _proxy_var in http_proxy https_proxy HTTP_PROXY HTTPS_PROXY no_proxy NO_PROXY; do
  _proxy_val="${!_proxy_var:-}"
  if [ -n "$_proxy_val" ]; then
    _proxy_val=$(yaml_escape_value "$_proxy_val")
    PROXY_ENV_YAML+="        - name: ${_proxy_var}"$'\n'
    PROXY_ENV_YAML+="          value: \"${_proxy_val}\""$'\n'
  fi
done
PROXY_ENV_YAML="${PROXY_ENV_YAML%$'\n'}"
if [ -n "$PROXY_ENV_YAML" ]; then
  log_info "Proxy environment variables will be injected into worker pods"
fi

# === Automatically forward user workflow/job env variables ===
USER_ENV_YAML=""
if [[ -n "${USER_ENV_JSON:-}" && "${USER_ENV_JSON}" != "{}" && "${USER_ENV_JSON}" != "null" ]]; then
  while IFS= read -r line; do
    _key="${line%%=*}"
    _b64val="${line#*=}"
    [[ -z "${_key}" ]] && continue
    # Skip action-internal variables to avoid duplicate injection.
    case "$_key" in
      INPUT_*|CONTAINER_OPTIONS|SOURCE_STAGE_DIR|USER_ENV_JSON|GITHUB_*) continue ;;
    esac
    _val=$(printf '%s' "${_b64val}" | base64 -d && printf '.')
    _val="${_val%.}"
    _val=$(yaml_escape_value "$_val")
    USER_ENV_YAML+="        - name: \"${_key}\""$'\n'
    USER_ENV_YAML+="          value: \"${_val}\""$'\n'
  done < <(echo "${USER_ENV_JSON}" | jq -r 'to_entries[] | "\(.key)=\(.value | @base64)"')
  USER_ENV_YAML="${USER_ENV_YAML%$'\n'}"
  if [[ -n "${USER_ENV_YAML}" ]]; then
    log_info "User env variables will be injected into worker pods: $(echo "${USER_ENV_JSON}" | jq -r 'keys | join(", ")')"
  fi
fi

NODE_SELECTOR_YAML=""
BOARD_TYPE=""
if [ -n "$NODE_SELECTOR" ]; then
  _ns_items=""
  IFS=',' read -ra _ns_pairs <<< "$NODE_SELECTOR"
  for _pair in "${_ns_pairs[@]}"; do
    [ -z "$_pair" ] && continue
    _key="${_pair%%=*}"
    _val="${_pair#*=}"
    _key=$(echo "$_key" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
    [ -z "$_key" ] && continue

    _key_name="$_key"
    if [[ "$_key" == */* ]]; then
      _key_prefix="${_key%/*}"
      _key_name="${_key##*/}"
      if [ ${#_key_prefix} -gt 253 ] || [ -z "$_key_prefix" ] \
         || ! [[ "$_key_prefix" =~ ^[a-zA-Z0-9]([a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?$ ]]; then
        log_error "node_selector: invalid key prefix: '${_key_prefix}'"
        exit 1
      fi
    fi
    if [ -z "$_key_name" ] || [ ${#_key_name} -gt 63 ] \
       || ! [[ "$_key_name" =~ ^[a-zA-Z0-9]([a-zA-Z0-9._-]{0,61}[a-zA-Z0-9])?$ ]]; then
      log_error "node_selector: invalid key name: '${_key_name}'"
      exit 1
    fi

    if [ -n "$_val" ]; then
      if [ ${#_val} -gt 63 ] \
         || ! [[ "$_val" =~ ^[a-zA-Z0-9]([a-zA-Z0-9._-]{0,61}[a-zA-Z0-9])?$ ]]; then
        log_error "node_selector: invalid value: '${_val}'"
        exit 1
      fi
    fi

    if [ "$_key" = "board-type" ]; then
      BOARD_TYPE="$_val"
      [ "$_val" = "ZW-810E" ] && _val="OAM-810E"
    fi
    _ns_items+="    ${_key}: \"${_val}\""$'\n'
  done
  _ns_items="${_ns_items%$'\n'}"
  if [ -n "$_ns_items" ]; then
    NODE_SELECTOR_YAML="  nodeSelector:"$'\n'"${_ns_items}"
  fi
fi

# --- Parse container runtime options ---
# CONTAINER_OPTIONS is a user-supplied YAML fragment (optionally multiline). Supported keys:
#   privileged:   true/false             → container securityContext.privileged
#   host_ipc:     true/false             → pod spec.hostIPC
#   host_network: true/false             → pod spec.hostNetwork
#   dns_policy:   "ClusterFirstWithHostNet" → pod spec.dnsPolicy
#   shm_size:     "8Gi"                  → /dev/shm emptyDir sizeLimit (default 64Gi)
#   cap_add:      "SYS_PTRACE,IPC_LOCK"  → securityContext.capabilities.add (comma-separated)
#   cpus:         "8" / "1.5" / "500m"   → resources.limits.cpu (alias cpu)
#   memory:       "64Gi" / "64g"         → resources.limits.memory (Docker lowercase units converted using base 1024)
# When cpus/memory are empty, calculate from the card type and NPROC; other options retain their defaults.
PRIVILEGED="false"
HOST_IPC="false"
HOST_NETWORK="false"
DNS_POLICY=""
SHM_SIZE=""
CAP_ADD=""
CPUS=""
MEMORY=""

# Extract the value for a case-insensitive key; strip quotes and whitespace from the value.
# With pipefail, grep without matches makes the pipeline nonzero; || true handles this, and callers apply defaults to empty values.
_co_opt_value() {
  printf '%s\n' "$CONTAINER_OPTIONS" | grep -i "^[[:space:]]*${1}:" | head -1 | awk '{print $2}' | tr -d "\"' " || true
}

if [ -n "$CONTAINER_OPTIONS" ]; then
  PRIVILEGED=$(_co_opt_value 'privileged'); PRIVILEGED="${PRIVILEGED:-false}"
  HOST_IPC=$(_co_opt_value 'host_ipc');     HOST_IPC="${HOST_IPC:-false}"
  HOST_NETWORK=$(_co_opt_value 'host_network'); HOST_NETWORK="${HOST_NETWORK:-false}"
  DNS_POLICY=$(_co_opt_value 'dns_policy')
  SHM_SIZE=$(_co_opt_value 'shm_size')
  CAP_ADD=$(_co_opt_value 'cap_add')
  CPUS=$(_co_opt_value 'cpus'); [ -z "$CPUS" ] && CPUS=$(_co_opt_value 'cpu')
  MEMORY=$(_co_opt_value 'memory')

  # Normalize booleans (True/TRUE → true) and validate.
  PRIVILEGED=$(printf '%s' "$PRIVILEGED" | tr '[:upper:]' '[:lower:]')
  HOST_IPC=$(printf '%s' "$HOST_IPC" | tr '[:upper:]' '[:lower:]')
  HOST_NETWORK=$(printf '%s' "$HOST_NETWORK" | tr '[:upper:]' '[:lower:]')
  if [ "$PRIVILEGED" != "true" ] && [ "$PRIVILEGED" != "false" ]; then
    log_error "container_options: privileged must be true/false; got '${PRIVILEGED}'"
    exit 1
  fi
  if [ "$HOST_IPC" != "true" ] && [ "$HOST_IPC" != "false" ]; then
    log_error "container_options: host_ipc must be true/false; got '${HOST_IPC}'"
    exit 1
  fi
  if [ "$HOST_NETWORK" != "true" ] && [ "$HOST_NETWORK" != "false" ]; then
    log_error "container_options: host_network must be true/false; got '${HOST_NETWORK}'"
    exit 1
  fi

  # Validate dns_policy and normalize to K8s camel-case values. None also requires dnsConfig,
  # which this action does not generate, so reject it explicitly instead of producing a pod spec the apiserver would reject.
  if [ -n "$DNS_POLICY" ]; then
    case "$(printf '%s' "$DNS_POLICY" | tr '[:upper:]' '[:lower:]')" in
      clusterfirst)            DNS_POLICY="ClusterFirst" ;;
      clusterfirstwithhostnet) DNS_POLICY="ClusterFirstWithHostNet" ;;
      default)                 DNS_POLICY="Default" ;;
      *)
        log_error "container_options: dns_policy must be ClusterFirst/ClusterFirstWithHostNet/Default; got '${DNS_POLICY}'"
        exit 1
        ;;
    esac
  fi

  # Validate shm_size as a K8s quantity (e.g. 8Gi / 512Mi / 1024) and prevent invalid characters from entering YAML.
  if [ -n "$SHM_SIZE" ]; then
    if ! [[ "$SHM_SIZE" =~ ^[0-9]+(\.[0-9]+)?([KMGTPE]i|[kMGTPE])?$ ]]; then
      log_error "container_options: shm_size is not a valid K8s size (e.g. 8Gi, 512Mi); got '${SHM_SIZE}'"
      exit 1
    fi
  fi

  if [ -n "$CPUS" ]; then
    if ! [[ "$CPUS" =~ ^([0-9]+(\.[0-9]+)?|[0-9]+m)$ ]]; then
      log_error "container_options: cpus is not a valid CPU quantity (e.g. 8, 1.5, 500m); got '${CPUS}'"
      exit 1
    fi
  fi

  # Docker b/k/m/g/t units use base 1024, whereas lowercase m in K8s means milli; convert to bytes/Ki/Mi/Gi/Ti.
  if [ -n "$MEMORY" ]; then
    if [[ "$MEMORY" =~ ^([0-9]+)([bkmgt])$ ]]; then
      case "${BASH_REMATCH[2]}" in
        b) MEMORY="${BASH_REMATCH[1]}" ;;
        k) MEMORY="${BASH_REMATCH[1]}Ki" ;;
        m) MEMORY="${BASH_REMATCH[1]}Mi" ;;
        g) MEMORY="${BASH_REMATCH[1]}Gi" ;;
        t) MEMORY="${BASH_REMATCH[1]}Ti" ;;
      esac
    elif ! [[ "$MEMORY" =~ ^[0-9]+(\.[0-9]+)?([KMGTPE]i?|k)?$ ]]; then
      log_error "container_options: memory is not a valid memory size (e.g. 64Gi, 512Mi, 64g); got '${MEMORY}'"
      exit 1
    fi
  fi
fi

if [ -z "$CPUS" ]; then
  case "$BOARD_TYPE" in
    ZW-M890P)
      CPU_PER_PPU=31
      CPU_MAX=248
      ;;
    OAM-810E|ZW-810E|"")
      CPU_PER_PPU=11
      CPU_MAX=176
      ;;
    *)
      log_error "node_selector: unknown board-type '${BOARD_TYPE}'; cannot determine default CPU; set container_options.cpus explicitly"
      exit 1
      ;;
  esac
  CPUS=$((NPROC * CPU_PER_PPU))
  [ "$CPUS" -gt "$CPU_MAX" ] && CPUS="$CPU_MAX"
  log_info "container_options.cpus is unset; using board-type=${BOARD_TYPE:-<unspecified, assuming OAM-810E>}, ${NPROC} PPU to automatically set ${CPUS}"
fi

if [ -z "$MEMORY" ]; then
  case "$BOARD_TYPE" in
    ZW-M890P)
      MEMORY_PER_PPU_GI=256
      MEMORY_MAX_GI=2048
      ;;
    OAM-810E|ZW-810E|"")
      MEMORY_PER_PPU_GI=112
      MEMORY_MAX_GI=1792
      ;;
    *)
      log_error "node_selector: unknown board-type '${BOARD_TYPE}'; cannot determine default memory; set container_options.memory explicitly"
      exit 1
      ;;
  esac
  MEMORY_GI=$((NPROC * MEMORY_PER_PPU_GI))
  [ "$MEMORY_GI" -gt "$MEMORY_MAX_GI" ] && MEMORY_GI="$MEMORY_MAX_GI"
  MEMORY="${MEMORY_GI}Gi"
  log_info "container_options.memory is unset; using board-type=${BOARD_TYPE:-<unspecified, assuming OAM-810E>}, ${NPROC} PPU to automatically set ${MEMORY}"
fi

log_info "container_options: privileged=${PRIVILEGED} host_ipc=${HOST_IPC} host_network=${HOST_NETWORK} dns_policy=${DNS_POLICY:-<cluster default>} shm_size=${SHM_SIZE:-64Gi(default)} cap_add=${CAP_ADD:-<none>} cpus=${CPUS:-<unlimited>} memory=${MEMORY}"

# Combining hostNetwork with the default ClusterFirst makes pods use the host resolver, so this action's
# injected MASTER_ADDR (the headless Service's *.svc.cluster.local address) cannot resolve,
# causing multi-node rendezvous to fail. The official K8s solution for this combination is
# ClusterFirstWithHostNet: CoreDNS resolves cluster names and forwards other queries upstream.
# Apply this default when dns_policy is unset; always honor explicitly supplied values.
if [ "$HOST_NETWORK" = "true" ] && [ -z "$DNS_POLICY" ]; then
  DNS_POLICY="ClusterFirstWithHostNet"
  log_info "host_network=true; dnsPolicy automatically set to ClusterFirstWithHostNet (override with container_options.dns_policy)"
fi

# /dev/shm size (default 64Gi, preserving historical behavior).
SHM_SIZE_LIMIT="${SHM_SIZE:-64Gi}"

# cap_add → securityContext.capabilities.add list entries (12-space indentation, aligned under add:).
CAP_ADD_ITEMS=""
if [ -n "$CAP_ADD" ]; then
  IFS=',' read -ra _caps <<< "$CAP_ADD"
  for _cap in "${_caps[@]}"; do
    _cap=$(printf '%s' "$_cap" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' | tr '[:lower:]' '[:upper:]')
    [ -z "$_cap" ] && continue
    # K8s capability names start with an uppercase letter and contain only uppercase letters, digits, and underscores.
    if ! [[ "$_cap" =~ ^[A-Z][A-Z0-9_]*$ ]]; then
      log_error "container_options: cap_add contains an invalid capability name '${_cap}' (e.g. SYS_PTRACE,IPC_LOCK)"
      exit 1
    fi
    CAP_ADD_ITEMS+="            - ${_cap}"$'\n'
  done
  CAP_ADD_ITEMS="${CAP_ADD_ITEMS%$'\n'}"
fi

# Set only limits: K8s defaults unspecified requests to the same values, matching Docker --cpus/--memory hard limits.
RESOURCE_LIMITS_YAML=""
[ -n "$CPUS" ] && RESOURCE_LIMITS_YAML+="          cpu: \"${CPUS}\""$'\n'
[ -n "$MEMORY" ] && RESOURCE_LIMITS_YAML+="          memory: \"${MEMORY}\""$'\n'
RESOURCE_LIMITS_YAML="${RESOURCE_LIMITS_YAML%$'\n'}"

# Container-level securityContext fragment (6-space indentation, at the same level as image/env/resources).
SECURITY_CONTEXT_YAML=""
if [ "$PRIVILEGED" = "true" ] || [ -n "$CAP_ADD_ITEMS" ]; then
  _sc="      securityContext:"
  if [ "$PRIVILEGED" = "true" ]; then
    _sc+=$'\n'"        privileged: true"
  fi
  if [ -n "$CAP_ADD_ITEMS" ]; then
    _sc+=$'\n'"        capabilities:"
    _sc+=$'\n'"          add:"
    _sc+=$'\n'"$CAP_ADD_ITEMS"
  fi
  SECURITY_CONTEXT_YAML="$_sc"
fi

# Pod-level options fragment (2-space indentation, at the same level as nodeSelector/containers).
POD_LEVEL_OPTIONS_YAML=""
_append_pod_option() {
  if [ -n "$POD_LEVEL_OPTIONS_YAML" ]; then
    POD_LEVEL_OPTIONS_YAML+=$'\n'"  ${1}"
  else
    POD_LEVEL_OPTIONS_YAML="  ${1}"
  fi
}
if [ "$HOST_IPC" = "true" ]; then
  _append_pod_option "hostIPC: true"
fi
if [ "$HOST_NETWORK" = "true" ]; then
  _append_pod_option "hostNetwork: true"
fi
if [ -n "$DNS_POLICY" ]; then
  _append_pod_option "dnsPolicy: ${DNS_POLICY}"
fi

HOST_VOLUME_MOUNTS=""
HOST_VOLUME_DEFS=""
if [ -n "$HOST_VOLUMES" ]; then
  _vol_idx=0
  IFS=',' read -ra _vol_pairs <<< "$HOST_VOLUMES"
  for _pair in "${_vol_pairs[@]}"; do
    [ -z "$_pair" ] && continue
    _host_path="${_pair%%:*}"
    _container_path="${_pair#*:}"
    if [ -z "$_host_path" ] || [ -z "$_container_path" ]; then
      log_error "Invalid host_volumes format: '${_pair}'; expected /host/path:/container/path"
      exit 1
    fi
    # Filter paths that duplicate default NAS mounts.
    _is_default=false
    for _def_entry in "${DEFAULT_NAS_VOLUMES[@]}"; do
      _def_path="${_def_entry#*|}"; _def_path="${_def_path%%|*}"
      if [ "$_container_path" = "$_def_path" ]; then
        _is_default=true
        break
      fi
    done
    if [ "$_is_default" = true ]; then
      log_warn "host_volumes entry '${_pair}' uses default mount path ${_container_path}; skipped to avoid duplication."
      continue
    fi
    _vol_name="host-vol-${_vol_idx}"
    HOST_VOLUME_MOUNTS+="        - name: ${_vol_name}"$'\n'
    HOST_VOLUME_MOUNTS+="          mountPath: ${_container_path}"$'\n'
    HOST_VOLUME_DEFS+="    - name: ${_vol_name}"$'\n'
    HOST_VOLUME_DEFS+="      hostPath:"$'\n'
    HOST_VOLUME_DEFS+="        path: ${_host_path}"$'\n'
    HOST_VOLUME_DEFS+="        type: DirectoryOrCreate"$'\n'
    _vol_idx=$(( _vol_idx + 1 ))
  done
  HOST_VOLUME_MOUNTS="${HOST_VOLUME_MOUNTS%$'\n'}"
  HOST_VOLUME_DEFS="${HOST_VOLUME_DEFS%$'\n'}"
  log_info "Parsed host_volumes:"
  echo "${HOST_VOLUME_MOUNTS}" >&2
  echo "${HOST_VOLUME_DEFS}" >&2
fi

# --- Inject extraction of the source tarball from NAS ---
# SOURCE_STAGE_DIR is the tarball path as seen by the CPU runner (/wl_nas/devops/xxx/source.tar.gz).
# PPU pods access the same file through the default NAS mount (/mnt/wl_nas), so remap the path prefix.
if [ -n "$SOURCE_STAGE_DIR" ]; then
  # NAS path inside the CPU runner container /wl_nas → NAS path inside the PPU pod container /mnt/wl_nas.
  # Avoid patsub (${var/\/wl_nas\//...}): bash preserves the backslash in \/ within the replacement string.
  if [[ "$SOURCE_STAGE_DIR" == /wl_nas/* ]]; then
    TARBALL_PATH_IN_POD="/mnt/wl_nas/${SOURCE_STAGE_DIR#/wl_nas/}"
  else
    TARBALL_PATH_IN_POD="$SOURCE_STAGE_DIR"
  fi
  printf -v EXTRACT_CMD 'mkdir -p %q && tar xzf %q -C %q && cd %q' \
    "$SOURCE_MOUNT_PATH" "$TARBALL_PATH_IN_POD" "$SOURCE_MOUNT_PATH" "$SOURCE_MOUNT_PATH"
  COMMAND="${EXTRACT_CMD} || exit \$?
${COMMAND}"
  log_info "Source tarball extraction injected: ${TARBALL_PATH_IN_POD} → ${SOURCE_MOUNT_PATH}"
fi

if [ "$DMESG_POLICY" != "never" ]; then
  DMESG_DIR_IN_POD="/mnt/wl_nas/devops/dmesg/${RUN_ID}-${RUN_ATTEMPT}/${JOB_NAME}"
  DMESG_COLLECTOR=$(<"$(dirname "${BASH_SOURCE[0]}")/dmesg-collector.sh")
  COMMAND="${DMESG_COLLECTOR}
PPU_DMESG_DIR='${DMESG_DIR_IN_POD}'
export PPU_DMESG_DIR
_ppu_dmesg_begin
_ppu_worker_pid=''
_ppu_worker_signal() {
  trap - TERM INT
  [ -n \"\${_ppu_worker_pid:-}\" ] && kill -TERM \"\$_ppu_worker_pid\" 2>/dev/null
  wait \"\$_ppu_worker_pid\" 2>/dev/null
  _ppu_dmesg_end \"\$1\"
  exit \"\$1\"
}
trap '_ppu_worker_signal 143' TERM
trap '_ppu_worker_signal 130' INT
(
${COMMAND}
) &
_ppu_worker_pid=\$!
wait \"\$_ppu_worker_pid\"
_ppu_command_rc=\$?
trap - TERM INT
_ppu_dmesg_end \"\$_ppu_command_rc\"
exit \"\$_ppu_command_rc\""
  log_info "dmesg collection policy: ${DMESG_POLICY}; NAS path: /wl_nas/devops/dmesg/${RUN_ID}-${RUN_ATTEMPT}/${JOB_NAME}"
fi

SCRIPT_INDENTED=$(printf '%s\n' "$COMMAND" | sed 's/^/          /')

# --- Remove existing resources with the same name ---
log_info "Removing old resources with the same name, if any: $JOB_NAME"
kube_request delete pods -n "$NAMESPACE" -l "ppu-job=$JOB_NAME" --ignore-not-found=true --wait=false
if [ "$SINGLE_MODE" = false ]; then
  kube_request delete svc "$JOB_NAME" -n "$NAMESPACE" --ignore-not-found=true --wait=false
  kube_request delete podgroups.scheduling.x-k8s.io "$JOB_NAME" -n "$NAMESPACE" --ignore-not-found=true --wait=false
fi
WAIT_SECONDS=$(( DEADLINE - $(date +%s) ))
[ "$WAIT_SECONDS" -gt 3 ] && WAIT_SECONDS=3
[ "$WAIT_SECONDS" -gt 0 ] && sleep "$WAIT_SECONDS"
while true; do
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then
    log_error "Job timeout reached while waiting for previous resources to disappear"
    FINAL_STATUS="timeout"
    exit 0
  fi
  OLD_PODS=$(kube_request get pods -n "$NAMESPACE" -l "ppu-job=$JOB_NAME" --no-headers -o custom-columns=NAME:.metadata.name)
  OLD_SERVICE=""
  OLD_PODGROUP=""
  if [ "$SINGLE_MODE" = false ]; then
    OLD_SERVICE=$(kube_request get svc "$JOB_NAME" -n "$NAMESPACE" --ignore-not-found=true -o name)
    OLD_PODGROUP=$(kube_request get podgroups.scheduling.x-k8s.io "$JOB_NAME" -n "$NAMESPACE" --ignore-not-found=true -o name)
  fi
  [ -z "$OLD_PODS$OLD_SERVICE$OLD_PODGROUP" ] && break
  WAIT_SECONDS=$(( DEADLINE - $(date +%s) ))
  [ "$WAIT_SECONDS" -gt 3 ] && WAIT_SECONDS=3
  [ "$WAIT_SECONDS" -gt 0 ] && sleep "$WAIT_SECONDS"
done

# --- Create Headless Service ---
if [ "$SINGLE_MODE" = false ]; then
  SVC_MANIFEST="$MANIFEST_DIR/service.yaml"
  cat > "$SVC_MANIFEST" <<SVC_EOF
apiVersion: v1
kind: Service
metadata:
  name: ${JOB_NAME}
  namespace: ${NAMESPACE}
spec:
  clusterIP: None
  selector:
    ppu-job: ${JOB_NAME}
  ports:
    - name: master
      port: ${MASTER_PORT}
      targetPort: ${MASTER_PORT}
SVC_EOF

  log_info "Creating Headless Service: $JOB_NAME"
  kube_request create -f "$SVC_MANIFEST"

  # --- Create PodGroup ---
  PG_MANIFEST="$MANIFEST_DIR/podgroup.yaml"
  cat > "$PG_MANIFEST" <<PG_EOF
apiVersion: scheduling.x-k8s.io/v1alpha1
kind: PodGroup
metadata:
  name: ${JOB_NAME}
  namespace: ${NAMESPACE}
spec:
  minMember: ${NNODES}
PG_EOF

  log_info "Creating PodGroup: $JOB_NAME (minMember=$NNODES)"
  kube_request create -f "$PG_MANIFEST"
else
  log_info "Single-card/multi-card mode: skipping Service/PodGroup creation"
fi

# --- Build Pod labels (include the pod-group label only in gang mode) ---
POD_LABELS="    ppu-job: ${JOB_NAME}"
if [ "$SINGLE_MODE" = false ]; then
  POD_LABELS="${POD_LABELS}"$'\n'"    scheduling.x-k8s.io/pod-group: ${JOB_NAME}"
fi
POD_LABELS="${POD_LABELS}"$'\n'"    app.kubernetes.io/managed-by: cluster-run"

POD_AFFINITY_YAML=""
if [ "$NNODES" -gt 1 ]; then
  POD_AFFINITY_YAML=$(cat <<AFFINITY_EOF
  affinity:
    podAntiAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        - labelSelector:
            matchLabels:
              ppu-job: "${JOB_NAME}"
          topologyKey: kubernetes.io/hostname
AFFINITY_EOF
)
fi

# Generate NAS volumeMounts and volumes in the pod spec dynamically from DEFAULT_NAS_VOLUMES.
DEFAULT_NAS_VOLUME_MOUNTS=""
DEFAULT_NAS_VOLUME_DEFS=""
for _def_entry in "${DEFAULT_NAS_VOLUMES[@]}"; do
  IFS='|' read -r _vname _mpath _hpath <<< "$_def_entry"
  DEFAULT_NAS_VOLUME_MOUNTS+="        - name: ${_vname}"$'\n'
  DEFAULT_NAS_VOLUME_MOUNTS+="          mountPath: ${_mpath}"$'\n'
  DEFAULT_NAS_VOLUME_DEFS+="    - name: ${_vname}"$'\n'
  DEFAULT_NAS_VOLUME_DEFS+="      hostPath:"$'\n'
  DEFAULT_NAS_VOLUME_DEFS+="        path: ${_hpath}"$'\n'
  DEFAULT_NAS_VOLUME_DEFS+="        type: DirectoryOrCreate"$'\n'
done
DEFAULT_NAS_VOLUME_MOUNTS="${DEFAULT_NAS_VOLUME_MOUNTS%$'\n'}"
DEFAULT_NAS_VOLUME_DEFS="${DEFAULT_NAS_VOLUME_DEFS%$'\n'}"

MASTER_ADDR="${JOB_NAME}-worker-0.${JOB_NAME}.${NAMESPACE}.svc.cluster.local"
[ "$SINGLE_MODE" = true ] && MASTER_ADDR="127.0.0.1"

# --- Create N Pods in a loop ---
for i in $(seq 0 $(( NNODES - 1 ))); do
  POD_MANIFEST="$MANIFEST_DIR/pod-worker-${i}.yaml"
  cat > "$POD_MANIFEST" <<POD_EOF
apiVersion: v1
kind: Pod
metadata:
  name: ${JOB_NAME}-worker-${i}
  namespace: ${NAMESPACE}
  labels:
${POD_LABELS}
    worker-index: "${i}"
spec:
  schedulerName: ppu-scheduler
  hostname: ${JOB_NAME}-worker-${i}
  subdomain: ${JOB_NAME}
  restartPolicy: Never
${POD_AFFINITY_YAML}
${NODE_SELECTOR_YAML}
${POD_LEVEL_OPTIONS_YAML}
  dnsConfig:
    options:
    - name: ndots
      value: "2"
    - name: single-request-reopen
    - name: attempts
      value: "3"
    - name: timeout
      value: "2"
  containers:
    - name: worker
      image: ${IMAGE}
      imagePullPolicy: IfNotPresent
      command: ["/bin/bash", "-c"]
      args:
        - |
${SCRIPT_INDENTED}
      env:
        - name: NNODES
          value: "${NNODES}"
        - name: NPROC_PER_NODE
          value: "${NPROC}"
        - name: WORLD_SIZE
          value: "${NNODES}"
        - name: MASTER_PORT
          value: "${MASTER_PORT}"
        - name: LOCAL_RANK
          value: "0"
        - name: NODE_RANK
          value: "${i}"
        - name: RANK
          value: "${i}"
        - name: MASTER_ADDR
          value: "${MASTER_ADDR}"
        - name: NODE_NAME
          valueFrom:
            fieldRef:
              fieldPath: spec.nodeName
${PROXY_ENV_YAML}
${USER_ENV_YAML}
${EXTRA_ENV_YAML}
      resources:
        requests:
          alibabacloud.com/ppu: "${NPROC}"
        limits:
          alibabacloud.com/ppu: "${NPROC}"
${RESOURCE_LIMITS_YAML}
${SECURITY_CONTEXT_YAML}
      volumeMounts:
        - name: dshm
          mountPath: /dev/shm
${DEFAULT_NAS_VOLUME_MOUNTS}
${HOST_VOLUME_MOUNTS}
  volumes:
    - name: dshm
      emptyDir:
        medium: Memory
        sizeLimit: ${SHM_SIZE_LIMIT}
${DEFAULT_NAS_VOLUME_DEFS}
${HOST_VOLUME_DEFS}
POD_EOF

  log_info "Creating Pod: ${JOB_NAME}-worker-${i}"
  kube_request create -f "$POD_MANIFEST"
done

log_info "All ${NNODES} worker Pods submitted"

# --- Write the task record to NAS ---
{
  TASK_DIR="/wl_nas/devops/ppu-dashboard/tasks"
  mkdir -p "$TASK_DIR" 2>/dev/null || true
  TASK_FILE="${TASK_DIR}/${JOB_NAME}.json"
  cat > "$TASK_FILE" <<TASK_EOF
{
  "run_id": "${RUN_ID}",
  "run_attempt": "${RUN_ATTEMPT}",
  "repo": "${GITHUB_REPOSITORY}",
  "job_name": "${JOB_NAME}",
  "github_job": "${GITHUB_JOB_NAME}",
  "pod_name": "${JOB_NAME}-worker-0",
  "namespace": "${NAMESPACE}",
  "ppu_per_node": ${NPROC},
  "nnodes": ${NNODES},
  "ppu_total": $(( NNODES * NPROC )),
  "start_time": "$(date +%Y-%m-%dT%H:%M:%S%z)",
  "status": "pending"
}
TASK_EOF
  log_info "Task record written: ${TASK_FILE}"
} 2>/dev/null || true

log_info "Waiting for PPU scheduling (shared job timeout: ${TIMEOUT}s)..."

while true; do
  NOW=$(date +%s)
  ELAPSED=$(( NOW - START_TIME ))
  if [ "$NOW" -ge "$DEADLINE" ]; then
    log_error "PPU scheduling timed out after ${TIMEOUT}s"
    FINAL_STATUS="timeout"
    exit 0
  fi

  POD_SNAPSHOT=$(kube_request get pods -n "$NAMESPACE" -l "ppu-job=$JOB_NAME" -o json)
  if [ -f "${TASK_FILE:-}" ] && printf '%s' "$POD_SNAPSHOT" | jq -e 'any(.items[]; .status.phase == "Running" or .status.phase == "Succeeded")' >/dev/null; then
    TASK_RECORDED_STATUS=$(jq -r '.status // empty' "$TASK_FILE")
    if [ "$TASK_RECORDED_STATUS" != "running" ]; then
      jq '.status = "running"' "$TASK_FILE" > "${TASK_FILE}.tmp" && mv "${TASK_FILE}.tmp" "$TASK_FILE"
    fi
  fi
  SCHEDULED=$(printf '%s' "$POD_SNAPSHOT" | jq '[.items[] | select(any(.status.conditions[]?; .type == "PodScheduled" and .status == "True"))] | length')
  if [ "$SCHEDULED" -eq "$NNODES" ]; then
    printf '%s' "$POD_SNAPSHOT" | jq -r '.items[] | "Pod \(.metadata.name) -> Node: \(.spec.nodeName)"'
    FINAL_STATUS="scheduled"
    set_output "job_status" "$FINAL_STATUS"
    log_info "All ${NNODES} worker pods are scheduled"
    exit 0
  fi

  FAILED=$(printf '%s' "$POD_SNAPSHOT" | jq '[.items[] | select(.status.phase == "Failed")] | length')
  if [ "$FAILED" -gt 0 ]; then
    log_error "A worker pod failed before all pods were scheduled"
    exit 0
  fi

  log_info "${ELAPSED}s/${TIMEOUT}s - queued=$(( NNODES - SCHEDULED )) scheduled=${SCHEDULED}/${NNODES}"
  printf '%s' "$POD_SNAPSHOT" | jq -r '
    .items[] | select(any(.status.conditions[]?; .type == "PodScheduled" and .status == "True") | not) |
    "  \(.metadata.name): \([.status.conditions[]? | select(.type == "PodScheduled") | .message] | first // "waiting for scheduling")"'
  WAIT_SECONDS=$(( DEADLINE - $(date +%s) ))
  [ "$WAIT_SECONDS" -gt "$POLL_INTERVAL" ] && WAIT_SECONDS=$POLL_INTERVAL
  if [ "$WAIT_SECONDS" -gt 0 ]; then
    sleep "$WAIT_SECONDS" &
    WAIT_PID=$!
    wait "$WAIT_PID"
    WAIT_PID=""
  fi
done
