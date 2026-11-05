#!/bin/bash
# argv-preserving remote execution; cluster-ssh remains the identity boundary.
if [[ ${1:-} == -- ]]; then shift; fi
if (( $# < 2 )); then
    printf 'usage: cluster-exec [--] host program [argument ...]\n' >&2
    exit 64
fi
host=$1
shift
if [[ ${1:-} == -- ]]; then shift; fi
if (( $# == 0 )); then
    printf 'cluster-exec: missing program\n' >&2
    exit 64
fi
command=
for argument in "$@"; do
    quoted=${argument//\'/\'\\\'\'}
    command="$command'$quoted' "
done
exec /bin/bash /run/cluster/cluster-ssh.sh -- "$host" "$command"
