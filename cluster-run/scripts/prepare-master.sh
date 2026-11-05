#!/usr/bin/env bash

prepare_master_secret() {
  local scripts_dir nonce i host manifest
  scripts_dir="$(dirname "${BASH_SOURCE[0]}")"
  command -v ssh-keygen >/dev/null || { log_error "Master mode requires ssh-keygen on the orchestration runner"; return 1; }
  MASTER_KEYS_DIR=$(mktemp -d /dev/shm/cluster-keys.XXXXXX)
  chmod 700 "$MASTER_KEYS_DIR"
  nonce=$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')
  MASTER_JOB_ID="${JOB_NAME}-${RUN_ID}-${RUN_ATTEMPT}-${nonce}"
  set_output master_runtime_dir "/wl_nas/devops/runtime/master/${MASTER_JOB_ID}"
  printf '%s\n' "$MASTER_JOB_ID" > "$MASTER_KEYS_DIR/job-id"
  printf '[]\n' > "$MASTER_KEYS_DIR/nodes.json"
  ssh-keygen -q -t ed25519 -N '' -f "$MASTER_KEYS_DIR/identity"
  for ((i=0; i<NNODES; i++)); do
    [ "$(date +%s)" -lt "$DEADLINE" ] || return 124
    ssh-keygen -q -t ed25519 -N '' -f "$MASTER_KEYS_DIR/host-${i}"
    host="${JOB_NAME}-worker-${i}.${JOB_NAME}.${NAMESPACE}.svc.cluster.local"
    jq --arg host "$host" --argjson rank "$i" --argjson cards "$NPROC" \
      '. + [{rank: $rank, host: $host, cards: $cards}]' \
      "$MASTER_KEYS_DIR/nodes.json" > "$MASTER_KEYS_DIR/nodes.tmp"
    mv "$MASTER_KEYS_DIR/nodes.tmp" "$MASTER_KEYS_DIR/nodes.json"
  done
  manifest="$MASTER_KEYS_DIR/secret.json"
  jq -n --arg name "${JOB_NAME}-ssh" --arg job "$JOB_NAME" --arg ns "$NAMESPACE" \
    --rawfile identity "$MASTER_KEYS_DIR/identity" \
    --rawfile public "$MASTER_KEYS_DIR/identity.pub" \
    --rawfile nodes "$MASTER_KEYS_DIR/nodes.json" \
    --rawfile id "$MASTER_KEYS_DIR/job-id" \
    '{apiVersion: "v1", kind: "Secret", type: "Opaque", immutable: true,
      metadata: {name: $name, namespace: $ns, labels: {"ppu-job": $job}},
      data: {identity: ($identity | @base64), "identity.pub": ($public | @base64),
        "nodes.json": ($nodes | @base64), "job-id": ($id | @base64)}}' > "$manifest"
  for ((i=0; i<NNODES; i++)); do
    jq --arg key "host-${i}" --arg pub "host-${i}.pub" \
      --rawfile private "$MASTER_KEYS_DIR/host-${i}" \
      --rawfile public "$MASTER_KEYS_DIR/host-${i}.pub" \
      '.data[$key] = ($private | @base64) | .data[$pub] = ($public | @base64)' \
      "$manifest" > "$MASTER_KEYS_DIR/secret.tmp"
    mv "$MASTER_KEYS_DIR/secret.tmp" "$manifest"
  done
  local script
  for script in install-ssh.sh install-ssh-rpm.sh master-worker.sh master-session.sh cluster-ssh.sh cluster-exec.sh; do
    jq --arg key "$script" --rawfile body "$scripts_dir/$script" \
      '.data[$key] = ($body | @base64)' "$manifest" > "$MASTER_KEYS_DIR/secret.tmp"
    mv "$MASTER_KEYS_DIR/secret.tmp" "$manifest"
  done
  kube_request create -f "$manifest"
  rm -rf -- "$MASTER_KEYS_DIR"
  MASTER_KEYS_DIR=""
}

master_secret_items() {
  local rank=$1 i key
  for key in identity identity.pub nodes.json job-id install-ssh.sh install-ssh-rpm.sh master-worker.sh master-session.sh cluster-ssh.sh cluster-exec.sh "host-${rank}"; do
    printf '            - key: %s\n              path: %s\n' "$key" "$key"
  done
  for ((i=0; i<NNODES; i++)); do
    printf '            - key: host-%s.pub\n              path: host-%s.pub\n' "$i" "$i"
  done
}
