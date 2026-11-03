#!/bin/bash
# Also sourced, without side effects, by the fixed SSH entry points.

cluster_error() { printf 'master runtime: %s\n' "$*" >&2; }
cluster_number() { [[ $1 =~ ^[0-9]+$ && ${#1} -le 10 ]]; }
cluster_remaining() {
    local CLUSTER_NOW
    CLUSTER_NOW=$(/bin/date +%s) || return 1
    cluster_number "${PPU_DEADLINE:-}" || return 1
    CLUSTER_REMAINING=$((10#$PPU_DEADLINE - CLUSTER_NOW))
    (( CLUSTER_REMAINING > 0 ))
}
cluster_regular() {
    local CLUSTER_FILE=$1 CLUSTER_LIMIT=$2 CLUSTER_SIZE
    [[ -f $CLUSTER_FILE && ! -L $CLUSTER_FILE ]] || return 1
    CLUSTER_SIZE=$(/usr/bin/wc -c < "$CLUSTER_FILE") || return 1
    (( CLUSTER_SIZE > 0 && CLUSTER_SIZE <= CLUSTER_LIMIT ))
}
cluster_safe_dir() {
    local CLUSTER_PATH=$1 CLUSTER_CREATE=${2:-1} CLUSTER_PART CLUSTER_CURRENT= CLUSTER_OLDIFS=$IFS
    local -a CLUSTER_PARTS
    [[ $CLUSTER_PATH == /* ]] || return 1
    IFS=/ read -r -a CLUSTER_PARTS <<< "$CLUSTER_PATH"
    IFS=$CLUSTER_OLDIFS
    for CLUSTER_PART in "${CLUSTER_PARTS[@]}"; do
        [[ -n $CLUSTER_PART ]] || continue
        [[ $CLUSTER_PART != . && $CLUSTER_PART != .. ]] || return 1
        CLUSTER_CURRENT=$CLUSTER_CURRENT/$CLUSTER_PART
        [[ ! -L $CLUSTER_CURRENT ]] || return 1
        if [[ ! -d $CLUSTER_CURRENT ]]; then
            [[ $CLUSTER_CREATE == 1 ]] || return 1
            /bin/mkdir "$CLUSTER_CURRENT" 2>/dev/null || [[ -d $CLUSTER_CURRENT && ! -L $CLUSTER_CURRENT ]] || return 1
        fi
        [[ -d $CLUSTER_CURRENT && ! -L $CLUSTER_CURRENT ]] || return 1
    done
}
cluster_registry_owner() {
    local CLUSTER_ROOT=/mnt/wl_nas/devops/runtime/master CLUSTER_LISTING CLUSTER_MODE CLUSTER_LINKS CLUSTER_UID CLUSTER_GID CLUSTER_REST
    [[ $CLUSTER_JOB_ID =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ &&
       $CLUSTER_REGISTRY == "$CLUSTER_ROOT/$CLUSTER_JOB_ID/nodes" ]] || {
        cluster_error 'invalid registry path'; return 1;
    }
    # Infra owns this fixed root. Never create it or infer ownership from input.
    cluster_safe_dir "$CLUSTER_ROOT" 0 || {
        cluster_error "registry root must be pre-provisioned without symlinks: $CLUSTER_ROOT"; return 1;
    }
    CLUSTER_LISTING=$(LC_ALL=C /bin/ls -dn "$CLUSTER_ROOT") || {
        cluster_error "cannot read registry root ownership: $CLUSTER_ROOT"; return 1;
    }
    IFS=' ' read -r CLUSTER_MODE CLUSTER_LINKS CLUSTER_UID CLUSTER_GID CLUSTER_REST <<< "$CLUSTER_LISTING"
    cluster_number "$CLUSTER_UID" && cluster_number "$CLUSTER_GID" || {
        cluster_error "cannot read numeric registry root ownership: $CLUSTER_ROOT"; return 1;
    }
    CLUSTER_REGISTRY_OWNER=$CLUSTER_UID:$CLUSTER_GID
}
cluster_registry_chown() {
    local CLUSTER_CHOWN=/bin/chown
    [[ -x $CLUSTER_CHOWN ]] || CLUSTER_CHOWN=/usr/sbin/chown
    "$CLUSTER_CHOWN" "$CLUSTER_REGISTRY_OWNER" "$@" || {
        cluster_error "cannot assign registry ownership $CLUSTER_REGISTRY_OWNER (runner cleanup requires chown; check root squash)";
        return 1;
    }
}
cluster_registry_initialize() {
    cluster_registry_owner || return 1
    cluster_safe_dir "$CLUSTER_REGISTRY" || {
        cluster_error 'cannot create registry directories without symlinks'; return 1;
    }
    # Only this job's control metadata is handed to the runner, never ancestors,
    # other jobs, the SSH cache, private keys or local environment snapshots.
    cluster_registry_chown "${CLUSTER_REGISTRY%/nodes}" "$CLUSTER_REGISTRY"
}
cluster_validate_nodes() {
    cluster_regular "$1" 1048576 || return 1
    /usr/bin/jq -e --argjson count "$NNODES" '
      type == "array" and length == $count and
      ([.[].host] | unique | length) == $count and
      all(to_entries[]; .value as $n |
        ($n | type) == "object" and $n.rank == .key and
        ($n.host | type) == "string" and ($n.host | length) <= 253 and
        ($n.host | test("^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")) and
        ($n.cards | type) == "number" and $n.cards > 0 and
        $n.cards == ($n.cards | floor))' "$1" >/dev/null
}
cluster_registry_read() {
    local CLUSTER_READ_RANK=$1 CLUSTER_READ_FILE=$2 CLUSTER_READ_HOST=$3
    cluster_regular "$CLUSTER_READ_FILE" 4096 || return 1
    /usr/bin/jq -e --arg job "$CLUSTER_JOB_ID" --arg host "$CLUSTER_READ_HOST" --argjson rank "$CLUSTER_READ_RANK" '
      type == "object" and .jobid == $job and .rank == $rank and .host == $host and
      (.port | type) == "number" and .port >= 1 and .port <= 65535 and
      .port == (.port | floor) and (.pod_uid | type) == "string" and
      (.pod_uid | test("^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")) and
      (.phase as $phase | ["online", "connected", "setup", "ready", "running", "done", "finished", "failed"] | index($phase) != null)
    ' "$CLUSTER_READ_FILE" >/dev/null || return 1
    CLUSTER_RECORD=$(/usr/bin/jq -c . "$CLUSTER_READ_FILE")
}
cluster_port_candidates() {
    local CLUSTER_PREFERRED=$1 CLUSTER_MASTER=$2 CLUSTER_LOW=$3 CLUSTER_HIGH=$4 CLUSTER_SEED=$5 CLUSTER_OFFSET CLUSTER_CANDIDATE CLUSTER_EMITTED=0
    if (( CLUSTER_PREFERRED != CLUSTER_MASTER && (CLUSTER_PREFERRED < CLUSTER_LOW || CLUSTER_PREFERRED > CLUSTER_HIGH) )); then
        printf '%s\n' "$CLUSTER_PREFERRED"
        CLUSTER_EMITTED=1
    fi
    for ((CLUSTER_OFFSET=0; CLUSTER_OFFSET<5000 && CLUSTER_EMITTED<128; CLUSTER_OFFSET++)); do
        CLUSTER_CANDIDATE=$((20000 + (CLUSTER_SEED + CLUSTER_OFFSET) % 5000))
        (( CLUSTER_CANDIDATE != CLUSTER_MASTER && CLUSTER_CANDIDATE != CLUSTER_PREFERRED && (CLUSTER_CANDIDATE < CLUSTER_LOW || CLUSTER_CANDIDATE > CLUSTER_HIGH) )) || continue
        printf '%s\n' "$CLUSTER_CANDIDATE"
        CLUSTER_EMITTED=$((CLUSTER_EMITTED + 1))
    done
}
cluster_env_allowed() {
    [[ $1 =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || return 1
    case $1 in
        GITHUB_*|ACTIONS_*|RUNNER_*|SSH_*|BASH_*|BASHOPTS|SHELLOPTS|ENV|IFS|CDPATH|GLOBIGNORE|PS4|PROMPT_COMMAND|CLUSTER_*) return 1 ;;
    esac
}
cluster_metadata_name() {
    case $1 in
        NNODES|NODE_RANK|RANK|WORLD_SIZE|NPROC_PER_NODE|MASTER_ADDR|MASTER_PORT|SOURCE_DIR|POD_IP|HOST_IP|POD_UID|PPU_SSH_PORT|PPU_DEADLINE|CLUSTER_HOSTS_FILE|CLUSTER_NODES_FILE) return 0 ;;
    esac
    return 1
}
cluster_env_load_file() {
    local CLUSTER_LOAD_ENTRY CLUSTER_LOAD_NAME CLUSTER_LOAD_VALUE
    [[ -f $1 && ! -L $1 ]] || return 1
    while IFS= read -r -d '' CLUSTER_LOAD_ENTRY; do
        CLUSTER_LOAD_NAME=${CLUSTER_LOAD_ENTRY%%=*}
        [[ $CLUSTER_LOAD_ENTRY == *=* ]] || return 1
        CLUSTER_LOAD_VALUE=${CLUSTER_LOAD_ENTRY#*=}
        if cluster_env_allowed "$CLUSTER_LOAD_NAME" || cluster_metadata_name "$CLUSTER_LOAD_NAME"; then
            # Assignment is data, never parsed as shell source.
            export "$CLUSTER_LOAD_NAME=$CLUSTER_LOAD_VALUE"
        fi
    done < "$1"
}
cluster_env_clean() {
    # These two reserved scratch variables deliberately are not local: Bash
    # would restore a shadowed global's old export attribute on function return.
    export -n CLUSTER_CLEAN_ENTRY CLUSTER_CLEAN_NAME
    while IFS= read -r -d '' CLUSTER_CLEAN_ENTRY; do
        CLUSTER_CLEAN_NAME=${CLUSTER_CLEAN_ENTRY%%=*}
        if ! cluster_env_allowed "$CLUSTER_CLEAN_NAME" && ! cluster_metadata_name "$CLUSTER_CLEAN_NAME"; then
            case $CLUSTER_CLEAN_NAME in
                # Locals may inherit export attributes. Keep active scratch
                # values intact, but never pass reserved variables to children.
                CLUSTER_*) export -n "$CLUSTER_CLEAN_NAME" 2>/dev/null || : ;;
                *) unset "$CLUSTER_CLEAN_NAME" 2>/dev/null || : ;;
            esac
        fi
    done < <(/usr/bin/env -0)
    unset BASH_ENV ENV CDPATH GLOBIGNORE PROMPT_COMMAND
}
cluster_env_save() {
    local CLUSTER_SAVE_ENTRY CLUSTER_SAVE_NAME CLUSTER_SAVE_TEMP=/run/cluster/environment.tmp.$$
    cluster_env_load_file /run/cluster/metadata.env || return 1
    export PATH="/run/cluster/bin:${PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"
    (
        while IFS= read -r -d '' CLUSTER_SAVE_ENTRY; do
            CLUSTER_SAVE_NAME=${CLUSTER_SAVE_ENTRY%%=*}
            if cluster_env_allowed "$CLUSTER_SAVE_NAME" || cluster_metadata_name "$CLUSTER_SAVE_NAME"; then
                printf '%s\0' "$CLUSTER_SAVE_ENTRY"
            fi
        done < <(/usr/bin/env -0)
    ) > "$CLUSTER_SAVE_TEMP" || return 1
    /bin/chmod 600 "$CLUSTER_SAVE_TEMP" && /bin/mv -f "$CLUSTER_SAVE_TEMP" /run/cluster/environment.env
}
cluster_status() {
    local CLUSTER_STATUS_PHASE=$1 CLUSTER_STATUS_RC=${2:-0} CLUSTER_STATUS_TEMP=/run/cluster/status.tmp.$$
    /usr/bin/jq -cn --arg job "$CLUSTER_JOB_ID" --arg host "$CLUSTER_HOST" \
        --arg uid "$POD_UID" --arg pod_ip "$POD_IP" --arg host_ip "$HOST_IP" \
        --arg phase "$CLUSTER_STATUS_PHASE" --argjson rank "$NODE_RANK" --argjson port "$CLUSTER_PORT" \
        --argjson rc "$CLUSTER_STATUS_RC" \
        '{jobid:$job,rank:$rank,host:$host,port:$port,pod_uid:$uid,phase:$phase,
          pod_ip:$pod_ip,host_ip:$host_ip,exit_code:$rc}' > "$CLUSTER_STATUS_TEMP" || return 1
    /bin/mv -f "$CLUSTER_STATUS_TEMP" /run/cluster/status.json || return 1
    case $CLUSTER_STATUS_PHASE in
        ready|running) /usr/bin/touch /run/cluster/ready ;;
        *) /bin/rm -f /run/cluster/ready ;;
    esac
    [[ ${CLUSTER_REGISTERED:-0} == 1 ]] || return 0
    cluster_registry_owner || return 1
    local CLUSTER_TARGET=$CLUSTER_REGISTRY/$NODE_RANK.json
    [[ ! -L $CLUSTER_TARGET ]] || return 1
    cluster_safe_dir "$CLUSTER_REGISTRY" 0 || return 1
    CLUSTER_STATUS_TEMP=$(/usr/bin/mktemp "$CLUSTER_REGISTRY/.${NODE_RANK}.XXXXXXXX") || return 1
    if /usr/bin/jq '{jobid,rank,host,port,pod_uid,phase}' /run/cluster/status.json > "$CLUSTER_STATUS_TEMP" &&
        cluster_registry_chown "$CLUSTER_STATUS_TEMP" && [[ ! -L $CLUSTER_TARGET ]] &&
        /bin/mv -f "$CLUSTER_STATUS_TEMP" "$CLUSTER_TARGET"; then
        return 0
    fi
    /bin/rm -f "$CLUSTER_STATUS_TEMP"
    return 1
}
cluster_ssh_options() {
    CLUSTER_SSH_ARGS=(-F /dev/null -i /run/cluster/identity
        -o IdentitiesOnly=yes -o IdentityAgent=none -o BatchMode=yes
        -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/run/cluster/known_hosts
        -o GlobalKnownHostsFile=/dev/null -o CheckHostIP=no -o UpdateHostKeys=no
        -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no
        -o PreferredAuthentications=publickey -o ClearAllForwardings=yes
        -o ForwardAgent=no -o ForwardX11=no -o PermitLocalCommand=no
        -o ControlMaster=no -o ControlPath=none -o ProxyCommand=none
        -o ConnectionAttempts=1 -o ConnectTimeout=2 -o ServerAliveInterval=2
        -o ServerAliveCountMax=2 -o LogLevel=ERROR)
}
cluster_probe() {
    local CLUSTER_PROBE_RANK=$1 CLUSTER_PROBE_HOST=$2 CLUSTER_PROBE_PORT=$3 CLUSTER_PROBE_UID=$4 CLUSTER_REPLY CLUSTER_BUDGET
    cluster_remaining || return 124
    CLUSTER_BUDGET=$CLUSTER_REMAINING
    (( CLUSTER_BUDGET <= 5 )) || CLUSTER_BUDGET=5
    cluster_ssh_options
    # A hard deadline bounds authentication, commands and stalled transports.
    CLUSTER_REPLY=$(/usr/bin/timeout -k 1s "${CLUSTER_BUDGET}s" /usr/bin/ssh "${CLUSTER_SSH_ARGS[@]}" \
        -o "HostKeyAlias=cluster-rank-$CLUSTER_PROBE_RANK" -p "$CLUSTER_PROBE_PORT" -- "root@$CLUSTER_PROBE_HOST" \
        __ppu_master_probe_v1__ 2>/dev/null) || return 1
    (( ${#CLUSTER_REPLY} <= 4096 )) || return 1
    /usr/bin/jq -e --arg job "$CLUSTER_JOB_ID" --arg host "$CLUSTER_PROBE_HOST" --arg uid "$CLUSTER_PROBE_UID" \
        --argjson rank "$CLUSTER_PROBE_RANK" --argjson port "$CLUSTER_PROBE_PORT" '
        type == "object" and .jobid == $job and .host == $host and
        .rank == $rank and .port == $port and .pod_uid == $uid and
        (.phase as $phase | ["online", "connected", "setup", "ready", "running", "done", "finished", "failed"] | index($phase) != null) and
        (.exit_code | type) == "number" and .exit_code >= 0 and .exit_code <= 255 and
        .exit_code == (.exit_code | floor) and
        (.pod_ip | type) == "string" and (.host_ip | type) == "string" and
        (.pod_ip | test("^[0-9A-Fa-f:.]+$")) and (.host_ip | test("^[0-9A-Fa-f:.]+$"))
        ' <<< "$CLUSTER_REPLY" >/dev/null || return 1
    CLUSTER_PROBE=$CLUSTER_REPLY
}
cluster_peer() {
    local CLUSTER_PEER_RANK=$1 CLUSTER_PEER_HOST CLUSTER_PEER_PORT CLUSTER_PEER_UID
    CLUSTER_PEER_HOST=$(/usr/bin/jq -r --argjson rank "$CLUSTER_PEER_RANK" '.[$rank].host' /run/cluster/trusted-nodes.json) || return 1
    cluster_safe_dir "$CLUSTER_REGISTRY" || return 1
    cluster_registry_read "$CLUSTER_PEER_RANK" "$CLUSTER_REGISTRY/$CLUSTER_PEER_RANK.json" "$CLUSTER_PEER_HOST" || return 1
    CLUSTER_PEER_PORT=$(/usr/bin/jq -r .port <<< "$CLUSTER_RECORD")
    CLUSTER_PEER_UID=$(/usr/bin/jq -r .pod_uid <<< "$CLUSTER_RECORD")
    cluster_probe "$CLUSTER_PEER_RANK" "$CLUSTER_PEER_HOST" "$CLUSTER_PEER_PORT" "$CLUSTER_PEER_UID"
}
cluster_mark_deadline() {
    if ! { : > /run/cluster/deadline-exceeded; }; then
        cluster_error 'cannot write local deadline marker'
        return 73
    fi
}
cluster_check_deadline() {
    cluster_remaining && return 0
    cluster_mark_deadline || return "$?"
    cluster_error 'deadline exceeded'
    return 124
}
cluster_termination() {
    # Only the executable parent calls this, never a task or an SSH session.
    local CLUSTER_MESSAGE_DEADLINE=0 CLUSTER_MESSAGE_RANK=0
    if cluster_number "${PPU_DEADLINE:-}"; then CLUSTER_MESSAGE_DEADLINE=$((10#$PPU_DEADLINE)); fi
    if cluster_number "${NODE_RANK:-}"; then CLUSTER_MESSAGE_RANK=$((10#$NODE_RANK)); fi
    if ! /usr/bin/jq -cn --arg outcome "$CLUSTER_OUTCOME" \
        --argjson deadline "$CLUSTER_MESSAGE_DEADLINE" --argjson rank "$CLUSTER_MESSAGE_RANK" \
        '{source:"cluster-master-runtime",outcome:$outcome,deadline:$deadline,rank:$rank}' > /dev/termination-log; then
        cluster_error 'cannot write /dev/termination-log'
        return 73
    fi
}
cluster_initialize() {
    /bin/bash /run/cluster-config/install-ssh.sh
    command -v timeout >/dev/null || { cluster_error 'GNU timeout is required'; return 69; }
    umask 077
    # Debian/Ubuntu sshd requires its compiled-in empty privilege-separation dir.
    # Do not change an existing daemon's directory, configuration or permissions.
    if [[ ! -e /run/sshd ]]; then
        ( umask 022; /bin/mkdir /run/sshd ) 2>/dev/null || [[ -d /run/sshd && ! -L /run/sshd ]] || return 69
    fi
    [[ -d /run/sshd && ! -L /run/sshd ]] || return 69
    [[ -d /run/cluster && ! -L /run/cluster ]] || return 65
    /bin/mkdir -p /run/cluster/bin /run/cluster/sessions
    /bin/chmod 700 /run/cluster /run/cluster/bin /run/cluster/sessions
    local CLUSTER_INIT_NAME CLUSTER_INIT_RANK CLUSTER_TYPE CLUSTER_KEY CLUSTER_COMMENT CLUSTER_LOW CLUSTER_HIGH CLUSTER_SEED CLUSTER_INIT_PORT CLUSTER_LISTENER CLUSTER_I CLUSTER_REPLY
    for CLUSTER_INIT_NAME in master-worker.sh master-session.sh cluster-ssh.sh cluster-exec.sh identity identity.pub nodes.json job-id "host-$NODE_RANK"; do
        [[ -f /run/cluster-config/$CLUSTER_INIT_NAME ]] || return 65
        cp /run/cluster-config/"$CLUSTER_INIT_NAME" /run/cluster/"$CLUSTER_INIT_NAME"
    done
    /bin/chmod 600 /run/cluster/identity /run/cluster/"host-$NODE_RANK"
    /bin/chmod 700 /run/cluster/master-session.sh /run/cluster/cluster-ssh.sh /run/cluster/cluster-exec.sh
    ln -sf /run/cluster/cluster-ssh.sh /run/cluster/bin/cluster-ssh
    ln -sf /run/cluster/cluster-ssh.sh /run/cluster/bin/ssh
    ln -sf /run/cluster/cluster-exec.sh /run/cluster/bin/cluster-exec
    cluster_regular /run/cluster/job-id 129 || return 65
    CLUSTER_JOB_ID=$(< /run/cluster/job-id)
    [[ $CLUSTER_JOB_ID =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || return 65
    CLUSTER_REGISTRY=/mnt/wl_nas/devops/runtime/master/$CLUSTER_JOB_ID/nodes
    cluster_registry_initialize || return 65
    cluster_validate_nodes /run/cluster/nodes.json || return 65
    cp /run/cluster/nodes.json /run/cluster/trusted-nodes.json
    CLUSTER_HOST=$(/usr/bin/jq -r --argjson rank "$NODE_RANK" '.[$rank].host' /run/cluster/trusted-nodes.json)
    [[ $(/usr/bin/jq -r --argjson rank "$NODE_RANK" '.[$rank].cards' /run/cluster/trusted-nodes.json) == "$NPROC_PER_NODE" ]] || return 65
    : > /run/cluster/known_hosts
    for ((CLUSTER_INIT_RANK=0; CLUSTER_INIT_RANK<NNODES; CLUSTER_INIT_RANK++)); do
        cp /run/cluster-config/"host-$CLUSTER_INIT_RANK.pub" /run/cluster/"host-$CLUSTER_INIT_RANK.pub"
        cluster_regular /run/cluster/"host-$CLUSTER_INIT_RANK.pub" 16384 || return 65
        ssh-keygen -l -f /run/cluster/"host-$CLUSTER_INIT_RANK.pub" >/dev/null || return 65
        IFS=' ' read -r CLUSTER_TYPE CLUSTER_KEY CLUSTER_COMMENT < /run/cluster/"host-$CLUSTER_INIT_RANK.pub"
        printf 'cluster-rank-%s %s %s\n' "$CLUSTER_INIT_RANK" "$CLUSTER_TYPE" "$CLUSTER_KEY" >> /run/cluster/known_hosts
    done
    ssh-keygen -l -f /run/cluster/identity.pub >/dev/null
    printf 'restrict ' > /run/cluster/authorized_keys
    /bin/cat /run/cluster/identity.pub >> /run/cluster/authorized_keys
    export CLUSTER_HOSTS_FILE=/run/cluster/hosts CLUSTER_NODES_FILE=/run/cluster/nodes.json
    : > /run/cluster/metadata.env
    for CLUSTER_INIT_NAME in NNODES NODE_RANK RANK WORLD_SIZE NPROC_PER_NODE MASTER_ADDR MASTER_PORT SOURCE_DIR POD_IP HOST_IP POD_UID PPU_SSH_PORT PPU_DEADLINE CLUSTER_HOSTS_FILE CLUSTER_NODES_FILE; do
        printf '%s=%s\0' "$CLUSTER_INIT_NAME" "${!CLUSTER_INIT_NAME}" >> /run/cluster/metadata.env
    done
    cluster_env_save
    CLUSTER_LOW=32768 CLUSTER_HIGH=60999
    if [[ -r /proc/sys/net/ipv4/ip_local_port_range ]]; then
        read -r CLUSTER_LOW CLUSTER_HIGH < /proc/sys/net/ipv4/ip_local_port_range
        cluster_number "$CLUSTER_LOW" && cluster_number "$CLUSTER_HIGH" || return 65
    fi
    CLUSTER_SEED=$(( (RANDOM * 32768 + RANDOM) % 5000 ))
    local CLUSTER_BIND=$POD_IP
    [[ $POD_IP == "$HOST_IP" ]] && CLUSTER_BIND=$HOST_IP
    while IFS= read -r CLUSTER_INIT_PORT; do
        cluster_check_deadline
        CLUSTER_PORT=$CLUSTER_INIT_PORT
        cluster_status online
        {
            printf 'Port %s\nListenAddress %s\nHostKey /run/cluster/host-%s\n' "$CLUSTER_INIT_PORT" "$CLUSTER_BIND" "$NODE_RANK"
            printf '%s\n' 'PidFile /run/cluster/sshd.pid' 'AuthorizedKeysFile /run/cluster/authorized_keys' \
                'PermitRootLogin prohibit-password' 'PubkeyAuthentication yes' 'PasswordAuthentication no' \
                'KbdInteractiveAuthentication no' 'ChallengeResponseAuthentication no' 'AuthenticationMethods publickey' 'UsePAM yes' \
                'StrictModes yes' 'AllowUsers root' 'AllowAgentForwarding no' 'AllowTcpForwarding no' \
                'AllowStreamLocalForwarding no' 'X11Forwarding no' 'PermitTunnel no' \
                'PermitUserEnvironment no' 'PermitUserRC no' 'PrintMotd no' 'PrintLastLog no' \
                'MaxAuthTries 3' 'LoginGraceTime 5' 'LogLevel ERROR' \
                'ForceCommand /bin/bash --noprofile --norc /run/cluster/master-session.sh'
        } > /run/cluster/sshd_config
        /usr/sbin/sshd -t -f /run/cluster/sshd_config || return 69
        # Keep the successful listener; never probe a socket and then rebind it.
        set -m
        /usr/sbin/sshd -D -e -f /run/cluster/sshd_config 2>> /run/cluster/sshd.log &
        CLUSTER_LISTENER=$!
        set +m
        printf '%s\n' "$CLUSTER_LISTENER" > /run/cluster/listener.pending
        for ((CLUSTER_I=0; CLUSTER_I<10; CLUSTER_I++)); do
            cluster_check_deadline
            kill -0 "$CLUSTER_LISTENER" 2>/dev/null || break
            # Local dialing uses the IP, while metadata always identifies the trusted FQDN.
            cluster_check_deadline
            cluster_ssh_options
            if CLUSTER_REPLY=$(/usr/bin/timeout -k 1s "${CLUSTER_REMAINING}s" /usr/bin/ssh "${CLUSTER_SSH_ARGS[@]}" \
                -o "HostKeyAlias=cluster-rank-$NODE_RANK" -p "$CLUSTER_INIT_PORT" -- "root@$CLUSTER_BIND" __ppu_master_probe_v1__ 2>/dev/null); then
                if /usr/bin/jq -e --arg job "$CLUSTER_JOB_ID" --arg host "$CLUSTER_HOST" --arg uid "$POD_UID" \
                    --argjson rank "$NODE_RANK" --argjson port "$CLUSTER_INIT_PORT" \
                    '.jobid == $job and .host == $host and .pod_uid == $uid and .rank == $rank and .port == $port' \
                    <<< "$CLUSTER_REPLY" >/dev/null && kill -0 "$CLUSTER_LISTENER" 2>/dev/null; then
                    printf '%s\n' "$CLUSTER_LISTENER" > /run/cluster/listener.pid
                    CLUSTER_REGISTERED=1
                    cluster_status online
                    return 0
                fi
            fi
            /bin/sleep 0.2
        done
        if kill -0 "$CLUSTER_LISTENER" 2>/dev/null; then
            cluster_error 'sshd bound but public-key self-authentication failed (check the PAM account policy)'
            kill -TERM -- "-$CLUSTER_LISTENER" 2>/dev/null || :
            wait "$CLUSTER_LISTENER" 2>/dev/null || :
            return 69
        fi
        wait "$CLUSTER_LISTENER" 2>/dev/null || :
    done < <(cluster_port_candidates "$PPU_SSH_PORT" "$MASTER_PORT" "$CLUSTER_LOW" "$CLUSTER_HIGH" "$CLUSTER_SEED")
    cluster_error 'no usable SSH port within 128 candidates'
    return 69
}
cluster_mesh() {
    local CLUSTER_MESH_RANK CLUSTER_PHASE CLUSTER_TEMP=/run/cluster/nodes.tmp.$$ CLUSTER_NEXT
    printf '[]\n' > "$CLUSTER_TEMP"
    : > /run/cluster/hosts
    for ((CLUSTER_MESH_RANK=0; CLUSTER_MESH_RANK<NNODES; CLUSTER_MESH_RANK++)); do
        until cluster_peer "$CLUSTER_MESH_RANK"; do cluster_check_deadline; /bin/sleep 0.2; done
        CLUSTER_PHASE=$(/usr/bin/jq -r .phase <<< "$CLUSTER_PROBE")
        if [[ $CLUSTER_PHASE == failed || $CLUSTER_PHASE == done ]]; then return 70; fi
        CLUSTER_NEXT=$CLUSTER_TEMP.next
        /usr/bin/jq --argjson peer "$CLUSTER_PROBE" --argjson rank "$CLUSTER_MESH_RANK" \
            --slurpfile trusted /run/cluster/trusted-nodes.json \
            '. + [($trusted[0][$rank] + {port:$peer.port,pod_uid:$peer.pod_uid,pod_ip:$peer.pod_ip,host_ip:$peer.host_ip})]' \
            "$CLUSTER_TEMP" > "$CLUSTER_NEXT"
        /bin/mv -f "$CLUSTER_NEXT" "$CLUSTER_TEMP"
        /usr/bin/jq -r .host <<< "$CLUSTER_PROBE" >> /run/cluster/hosts
    done
    /bin/mv -f "$CLUSTER_TEMP" /run/cluster/nodes.json
}
cluster_mesh_barrier() {
    local CLUSTER_BARRIER_RANK CLUSTER_PHASE CLUSTER_RC
    cluster_status connected
    for ((CLUSTER_BARRIER_RANK=0; CLUSTER_BARRIER_RANK<NNODES; CLUSTER_BARRIER_RANK++)); do
        while :; do
            cluster_check_deadline
            if ! cluster_peer "$CLUSTER_BARRIER_RANK"; then
                cluster_check_deadline || return "$?"
                return 70
            else
                CLUSTER_PHASE=$(/usr/bin/jq -r .phase <<< "$CLUSTER_PROBE")
                case $CLUSTER_PHASE in
                    connected|setup|ready|running) break ;;
                    failed|done|finished)
                        CLUSTER_RC=$(/usr/bin/jq -r .exit_code <<< "$CLUSTER_PROBE")
                        (( CLUSTER_RC != 0 )) || CLUSTER_RC=70
                        return "$CLUSTER_RC" ;;
                esac
            fi
            /bin/sleep 0.2
        done
    done
}
cluster_ready_barrier() {
    local CLUSTER_BARRIER_RANK CLUSTER_PHASE CLUSTER_RC
    for ((CLUSTER_BARRIER_RANK=0; CLUSTER_BARRIER_RANK<NNODES; CLUSTER_BARRIER_RANK++)); do
        while :; do
            cluster_check_deadline
            if ! cluster_peer "$CLUSTER_BARRIER_RANK"; then
                cluster_check_deadline || return "$?"
                return 70
            else
                CLUSTER_PHASE=$(/usr/bin/jq -r .phase <<< "$CLUSTER_PROBE")
                if [[ $CLUSTER_PHASE == failed ]]; then
                    CLUSTER_RC=$(/usr/bin/jq -r .exit_code <<< "$CLUSTER_PROBE")
                    (( CLUSTER_RC != 0 )) || CLUSTER_RC=70
                    return "$CLUSTER_RC"
                fi
                [[ $CLUSTER_PHASE == ready ]] && break
                [[ $CLUSTER_PHASE != done && $CLUSTER_PHASE != finished ]] || return 70
            fi
            /bin/sleep 0.2
        done
    done
}
cluster_finish() {
    local CLUSTER_FINISH_RC=$1
    trap - EXIT
    set +e
    [[ ${CLUSTER_REGISTERED:-0} == 1 ]] || return "$CLUSTER_FINISH_RC"
    cluster_env_load_file /run/cluster/metadata.env || return "$CLUSTER_FINISH_RC"
    if (( NODE_RANK == 0 )); then
        cluster_status done "$CLUSTER_FINISH_RC"
    else
        cluster_status failed "$CLUSTER_FINISH_RC"
    fi
    return "$CLUSTER_FINISH_RC"
}
cluster_task() {
    set +m
    set -e -E -o pipefail
    trap 'cluster_finish "$?"' EXIT
    cluster_initialize
    cluster_mesh
    cluster_mesh_barrier
    cluster_status setup
    cluster_env_clean
    cluster_env_load_file /run/cluster/environment.env
    cd -- "$SOURCE_DIR"
    # Deliberately source in this shell, outside any conditional: errexit propagates.
    source /run/cluster/setup.sh
    set -e -E -o pipefail
    cluster_env_clean
    cluster_env_save
    cd -- "$SOURCE_DIR"
    cluster_status ready
    if (( NODE_RANK == 0 )); then
        cluster_ready_barrier
        cluster_status running
        # Inherit setup's shell state, but keep supervision alive across user exec/exit.
        ( trap - EXIT; set +e; source /run/cluster/command.sh ) <&0 &
        local CLUSTER_COMMAND_PID=$! CLUSTER_COMMAND_RC
        if wait "$CLUSTER_COMMAND_PID"; then CLUSTER_COMMAND_RC=0; else CLUSTER_COMMAND_RC=$?; fi
        printf '%s\n' "$CLUSTER_COMMAND_RC" > /run/cluster/command.exit
        return "$CLUSTER_COMMAND_RC"
    else
        while :; do
            cluster_check_deadline
            /bin/sleep 0.2
        done
    fi
}
cluster_kill_tree() {
    local CLUSTER_PID=$1 CLUSTER_CHILD CLUSTER_CHILDREN
    cluster_number "$CLUSTER_PID" || return 0
    [[ -r /proc/$CLUSTER_PID/task/$CLUSTER_PID/children ]] || return 0
    CLUSTER_CHILDREN=$(< /proc/$CLUSTER_PID/task/$CLUSTER_PID/children)
    for CLUSTER_CHILD in $CLUSTER_CHILDREN; do cluster_kill_tree "$CLUSTER_CHILD"; done
    kill -TERM "$CLUSTER_PID" 2>/dev/null || :
}
cluster_cleanup() {
    local CLUSTER_CLEANUP_TASK=$1 CLUSTER_LISTENER= CLUSTER_FILE CLUSTER_SESSION CLUSTER_CHILD
    trap - TERM INT HUP
    [[ ! -f /run/cluster/listener.pending ]] || CLUSTER_LISTENER=$(< /run/cluster/listener.pending)
    [[ ! -f /run/cluster/listener.pid ]] || CLUSTER_LISTENER=$(< /run/cluster/listener.pid)
    for CLUSTER_FILE in /run/cluster/sessions/*; do
        [[ -f $CLUSTER_FILE && ! -L $CLUSTER_FILE ]] || continue
        CLUSTER_SESSION=${CLUSTER_FILE##*/}
        CLUSTER_CHILD=$(< "$CLUSTER_FILE")
        if cluster_number "$CLUSTER_CHILD"; then kill -TERM -- "-$CLUSTER_CHILD" 2>/dev/null || :; fi
        if cluster_number "$CLUSTER_SESSION"; then kill -TERM "$CLUSTER_SESSION" 2>/dev/null || :; fi
    done
    cluster_kill_tree "$CLUSTER_CLEANUP_TASK"
    kill -TERM -- "-$CLUSTER_CLEANUP_TASK" 2>/dev/null || :
    if cluster_number "$CLUSTER_LISTENER"; then
        cluster_kill_tree "$CLUSTER_LISTENER"
        kill -TERM -- "-$CLUSTER_LISTENER" 2>/dev/null || :
    fi
    /bin/sleep 0.2
    for CLUSTER_FILE in /run/cluster/sessions/*; do
        [[ -f $CLUSTER_FILE && ! -L $CLUSTER_FILE ]] || continue
        CLUSTER_CHILD=$(< "$CLUSTER_FILE")
        if cluster_number "$CLUSTER_CHILD"; then kill -KILL -- "-$CLUSTER_CHILD" 2>/dev/null || :; fi
    done
    kill -KILL -- "-$CLUSTER_CLEANUP_TASK" 2>/dev/null || :
    if cluster_number "$CLUSTER_LISTENER"; then kill -KILL -- "-$CLUSTER_LISTENER" 2>/dev/null || :; fi
    wait "$CLUSTER_CLEANUP_TASK" 2>/dev/null || :
}
cluster_main() {
    local CLUSTER_MAIN_NAME CLUSTER_MAIN_TASK CLUSTER_MAIN_LISTENER CLUSTER_MAIN_RC=0 CLUSTER_MAIN_RANK CLUSTER_MAIN_PHASE CLUSTER_HEALTH_NOW CLUSTER_HEALTH_LAST=0
    for CLUSTER_MAIN_NAME in NNODES NODE_RANK RANK WORLD_SIZE NPROC_PER_NODE MASTER_PORT PPU_DEADLINE; do
        cluster_number "${!CLUSTER_MAIN_NAME}" || { cluster_error "invalid $CLUSTER_MAIN_NAME"; return 65; }
        printf -v "$CLUSTER_MAIN_NAME" '%d' "$((10#${!CLUSTER_MAIN_NAME}))"
        export "$CLUSTER_MAIN_NAME"
    done
    PPU_SSH_PORT=${PPU_SSH_PORT:-2222}
    cluster_number "$PPU_SSH_PORT" || return 65
    PPU_SSH_PORT=$((10#$PPU_SSH_PORT))
    export PPU_SSH_PORT
    (( NNODES > 0 && NODE_RANK < NNODES && RANK == NODE_RANK && WORLD_SIZE == NNODES &&
       NPROC_PER_NODE > 0 && MASTER_PORT > 0 && MASTER_PORT <= 65535 &&
       PPU_SSH_PORT > 0 && PPU_SSH_PORT <= 65535 )) || return 65
    [[ ${POD_IP:-} =~ ^[0-9A-Fa-f:.]+$ && ${HOST_IP:-} =~ ^[0-9A-Fa-f:.]+$ &&
       ${POD_UID:-} =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ &&
       -n ${MASTER_ADDR:-} && -d ${SOURCE_DIR:-} ]] || return 65
    # Markers are local to this runtime attempt, never stored on NAS.
    /bin/rm -f /run/cluster/deadline-exceeded /run/cluster/command.exit || return 73
    cluster_check_deadline || {
        CLUSTER_MAIN_RC=$?
        [[ ! -f /run/cluster/deadline-exceeded ]] || CLUSTER_OUTCOME=timeout
        return "$CLUSTER_MAIN_RC"
    }
    unset BASH_ENV ENV
    set -m
    ( cluster_task; exit $? ) &
    CLUSTER_MAIN_TASK=$!
    set +m
    trap 'CLUSTER_OUTCOME=runtime; cluster_cleanup "$CLUSTER_MAIN_TASK"; exit 143' TERM
    trap 'CLUSTER_OUTCOME=runtime; cluster_cleanup "$CLUSTER_MAIN_TASK"; exit 130' INT
    trap 'CLUSTER_OUTCOME=runtime; cluster_cleanup "$CLUSTER_MAIN_TASK"; exit 129' HUP
    while kill -0 "$CLUSTER_MAIN_TASK" 2>/dev/null; do
        # Completion wins over the next deadline/health poll, including the
        # brief interval in which the task is publishing its final status.
        [[ ! -f /run/cluster/command.exit ]] || break
        if ! cluster_remaining; then
            CLUSTER_MAIN_PHASE=$(/usr/bin/jq -r .phase /run/cluster/status.json 2>/dev/null)
            if [[ -f /run/cluster/command.exit || $CLUSTER_MAIN_PHASE == done ]] || ! kill -0 "$CLUSTER_MAIN_TASK" 2>/dev/null; then break; fi
            if cluster_mark_deadline; then CLUSTER_MAIN_RC=124; else CLUSTER_MAIN_RC=$?; fi
            break
        fi
        if [[ -f /run/cluster/listener.pid ]]; then
            CLUSTER_MAIN_LISTENER=$(< /run/cluster/listener.pid)
            if ! cluster_number "$CLUSTER_MAIN_LISTENER" || ! kill -0 "$CLUSTER_MAIN_LISTENER" 2>/dev/null; then
                [[ ! -f /run/cluster/command.exit ]] || break
                kill -0 "$CLUSTER_MAIN_TASK" 2>/dev/null || break
                cluster_error 'SSH listener died'
                CLUSTER_MAIN_RC=70
                break
            fi
        fi
        if (( NODE_RANK == 0 )) && [[ -f /run/cluster/status.json ]]; then
            CLUSTER_MAIN_PHASE=$(/usr/bin/jq -r .phase /run/cluster/status.json 2>/dev/null)
            CLUSTER_HEALTH_NOW=$(/bin/date +%s)
            if [[ $CLUSTER_MAIN_PHASE == running ]] && (( CLUSTER_HEALTH_NOW > CLUSTER_HEALTH_LAST )); then
                CLUSTER_HEALTH_LAST=$CLUSTER_HEALTH_NOW
                CLUSTER_JOB_ID=$(< /run/cluster/job-id)
                CLUSTER_REGISTRY=/mnt/wl_nas/devops/runtime/master/$CLUSTER_JOB_ID/nodes
                for ((CLUSTER_MAIN_RANK=1; CLUSTER_MAIN_RANK<NNODES; CLUSTER_MAIN_RANK++)); do
                    if ! cluster_peer "$CLUSTER_MAIN_RANK"; then
                        CLUSTER_MAIN_RC=70
                    elif [[ $(/usr/bin/jq -r .phase <<< "$CLUSTER_PROBE") == failed ]]; then
                        CLUSTER_MAIN_RC=$(/usr/bin/jq -r .exit_code <<< "$CLUSTER_PROBE")
                        (( CLUSTER_MAIN_RC != 0 )) || CLUSTER_MAIN_RC=70
                    fi
                    if (( CLUSTER_MAIN_RC != 0 )); then
                        # A stalled probe may outlive the command. Recheck its
                        # completion before choosing the framework deadline.
                        CLUSTER_MAIN_PHASE=$(/usr/bin/jq -r .phase /run/cluster/status.json 2>/dev/null)
                        if [[ -f /run/cluster/command.exit || $CLUSTER_MAIN_PHASE != running ]] || ! kill -0 "$CLUSTER_MAIN_TASK" 2>/dev/null; then
                            CLUSTER_MAIN_RC=0
                        elif ! cluster_remaining; then
                            if cluster_mark_deadline; then CLUSTER_MAIN_RC=124; else CLUSTER_MAIN_RC=$?; fi
                        fi
                        break
                    fi
                done
                if (( CLUSTER_MAIN_RC != 0 )); then cluster_error 'worker SSH service failed'; break; fi
            fi
        fi
        /bin/sleep 0.2
    done
    if (( CLUSTER_MAIN_RC == 0 )); then
        wait "$CLUSTER_MAIN_TASK"
        CLUSTER_MAIN_RC=$?
        [[ ! -f /run/cluster/command.exit ]] || CLUSTER_OUTCOME=command
    fi
    cluster_cleanup "$CLUSTER_MAIN_TASK"
    [[ ! -f /run/cluster/deadline-exceeded ]] || CLUSTER_OUTCOME=timeout
    return "$CLUSTER_MAIN_RC"
}
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    unset CLUSTER_OUTCOME CLUSTER_EXIT_CODE
    CLUSTER_OUTCOME=runtime
    trap 'CLUSTER_EXIT_CODE=$?; trap - EXIT; cluster_termination || exit 73; exit "$CLUSTER_EXIT_CODE"' EXIT
    # Strip inherited reserved exports before main installs dynamically scoped
    # locals: unexporting a local alone can expose an exported shadowed global.
    cluster_env_clean
    cluster_main "$@"
    exit $?
fi
