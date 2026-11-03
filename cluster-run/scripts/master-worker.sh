#!/bin/bash
# Also sourced, without side effects, by the fixed SSH entry points.

cluster_error() { printf 'master runtime: %s\n' "$*" >&2; }
cluster_number() { [[ $1 =~ ^[0-9]+$ && ${#1} -le 10 ]]; }
cluster_remaining() {
    local now
    now=$(/bin/date +%s) || return 1
    cluster_number "${PPU_DEADLINE:-}" || return 1
    CLUSTER_REMAINING=$((10#$PPU_DEADLINE - now))
    (( CLUSTER_REMAINING > 0 ))
}
cluster_regular() {
    local file=$1 limit=$2 size
    [[ -f $file && ! -L $file ]] || return 1
    size=$(/usr/bin/wc -c < "$file") || return 1
    (( size > 0 && size <= limit ))
}
cluster_safe_dir() {
    local path=$1 create=${2:-1} part current= oldifs=$IFS
    local -a parts
    [[ $path == /* ]] || return 1
    IFS=/ read -r -a parts <<< "$path"
    IFS=$oldifs
    for part in "${parts[@]}"; do
        [[ -n $part ]] || continue
        [[ $part != . && $part != .. ]] || return 1
        current=$current/$part
        [[ ! -L $current ]] || return 1
        if [[ ! -d $current ]]; then
            [[ $create == 1 ]] || return 1
            /bin/mkdir "$current" 2>/dev/null || [[ -d $current && ! -L $current ]] || return 1
        fi
        [[ -d $current && ! -L $current ]] || return 1
    done
}
cluster_registry_owner() {
    local root=/mnt/wl_nas/devops/runtime/master listing mode links uid gid rest
    [[ $CLUSTER_JOB_ID =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ &&
       $CLUSTER_REGISTRY == "$root/$CLUSTER_JOB_ID/nodes" ]] || {
        cluster_error 'invalid registry path'; return 1;
    }
    # Infra owns this fixed root. Never create it or infer ownership from input.
    cluster_safe_dir "$root" 0 || {
        cluster_error "registry root must be pre-provisioned without symlinks: $root"; return 1;
    }
    listing=$(LC_ALL=C /bin/ls -dn "$root") || {
        cluster_error "cannot read registry root ownership: $root"; return 1;
    }
    IFS=' ' read -r mode links uid gid rest <<< "$listing"
    cluster_number "$uid" && cluster_number "$gid" || {
        cluster_error "cannot read numeric registry root ownership: $root"; return 1;
    }
    CLUSTER_REGISTRY_OWNER=$uid:$gid
}
cluster_registry_chown() {
    local chown=/bin/chown
    [[ -x $chown ]] || chown=/usr/sbin/chown
    "$chown" "$CLUSTER_REGISTRY_OWNER" "$@" || {
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
    local rank=$1 file=$2 host=$3
    cluster_regular "$file" 4096 || return 1
    /usr/bin/jq -e --arg job "$CLUSTER_JOB_ID" --arg host "$host" --argjson rank "$rank" '
      type == "object" and .jobid == $job and .rank == $rank and .host == $host and
      (.port | type) == "number" and .port >= 1 and .port <= 65535 and
      .port == (.port | floor) and (.pod_uid | type) == "string" and
      (.pod_uid | test("^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")) and
      (.phase as $phase | ["online", "connected", "setup", "ready", "running", "done", "finished", "failed"] | index($phase) != null)
    ' "$file" >/dev/null || return 1
    CLUSTER_RECORD=$(/usr/bin/jq -c . "$file")
}
cluster_port_candidates() {
    local preferred=$1 master=$2 low=$3 high=$4 seed=$5 offset port emitted=0
    if (( preferred != master && (preferred < low || preferred > high) )); then
        printf '%s\n' "$preferred"
        emitted=1
    fi
    for ((offset=0; offset<5000 && emitted<128; offset++)); do
        port=$((20000 + (seed + offset) % 5000))
        (( port != master && port != preferred && (port < low || port > high) )) || continue
        printf '%s\n' "$port"
        emitted=$((emitted + 1))
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
    local entry name value
    [[ -f $1 && ! -L $1 ]] || return 1
    while IFS= read -r -d '' entry; do
        name=${entry%%=*}
        [[ $entry == *=* ]] || return 1
        value=${entry#*=}
        if cluster_env_allowed "$name" || cluster_metadata_name "$name"; then
            # Assignment is data, never parsed as shell source.
            export "$name=$value"
        fi
    done < "$1"
}
cluster_env_clean() {
    local entry name
    while IFS= read -r -d '' entry; do
        name=${entry%%=*}
        if ! cluster_env_allowed "$name" && ! cluster_metadata_name "$name"; then
            unset "$name" 2>/dev/null || :
        fi
    done < <(/usr/bin/env -0)
    unset BASH_ENV ENV CDPATH GLOBIGNORE PROMPT_COMMAND
}
cluster_env_save() {
    local entry name temp=/run/cluster/environment.tmp.$$
    cluster_env_load_file /run/cluster/metadata.env || return 1
    export PATH="/run/cluster/bin:${PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"
    (
        while IFS= read -r -d '' entry; do
            name=${entry%%=*}
            if cluster_env_allowed "$name" || cluster_metadata_name "$name"; then
                printf '%s\0' "$entry"
            fi
        done < <(/usr/bin/env -0)
    ) > "$temp" || return 1
    /bin/chmod 600 "$temp" && /bin/mv -f "$temp" /run/cluster/environment.env
}
cluster_status() {
    local phase=$1 rc=${2:-0} temp=/run/cluster/status.tmp.$$
    /usr/bin/jq -cn --arg job "$CLUSTER_JOB_ID" --arg host "$CLUSTER_HOST" \
        --arg uid "$POD_UID" --arg pod_ip "$POD_IP" --arg host_ip "$HOST_IP" \
        --arg phase "$phase" --argjson rank "$NODE_RANK" --argjson port "$CLUSTER_PORT" \
        --argjson rc "$rc" \
        '{jobid:$job,rank:$rank,host:$host,port:$port,pod_uid:$uid,phase:$phase,
          pod_ip:$pod_ip,host_ip:$host_ip,exit_code:$rc}' > "$temp" || return 1
    /bin/mv -f "$temp" /run/cluster/status.json || return 1
    case $phase in
        ready|running) /usr/bin/touch /run/cluster/ready ;;
        *) /bin/rm -f /run/cluster/ready ;;
    esac
    [[ ${CLUSTER_REGISTERED:-0} == 1 ]] || return 0
    cluster_registry_owner || return 1
    local target=$CLUSTER_REGISTRY/$NODE_RANK.json
    [[ ! -L $target ]] || return 1
    cluster_safe_dir "$CLUSTER_REGISTRY" 0 || return 1
    temp=$(/usr/bin/mktemp "$CLUSTER_REGISTRY/.${NODE_RANK}.XXXXXXXX") || return 1
    if /usr/bin/jq '{jobid,rank,host,port,pod_uid,phase}' /run/cluster/status.json > "$temp" &&
        cluster_registry_chown "$temp" && [[ ! -L $target ]] &&
        /bin/mv -f "$temp" "$target"; then
        return 0
    fi
    /bin/rm -f "$temp"
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
        -o ControlMaster=no -o ControlPath=none -o ProxyCommand=none -o ProxyJump=none
        -o ConnectionAttempts=1 -o ConnectTimeout=2 -o ServerAliveInterval=2
        -o ServerAliveCountMax=2 -o LogLevel=ERROR)
}
cluster_probe() {
    local rank=$1 host=$2 port=$3 uid=$4 reply budget
    cluster_remaining || return 124
    budget=$CLUSTER_REMAINING
    (( budget <= 5 )) || budget=5
    cluster_ssh_options
    # A hard deadline bounds authentication, commands and stalled transports.
    reply=$(/usr/bin/timeout -k 1s "${budget}s" /usr/bin/ssh "${CLUSTER_SSH_ARGS[@]}" \
        -o "HostKeyAlias=cluster-rank-$rank" -p "$port" -- "root@$host" \
        __ppu_master_probe_v1__ 2>/dev/null) || return 1
    (( ${#reply} <= 4096 )) || return 1
    /usr/bin/jq -e --arg job "$CLUSTER_JOB_ID" --arg host "$host" --arg uid "$uid" \
        --argjson rank "$rank" --argjson port "$port" '
        type == "object" and .jobid == $job and .host == $host and
        .rank == $rank and .port == $port and .pod_uid == $uid and
        (.phase as $phase | ["online", "connected", "setup", "ready", "running", "done", "finished", "failed"] | index($phase) != null) and
        (.exit_code | type) == "number" and .exit_code >= 0 and .exit_code <= 255 and
        .exit_code == (.exit_code | floor) and
        (.pod_ip | type) == "string" and (.host_ip | type) == "string" and
        (.pod_ip | test("^[0-9A-Fa-f:.]+$")) and (.host_ip | test("^[0-9A-Fa-f:.]+$"))
        ' <<< "$reply" >/dev/null || return 1
    CLUSTER_PROBE=$reply
}
cluster_peer() {
    local rank=$1 host
    host=$(/usr/bin/jq -r --argjson rank "$rank" '.[$rank].host' /run/cluster/trusted-nodes.json) || return 1
    cluster_safe_dir "$CLUSTER_REGISTRY" || return 1
    cluster_registry_read "$rank" "$CLUSTER_REGISTRY/$rank.json" "$host" || return 1
    local port uid
    port=$(/usr/bin/jq -r .port <<< "$CLUSTER_RECORD")
    uid=$(/usr/bin/jq -r .pod_uid <<< "$CLUSTER_RECORD")
    cluster_probe "$rank" "$host" "$port" "$uid"
}
cluster_check_deadline() {
    cluster_remaining && return 0
    cluster_error 'deadline exceeded'
    return 124
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
    local name rank type key comment host low high seed port listener i
    for name in master-worker.sh master-session.sh cluster-ssh.sh cluster-exec.sh identity identity.pub nodes.json job-id "host-$NODE_RANK"; do
        [[ -f /run/cluster-config/$name ]] || return 65
        cp /run/cluster-config/"$name" /run/cluster/"$name"
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
    for ((rank=0; rank<NNODES; rank++)); do
        cp /run/cluster-config/"host-$rank.pub" /run/cluster/"host-$rank.pub"
        cluster_regular /run/cluster/"host-$rank.pub" 16384 || return 65
        ssh-keygen -l -f /run/cluster/"host-$rank.pub" >/dev/null || return 65
        IFS=' ' read -r type key comment < /run/cluster/"host-$rank.pub"
        printf 'cluster-rank-%s %s %s\n' "$rank" "$type" "$key" >> /run/cluster/known_hosts
    done
    ssh-keygen -l -f /run/cluster/identity.pub >/dev/null
    printf 'restrict ' > /run/cluster/authorized_keys
    /bin/cat /run/cluster/identity.pub >> /run/cluster/authorized_keys
    export CLUSTER_HOSTS_FILE=/run/cluster/hosts CLUSTER_NODES_FILE=/run/cluster/nodes.json
    : > /run/cluster/metadata.env
    for name in NNODES NODE_RANK RANK WORLD_SIZE NPROC_PER_NODE MASTER_ADDR MASTER_PORT SOURCE_DIR POD_IP HOST_IP POD_UID PPU_SSH_PORT PPU_DEADLINE CLUSTER_HOSTS_FILE CLUSTER_NODES_FILE; do
        printf '%s=%s\0' "$name" "${!name}" >> /run/cluster/metadata.env
    done
    cluster_env_save
    low=32768 high=60999
    if [[ -r /proc/sys/net/ipv4/ip_local_port_range ]]; then
        read -r low high < /proc/sys/net/ipv4/ip_local_port_range
        cluster_number "$low" && cluster_number "$high" || return 65
    fi
    seed=$(( (RANDOM * 32768 + RANDOM) % 5000 ))
    local bind=$POD_IP
    [[ $POD_IP == "$HOST_IP" ]] && bind=$HOST_IP
    while IFS= read -r port; do
        cluster_check_deadline
        CLUSTER_PORT=$port
        cluster_status online
        {
            printf 'Port %s\nListenAddress %s\nHostKey /run/cluster/host-%s\n' "$port" "$bind" "$NODE_RANK"
            printf '%s\n' 'PidFile /run/cluster/sshd.pid' 'AuthorizedKeysFile /run/cluster/authorized_keys' \
                'PermitRootLogin prohibit-password' 'PubkeyAuthentication yes' 'PasswordAuthentication no' \
                'KbdInteractiveAuthentication no' 'AuthenticationMethods publickey' 'UsePAM yes' \
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
        listener=$!
        set +m
        printf '%s\n' "$listener" > /run/cluster/listener.pending
        for ((i=0; i<10; i++)); do
            cluster_check_deadline
            kill -0 "$listener" 2>/dev/null || break
            # Local dialing uses the IP, while metadata always identifies the trusted FQDN.
            cluster_remaining || return 124
            cluster_ssh_options
            local reply
            if reply=$(/usr/bin/timeout -k 1s "${CLUSTER_REMAINING}s" /usr/bin/ssh "${CLUSTER_SSH_ARGS[@]}" \
                -o "HostKeyAlias=cluster-rank-$NODE_RANK" -p "$port" -- "root@$bind" __ppu_master_probe_v1__ 2>/dev/null); then
                if /usr/bin/jq -e --arg job "$CLUSTER_JOB_ID" --arg host "$CLUSTER_HOST" --arg uid "$POD_UID" \
                    --argjson rank "$NODE_RANK" --argjson port "$port" \
                    '.jobid == $job and .host == $host and .pod_uid == $uid and .rank == $rank and .port == $port' \
                    <<< "$reply" >/dev/null && kill -0 "$listener" 2>/dev/null; then
                    printf '%s\n' "$listener" > /run/cluster/listener.pid
                    CLUSTER_REGISTERED=1
                    cluster_status online
                    return 0
                fi
            fi
            /bin/sleep 0.2
        done
        if kill -0 "$listener" 2>/dev/null; then
            cluster_error 'sshd bound but public-key self-authentication failed (check the PAM account policy)'
            kill -TERM -- "-$listener" 2>/dev/null || :
            wait "$listener" 2>/dev/null || :
            return 69
        fi
        wait "$listener" 2>/dev/null || :
    done < <(cluster_port_candidates "$PPU_SSH_PORT" "$MASTER_PORT" "$low" "$high" "$seed")
    cluster_error 'no usable SSH port within 128 candidates'
    return 69
}
cluster_mesh() {
    local rank phase temp=/run/cluster/nodes.tmp.$$
    printf '[]\n' > "$temp"
    : > /run/cluster/hosts
    for ((rank=0; rank<NNODES; rank++)); do
        until cluster_peer "$rank"; do cluster_check_deadline; /bin/sleep 0.2; done
        phase=$(/usr/bin/jq -r .phase <<< "$CLUSTER_PROBE")
        if [[ $phase == failed || $phase == done ]]; then return 70; fi
        local next=$temp.next
        /usr/bin/jq --argjson peer "$CLUSTER_PROBE" --argjson rank "$rank" \
            --slurpfile trusted /run/cluster/trusted-nodes.json \
            '. + [($trusted[0][$rank] + {port:$peer.port,pod_uid:$peer.pod_uid,pod_ip:$peer.pod_ip,host_ip:$peer.host_ip})]' \
            "$temp" > "$next"
        /bin/mv -f "$next" "$temp"
        /usr/bin/jq -r .host <<< "$CLUSTER_PROBE" >> /run/cluster/hosts
    done
    /bin/mv -f "$temp" /run/cluster/nodes.json
}
cluster_mesh_barrier() {
    local rank phase rc
    cluster_status connected
    for ((rank=0; rank<NNODES; rank++)); do
        while :; do
            cluster_check_deadline
            if ! cluster_peer "$rank"; then
                cluster_check_deadline || return 124
                return 70
            else
                phase=$(/usr/bin/jq -r .phase <<< "$CLUSTER_PROBE")
                case $phase in
                    connected|setup|ready|running) break ;;
                    failed|done|finished)
                        rc=$(/usr/bin/jq -r .exit_code <<< "$CLUSTER_PROBE")
                        (( rc != 0 )) || rc=70
                        return "$rc" ;;
                esac
            fi
            /bin/sleep 0.2
        done
    done
}
cluster_ready_barrier() {
    local rank phase rc
    for ((rank=0; rank<NNODES; rank++)); do
        while :; do
            cluster_check_deadline
            if ! cluster_peer "$rank"; then
                cluster_check_deadline || return 124
                return 70
            else
                phase=$(/usr/bin/jq -r .phase <<< "$CLUSTER_PROBE")
                if [[ $phase == failed ]]; then
                    rc=$(/usr/bin/jq -r .exit_code <<< "$CLUSTER_PROBE")
                    (( rc != 0 )) || rc=70
                    return "$rc"
                fi
                [[ $phase == ready ]] && break
                [[ $phase != done && $phase != finished ]] || return 70
            fi
            /bin/sleep 0.2
        done
    done
}
cluster_finish() {
    local rc=$1
    trap - EXIT
    set +e
    [[ ${CLUSTER_REGISTERED:-0} == 1 ]] || return "$rc"
    cluster_env_load_file /run/cluster/metadata.env || return "$rc"
    if (( NODE_RANK == 0 )); then
        cluster_status done "$rc"
    else
        cluster_status failed "$rc"
    fi
    return "$rc"
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
        local command_pid=$!
        wait "$command_pid"
    else
        while :; do
            cluster_check_deadline
            /bin/sleep 0.2
        done
    fi
}
cluster_kill_tree() {
    local pid=$1 child children
    cluster_number "$pid" || return 0
    [[ -r /proc/$pid/task/$pid/children ]] || return 0
    children=$(< /proc/$pid/task/$pid/children)
    for child in $children; do cluster_kill_tree "$child"; done
    kill -TERM "$pid" 2>/dev/null || :
}
cluster_cleanup() {
    local task=$1 listener= file session child
    trap - TERM INT HUP
    [[ ! -f /run/cluster/listener.pending ]] || listener=$(< /run/cluster/listener.pending)
    [[ ! -f /run/cluster/listener.pid ]] || listener=$(< /run/cluster/listener.pid)
    for file in /run/cluster/sessions/*; do
        [[ -f $file && ! -L $file ]] || continue
        session=${file##*/}
        child=$(< "$file")
        if cluster_number "$child"; then kill -TERM -- "-$child" 2>/dev/null || :; fi
        if cluster_number "$session"; then kill -TERM "$session" 2>/dev/null || :; fi
    done
    cluster_kill_tree "$task"
    kill -TERM -- "-$task" 2>/dev/null || :
    if cluster_number "$listener"; then
        cluster_kill_tree "$listener"
        kill -TERM -- "-$listener" 2>/dev/null || :
    fi
    /bin/sleep 0.2
    for file in /run/cluster/sessions/*; do
        [[ -f $file && ! -L $file ]] || continue
        child=$(< "$file")
        if cluster_number "$child"; then kill -KILL -- "-$child" 2>/dev/null || :; fi
    done
    kill -KILL -- "-$task" 2>/dev/null || :
    if cluster_number "$listener"; then kill -KILL -- "-$listener" 2>/dev/null || :; fi
    wait "$task" 2>/dev/null || :
}
cluster_main() {
    local name task listener rc=0 rank phase health_now health_last=0
    for name in NNODES NODE_RANK RANK WORLD_SIZE NPROC_PER_NODE MASTER_PORT PPU_DEADLINE; do
        cluster_number "${!name}" || { cluster_error "invalid $name"; return 65; }
        printf -v "$name" '%d' "$((10#${!name}))"
        export "$name"
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
    cluster_check_deadline || return 124
    unset BASH_ENV ENV
    set -m
    ( cluster_task; exit $? ) &
    task=$!
    set +m
    trap 'cluster_cleanup "$task"; exit 143' TERM
    trap 'cluster_cleanup "$task"; exit 130' INT
    trap 'cluster_cleanup "$task"; exit 129' HUP
    while kill -0 "$task" 2>/dev/null; do
        if ! cluster_remaining; then rc=124; break; fi
        if [[ -f /run/cluster/listener.pid ]]; then
            listener=$(< /run/cluster/listener.pid)
            if ! cluster_number "$listener" || ! kill -0 "$listener" 2>/dev/null; then
                cluster_error 'SSH listener died'
                rc=70
                break
            fi
        fi
        if (( NODE_RANK == 0 )) && [[ -f /run/cluster/status.json ]]; then
            phase=$(/usr/bin/jq -r .phase /run/cluster/status.json 2>/dev/null)
            health_now=$(/bin/date +%s)
            if [[ $phase == running ]] && (( health_now > health_last )); then
                health_last=$health_now
                CLUSTER_JOB_ID=$(< /run/cluster/job-id)
                CLUSTER_REGISTRY=/mnt/wl_nas/devops/runtime/master/$CLUSTER_JOB_ID/nodes
                for ((rank=1; rank<NNODES; rank++)); do
                    if ! cluster_peer "$rank"; then
                        rc=70
                    elif [[ $(/usr/bin/jq -r .phase <<< "$CLUSTER_PROBE") == failed ]]; then
                        rc=$(/usr/bin/jq -r .exit_code <<< "$CLUSTER_PROBE")
                        (( rc != 0 )) || rc=70
                    fi
                    if (( rc != 0 )); then
                        if ! cluster_remaining; then rc=124; break; fi
                        # The command may have completed while its workers were probed.
                        phase=$(/usr/bin/jq -r .phase /run/cluster/status.json 2>/dev/null)
                        [[ $phase == running ]] || rc=0
                        break
                    fi
                done
                if (( rc != 0 )); then cluster_error 'worker SSH service failed'; break; fi
            fi
        fi
        /bin/sleep 0.2
    done
    if (( rc == 0 )); then wait "$task"; rc=$?; fi
    cluster_cleanup "$task"
    return "$rc"
}
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    cluster_main "$@"
    exit $?
fi
