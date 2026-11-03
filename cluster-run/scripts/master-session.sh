#!/bin/bash
# sshd's only entry point. The probe is independent of user setup/environment.
source /run/cluster/master-worker.sh
unset CLUSTER_SESSION_INTENT
CLUSTER_SESSION_INTENT=${SSH_ORIGINAL_COMMAND:-}
if [[ $CLUSTER_SESSION_INTENT == __ppu_master_probe_v1__ ]]; then
    cluster_regular /run/cluster/status.json 4096 || exit 65
    exec /bin/cat /run/cluster/status.json
fi
cluster_env_clean
cluster_env_load_file /run/cluster/environment.env || exit 65
cluster_env_load_file /run/cluster/metadata.env || exit 65
export PATH="/run/cluster/bin:${PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"
unset BASH_ENV ENV
cd -- "$SOURCE_DIR" || exit 65
# No eval, no env-as-source, no startup files. SSH_ORIGINAL_COMMAND is the
# authenticated caller's shell intent; stdin remains the original SSH stream.
set -m
if [[ -n $CLUSTER_SESSION_INTENT ]]; then
    /bin/bash --noprofile --norc -c "$CLUSTER_SESSION_INTENT" <&0 &
else
    /bin/bash --noprofile --norc <&0 &
fi
child=$!
set +m
printf '%s\n' "$child" > /run/cluster/sessions/$$
finish_session() {
    local rc=$1
    trap - EXIT TERM INT HUP
    kill -TERM -- "-$child" 2>/dev/null || :
    /bin/sleep 0.2
    kill -KILL -- "-$child" 2>/dev/null || :
    /bin/rm -f /run/cluster/sessions/$$
    exit "$rc"
}
trap 'finish_session 143' TERM
trap 'finish_session 130' INT
trap 'finish_session 129' HUP
wait "$child"
finish_session "$?"
