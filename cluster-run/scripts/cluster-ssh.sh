#!/bin/bash
source /run/cluster/master-worker.sh
fail() { cluster_error "$*"; exit 64; }
options=()
host=
while (( $# )); do
    case $1 in
        --) shift; (( $# )) || fail 'missing host'; host=$1; shift; break ;;
        -v|-vv|-vvv|-q|-T|-t|-tt|-n|-x|-C) options[${#options[@]}]=$1; shift ;;
        -l|-lroot)
            if [[ $1 == -l ]]; then
                (( $# >= 2 )) && [[ $2 == root ]] || fail 'only root is supported'
                shift 2
            else shift; fi ;;
        -o|-o*)
            if [[ $1 == -o ]]; then
                (( $# >= 2 )) || fail 'missing SSH option'
                option=$2; shift 2
            else option=${1#-o}; shift; fi
            # Native -o accepts Key=Value and a single argv containing Key Value.
            option=${option/ /=}
            [[ $option == *=* ]] || fail 'SSH options require a value'
            key=${option%%=*}; value=${option#*=}
            key=$(printf '%s' "$key" | /usr/bin/tr '[:upper:]' '[:lower:]')
            case $key in
                batchmode|clearallforwardings|stricthostkeychecking|identitiesonly) [[ $value == yes ]] || fail "unsafe $key" ;;
                forwardagent|forwardx11|passwordauthentication|kbdinteractiveauthentication|permitlocalcommand)
                    [[ $value == no ]] || fail "unsafe $key" ;;
                preferredauthentications) [[ $value == publickey ]] || fail "unsafe $key" ;;
                connecttimeout|serveraliveinterval|serveralivecountmax)
                    cluster_number "$value" && (( 10#$value >= 1 && 10#$value <= 30 )) || fail "invalid $key" ;;
                connectionattempts) [[ $value == 1 ]] || fail "unsafe $key" ;;
                loglevel) case $value in QUIET|FATAL|ERROR|INFO|VERBOSE|DEBUG|DEBUG1|DEBUG2|DEBUG3) ;; *) fail 'invalid LogLevel' ;; esac ;;
                requesttty) case $value in yes|no|auto|force) ;; *) fail 'invalid RequestTTY' ;; esac ;;
                addressfamily) case $value in any|inet|inet6) ;; *) fail 'invalid AddressFamily' ;; esac ;;
                compression|tcpkeepalive) case $value in yes|no) ;; *) fail "invalid $key" ;; esac ;;
                *) fail "unsupported or unsafe SSH option: $key" ;;
            esac
            options[${#options[@]}]=-o
            options[${#options[@]}]=$option
            ;;
        -*) fail "unsupported or unsafe SSH argument: $1" ;;
        *) host=$1; shift; break ;;
    esac
done
[[ -n $host ]] || fail 'usage: cluster-ssh [safe SSH options] [root@]host [command ...]'
case $host in root@*) host=${host#root@} ;; *@*) fail 'only root is supported' ;; esac
[[ $host =~ ^[A-Za-z0-9][A-Za-z0-9.:-]*$ ]] || fail 'invalid host'
cluster_regular /run/cluster/nodes.json 1048576 || fail 'missing trusted local nodes mapping'
# Every alias must resolve uniquely; all dialing still uses the trusted FQDN.
record=$(/usr/bin/jq -ce --arg host "$host" '
    . as $nodes |
    ([$nodes[] | select(.host == $host)]) as $exact |
    if ($exact | length) > 0 then $exact else
      ([$nodes[] | select(.pod_ip == $host or .host_ip == $host)]) as $address |
      if ($address | length) > 0 then $address
      elif ($host | test("^[A-Za-z0-9][A-Za-z0-9-]*$")) then
        [$nodes[] | select((.host | split(".")[0]) == $host)]
      else [] end
    end |
    if length == 1 then .[0] else error("unknown or ambiguous host") end |
    select((.rank | type) == "number" and .rank >= 0 and .rank == (.rank | floor) and
           (.host | type) == "string" and (.host | test("^[A-Za-z0-9][A-Za-z0-9.-]*$")) and
           (.port | type) == "number" and .port >= 1 and .port <= 65535 and .port == (.port | floor))
    ' /run/cluster/nodes.json 2>/dev/null) || fail 'unknown, ambiguous or invalid cluster host'
rank=$(/usr/bin/jq -r .rank <<< "$record")
target=$(/usr/bin/jq -r .host <<< "$record")
port=$(/usr/bin/jq -r .port <<< "$record")
trusted=$(/usr/bin/jq -er --argjson rank "$rank" '.[$rank].host' /run/cluster/trusted-nodes.json) || fail 'invalid trusted mapping'
[[ $target == "$trusted" ]] || fail 'host differs from trusted mapping'
cluster_ssh_options
exec /usr/bin/ssh "${options[@]}" "${CLUSTER_SSH_ARGS[@]}" \
    -o "HostKeyAlias=cluster-rank-$rank" -p "$port" -- "root@$target" "$@"
