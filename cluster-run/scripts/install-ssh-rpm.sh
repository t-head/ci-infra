#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C
umask 077
shopt -s nullglob dotglob
CACHE=/mnt/wl_nas/devops/cache/openssh/rpm
WORK=
LOCK=
PUBLISH=
PUBLIC_STAGE=
SCHEMA=signed-openssh-rpm-v1

fail() { printf 'install-ssh-rpm: ERROR: %s\n' "$*" >&2; exit 1; }
warn() { printf 'install-ssh-rpm: WARNING: %s\n' "$*" >&2; }
remaining() {
  local now
  now=$(date +%s)
  [ "$now" -lt "$PPU_DEADLINE" ] || return 124
  printf '%s\n' "$((PPU_DEADLINE - now))"
}
bounded() {
  local seconds
  seconds=$(remaining) || { warn 'PPU_DEADLINE expired'; return 124; }
  timeout --signal=TERM --kill-after=1 "${seconds}s" "$@"
}
cleanup() {
  local status=$?
  trap - EXIT
  [ -z "$PUBLISH" ] || timeout --kill-after=1 2s rm -rf "$PUBLISH" || true
  [ -z "$PUBLIC_STAGE" ] || timeout --kill-after=1 2s rm -f "$PUBLIC_STAGE" || true
  [ -z "$LOCK" ] || timeout --kill-after=1 2s rmdir "$LOCK" 2>/dev/null || true
  [ -z "$WORK" ] || timeout --kill-after=1 2s rm -rf "$WORK" || true
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

[ "$#" = 0 ] || fail 'arguments are not supported'
[ "$(id -u)" = 0 ] || fail 'root with a writable root filesystem is required (no sudo/privileged mode)'
case "${PPU_DEADLINE:-}" in ''|*[!0-9]*) fail 'PPU_DEADLINE must be absolute epoch seconds' ;; esac
[ "${#PPU_DEADLINE}" -le 10 ] || fail 'invalid PPU_DEADLINE'
remaining >/dev/null || fail 'PPU_DEADLINE expired'
[ -r /etc/os-release ] || fail 'missing /etc/os-release'
# Only the local system os-release is sourced. NAS manifests are never executable.
. /etc/os-release
for tool in rpm rpmkeys timeout sha256sum awk sort cp mktemp; do
  command -v "$tool" >/dev/null || fail "required tool unavailable: $tool"
done
if command -v dnf >/dev/null; then PM=dnf
elif command -v yum >/dev/null; then PM=yum
else fail 'yum or dnf is required'; fi
ARCH=$(bounded rpm --eval '%{_arch}') || fail 'cannot determine RPM architecture'
REPO_IDS=(); REPO_URLS=(); REPO_KEYS=()
KEY_NAMES=(); KEY_URLS=(); KEY_HASHES=()
add_key() { KEY_NAMES+=("$1"); KEY_URLS+=("$2"); KEY_HASHES+=("$3"); }
add_repo() { REPO_IDS+=("$1"); REPO_URLS+=("$2"); REPO_KEYS+=("$3"); }
case "${ID:-}" in
  centos)
    stream=unknown
    case "${NAME:-} ${VERSION:-}" in
      *Linux*Stream*|*Stream*Linux*) fail 'contradictory CentOS Linux/Stream identity' ;;
      *Stream*) stream=1 ;;
      *Linux*) stream=0 ;;
    esac
    case "${VERSION_ID:-}/$stream/$ARCH" in
      7/0/x86_64|7.9.2009/0/x86_64)
        RELEASE=7.9.2009
        add_key RPM-GPG-KEY-CentOS-7 https://www.centos.org/keys/RPM-GPG-KEY-CentOS-7 \
          8b48b04b336bd725b9e611c441c65456a4168083c4febc28e88828d8ec14827f
        add_key RPM-GPG-KEY-EPEL-7 https://mirrors.aliyun.com/epel/RPM-GPG-KEY-EPEL-7 \
          028b9accc59bab1d21f2f3f544df5469910581e728a64fd8c411a725a82300c2
        for repo in os updates extras; do
          add_repo "centos-$repo" "https://mirrors.aliyun.com/centos-vault/$RELEASE/$repo/$ARCH" 0
        done
        add_repo epel 'https://mirrors.aliyun.com/epel-archive/7/'"$ARCH" 1
        ;;
      8/0/x86_64|8/0/aarch64|8.5.2111/0/x86_64|8.5.2111/0/aarch64)
        RELEASE=8.5.2111; CENTOS_BASE=https://mirrors.aliyun.com/centos-vault/8.5.2111
        ;;
      8/1/x86_64|8/1/aarch64)
        RELEASE=8-stream; CENTOS_BASE=https://mirrors.aliyun.com/centos-vault/8-stream
        ;;
      9/1/x86_64|9/1/aarch64)
        RELEASE=9-stream; CENTOS_BASE=https://mirrors.aliyun.com/centos-stream/9-stream
        ;;
      *) fail "unsupported CentOS version/variant/architecture: ${VERSION_ID:-unknown}/$stream/$ARCH" ;;
    esac
    if [ "${#REPO_IDS[@]}" = 0 ]; then
      add_key RPM-GPG-KEY-CentOS-Official https://www.centos.org/keys/RPM-GPG-KEY-CentOS-Official \
        146059788b214d7ba0dd70c1cf21111e594c6cfde201da8a9a88fe7101be8a78
      for repo in BaseOS AppStream; do add_repo "centos-$repo" "$CENTOS_BASE/$repo/$ARCH/os" 0; done
    fi
    ;;
  alinux)
    case "${VERSION_ID:-}/$ARCH" in
      2/x86_64|2/aarch64|2.1903/x86_64|2.1903/aarch64)
        RELEASE=2.1903
        add_key RPM-GPG-KEY-ALIYUN https://mirrors.aliyun.com/alinux/RPM-GPG-KEY-ALIYUN \
          0809a3c485ad9323901cbd01dc9dde09dc10da21dd57272dd8b0d124d1ee6395
        ;;
      3/x86_64|3/aarch64|3.2104/x86_64|3.2104/aarch64)
        RELEASE=3
        add_key RPM-GPG-KEY-ALINUX-3 https://mirrors.aliyun.com/alinux/3/RPM-GPG-KEY-ALINUX-3 \
          b8f79e10cf214927dbc88787e92069e5f032c292f8c3d93c4b9c69026ff90eab
        ;;
      *) fail "unsupported Alibaba Cloud Linux version/architecture: ${VERSION_ID:-unknown}/$ARCH" ;;
    esac
    for repo in os updates plus; do add_repo "alinux-$repo" "https://mirrors.aliyun.com/alinux/$RELEASE/$repo/$ARCH" 0; done
    if [ "$RELEASE" = 2.1903 ]; then
      add_key RPM-GPG-KEY-EPEL-7 https://mirrors.aliyun.com/epel/RPM-GPG-KEY-EPEL-7 \
        028b9accc59bab1d21f2f3f544df5469910581e728a64fd8c411a725a82300c2
      add_repo epel "https://mirrors.aliyun.com/epel-archive/7/$ARCH" 1
    fi
    ;;
  *) fail "unsupported OS: ${ID:-unknown}" ;;
esac
WORK=$(mktemp -d /tmp/ppu-ssh-rpm.XXXXXXXX) || fail 'cannot create local staging directory'
[ -w /etc ] && [ -w /usr/sbin ] || fail 'root filesystem is not writable'
RPMDB=$(bounded rpm --eval '%{_dbpath}') || fail 'cannot locate system RPM database'
case "$RPMDB" in /*) ;; *) fail 'RPM database path must be absolute' ;; esac
[ -d "$RPMDB" ] && [ -w "$RPMDB" ] || fail 'system RPM database is not writable'
for directory in /usr/sbin "$RPMDB"; do
  probe=$(bounded mktemp "$directory/.ppu-ssh-write.XXXXXXXX") || fail 'root filesystem is not writable'
  rm -f "$probe"
done
minimum_version() {
  # Compare upstream numbers, not lexicographic RPM release suffixes.
  printf '%s\n' "$1" | awk -v major="$2" -v minor="$3" '
    /^[0-9]+\.[0-9]+([.p][0-9][a-zA-Z0-9.]*)?$/ {
      split($0, v, "."); sub(/[^0-9].*$/, "", v[2]);
      if(v[1]+0 > major || (v[1]+0 == major && v[2]+0 >= minor)) ok=1
    } END {exit !ok}'
}
installed_target() {
  local version
  version=$(bounded rpm -q --qf '%{VERSION}\n' "$1" 2>/dev/null) || return 1
  case "$1" in jq) minimum_version "$version" 1 5 ;; *) minimum_version "$version" 7 4 ;; esac
}
compatible() {
  local server client
  installed_target openssh-server && installed_target openssh-clients && installed_target jq || return 1
  server=$(bounded rpm -q --qf '%{EPOCHNUM}|%{VERSION}|%{RELEASE}|%{ARCH}\n' openssh-server) || return 1
  client=$(bounded rpm -q --qf '%{EPOCHNUM}|%{VERSION}|%{RELEASE}|%{ARCH}\n' openssh-clients) || return 1
  [ "$server" = "$client" ] || return 1
  command -v sshd >/dev/null && command -v ssh >/dev/null && command -v ssh-keygen >/dev/null &&
    command -v jq >/dev/null && bounded ssh -V >/dev/null 2>&1 && bounded jq --version >/dev/null 2>&1
}
prepare_sshd() {
  local directory
  for directory in /var/empty /var/empty/sshd; do
    [ ! -L "$directory" ] || fail 'SSH privilege-separation directory must not be a symlink'
    if [ ! -e "$directory" ]; then
      (umask 022; bounded mkdir "$directory") || fail 'cannot create SSH privilege-separation directory'
    fi
    [ -d "$directory" ] || fail 'invalid SSH privilege-separation directory'
  done
  if ! bounded getent passwd sshd >/dev/null; then
    if bounded getent group sshd >/dev/null; then
      bounded useradd -r -M -g sshd -d /var/empty/sshd -s /sbin/nologin -c 'Privilege-separated SSH' sshd
    else
      bounded useradd -r -M -U -d /var/empty/sshd -s /sbin/nologin -c 'Privilege-separated SSH' sshd
    fi
  fi
}
if compatible; then
  prepare_sshd
  printf 'install-ssh-rpm: reusing matching OpenSSH >=7.4 and jq >=1.5 (no daemon launched)\n'
  exit 0
fi
# Freeze the COMPLETE installed package baseline once, BEFORE key imports/install.
bounded rpm -qa --qf '%{NAME}|%{EPOCHNUM}|%{VERSION}|%{RELEASE}|%{ARCH}\n' > "$WORK/base-unsorted" || fail 'cannot query RPM baseline'
bounded sort "$WORK/base-unsorted" > "$WORK/base-packages" || fail 'cannot sort RPM baseline'
{
  printf '%s\n' "$SCHEMA" "$ID" "$RELEASE" "$ARCH" 'weak-deps=0;obsoletes=0;noscripts;notriggers'
  for ((i=0; i<${#REPO_IDS[@]}; i++)); do
    printf '%s|%s|%s\n' "${REPO_IDS[$i]}" "${REPO_URLS[$i]}" "${KEY_HASHES[${REPO_KEYS[$i]}]}"
  done
  while IFS= read -r line; do printf '%s\n' "$line"; done < "$WORK/base-packages"
} > "$WORK/fingerprint"
FINGERPRINT=$(bounded sha256sum "$WORK/fingerprint") || fail 'cannot fingerprint RPM baseline'
FINGERPRINT=${FINGERPRINT%% *}
KEY="$CACHE/$ID/$RELEASE/$ARCH/$FINGERPRINT"

safe_name() { case "$1" in ''|.|..|*[!a-zA-Z0-9._+~-]*) return 1 ;; esac; }
# A cache ancestor symlink would bypass the job's fixed NAS boundary.
safe_directory() {
  local path=$1 component current= rest
  case "$path" in /*) ;; *) return 1 ;; esac
  rest=${path#/}
  while [ -n "$rest" ]; do
    component=${rest%%/*}
    case "$component" in ''|.|..) return 1 ;; esac
    current="$current/$component"
    [ ! -L "$current" ] || return 1
    if [ -e "$current" ]; then [ -d "$current" ] || return 1; fi
    case "$rest" in */*) rest=${rest#*/} ;; *) rest= ;; esac
  done
}
key_matches() {
  local hash
  [ -f "$1" ] && [ ! -L "$1" ] || return 1
  hash=$(bounded sha256sum "$1") || return 1
  [ "${hash%% *}" = "$2" ]
}
mkdir -p "$WORK/keys" "$WORK/rpmdb" "$WORK/repos" "$WORK/pm-cache" "$WORK/pm-state" "$WORK/empty"
for ((i=0; i<${#KEY_NAMES[@]}; i++)); do
  name=${KEY_NAMES[$i]}; hash=${KEY_HASHES[$i]}; dest="$WORK/keys/$name"
  keycache="$CACHE/publickeys/$hash.asc"
  found=0
  for source in "/etc/pki/rpm-gpg/$name" "$keycache"; do
    safe_directory "${source%/*}" || continue
    [ -f "$source" ] && [ ! -L "$source" ] || continue
    if bounded cp -P "$source" "$dest" && key_matches "$dest" "$hash"; then found=1; break; fi
    rm -f "$dest"
  done
  if [ "$found" = 0 ]; then
    command -v curl >/dev/null || fail 'curl with base CA trust is required to bootstrap pinned vendor public keys'
    # No insecure TLS, redirects, alternate mirrors or manifest-selected URLs.
    bounded curl --fail --silent --show-error --proto '=https' --tlsv1.2 \
      --retry 0 --output "$dest" "${KEY_URLS[$i]}" || fail "pinned vendor public key download failed: $name (base CA trust required)"
    key_matches "$dest" "$hash" || fail "vendor public key does not match fixed SHA256: $name"
  fi
  # Cache only fixed-hash PUBLIC keys; a writable NAS never defines trust roots.
  if safe_directory "$CACHE/publickeys" && bounded mkdir -p "$CACHE/publickeys" 2>/dev/null &&
      safe_directory "$CACHE/publickeys"; then
    PUBLIC_STAGE=$(bounded mktemp "$CACHE/publickeys/.key.XXXXXXXX") || PUBLIC_STAGE=
    if [ -n "$PUBLIC_STAGE" ]; then
      if ! bounded cp -P "$dest" "$PUBLIC_STAGE" || ! bounded mv -fT "$PUBLIC_STAGE" "$keycache"; then
        warn 'public key cache is not writable; continuing with verified local key'
      fi
      if bounded rm -f "$PUBLIC_STAGE"; then PUBLIC_STAGE=; fi
    else warn 'public key cache is not writable; continuing with verified local key'; fi
  else warn 'public key cache unavailable; continuing with verified local key'; fi
done
bounded rpm --dbpath "$WORK/rpmdb" --initdb || fail 'cannot create private signature verification database'
for ((i=0; i<${#KEY_NAMES[@]}; i++)); do
  bounded rpmkeys --dbpath "$WORK/rpmdb" --import "$WORK/keys/${KEY_NAMES[$i]}" || fail 'cannot import pinned key into private RPM database'
done
# RPM signatures define trust; repository metadata is not assumed signed.
{
  printf '[main]\nreposdir=%s\ncachedir=%s\npersistdir=%s\nlogfile=%s\nlogdir=%s\n' \
    "$WORK/repos" "$WORK/pm-cache" "$WORK/pm-state" "$WORK/yum.log" "$WORK"
  printf 'plugins=0\npluginpath=%s\npluginconfpath=%s\nvarsdir=%s\n' "$WORK/empty" "$WORK/empty" "$WORK/empty"
  printf 'gpgcheck=1\nlocalpkg_gpgcheck=1\nsslverify=1\nkeepcache=1\nretries=0\ntimeout=15\n'
  printf 'install_weak_deps=False\nobsoletes=0\n'
} > "$WORK/pm.conf"
for ((i=0; i<${#REPO_IDS[@]}; i++)); do
  printf '[%s]\nname=%s\nbaseurl=%s\nenabled=1\nsslverify=1\ngpgcheck=1\nlocalpkg_gpgcheck=1\nrepo_gpgcheck=0\ngpgkey=file://%s\n\n' \
    "${REPO_IDS[$i]}" "${REPO_IDS[$i]}" "${REPO_URLS[$i]}" "$WORK/keys/${KEY_NAMES[${REPO_KEYS[$i]}]}" >> "$WORK/repos/aliyun.repo"
done
pm_run() { bounded "$PM" -c "$WORK/pm.conf" --noplugins -y "$@"; }
signature_ok() {
  bounded rpmkeys --dbpath "$WORK/rpmdb" --checksig --verbose "$1" > "$WORK/signature" 2>&1 || return 1
  # Some RPM releases return zero for NOKEY or digest-only verification.
  awk '
    /NOKEY|NOTOK|NOT OK|NOTTRUSTED|BAD|UNSIGNED/ {bad=1}
    /[Ss]ignature/ {if($0 ~ /: OK[[:space:]]*$/) signed=1; else bad=1}
    END {exit bad || !signed}' "$WORK/signature"
}
verify_bundle() {
  local bundle=$1 tag file extra info name epoch version release architecture count=0 actual
  local target
  [ -d "$bundle" ] && [ ! -L "$bundle" ] || return 1
  [ -f "$bundle/COMPLETE" ] && [ ! -L "$bundle/COMPLETE" ] || return 1
  [ -f "$bundle/MANIFEST" ] && [ ! -L "$bundle/MANIFEST" ] || return 1
  [ "$(wc -c < "$bundle/COMPLETE")" -le 64 ] && [ "$(< "$bundle/COMPLETE")" = "$SCHEMA" ] || return 1
  [ "$(wc -c < "$bundle/MANIFEST")" -le 1048576 ] || return 1
  [ -d "$bundle/rpms" ] && [ ! -L "$bundle/rpms" ] || return 1
  for file in "$bundle"/*; do
    case "$file" in "$bundle/COMPLETE"|"$bundle/MANIFEST"|"$bundle/rpms") ;; *) return 1 ;; esac
  done
  rm -rf "$WORK/verified"
  mkdir -p "$WORK/verified"
  : > "$WORK/names"; : > "$WORK/provides"; : > "$WORK/requires"
  RPMS=()
  while IFS='|' read -r tag file extra || [ -n "$tag$file$extra" ]; do
    [ "$tag" = rpm ] && [ -z "$extra" ] && safe_name "$file" || return 1
    case "$file" in *.rpm) ;; *) return 1 ;; esac
    [ ! -e "$WORK/verified/$file" ] && [ ! -L "$WORK/verified/$file" ] || return 1
    [ -f "$bundle/rpms/$file" ] && [ ! -L "$bundle/rpms/$file" ] || return 1
    bounded cp -P "$bundle/rpms/$file" "$WORK/verified/$file" || return 1
    signature_ok "$WORK/verified/$file" || { warn "RPM signature rejected: $file"; return 1; }
    info=$(bounded rpm -qp --qf '%{NAME}|%{EPOCHNUM}|%{VERSION}|%{RELEASE}|%{ARCH}\n' "$WORK/verified/$file") || return 1
    case "$info" in *$'\n'*) return 1 ;; esac
    IFS='|' read -r name epoch version release architecture extra <<< "$info"
    safe_name "$name" && safe_name "$version" && safe_name "$release" && [ -z "$extra" ] || return 1
    case "$epoch" in ''|*[!0-9]*) return 1 ;; esac
    case "$architecture" in "$ARCH"|noarch) ;; *) return 1 ;; esac
    awk -F '|' -v n="$name" '$2 == n {bad=1} END {exit bad}' "$WORK/names" || return 1
    printf '%s|%s\n' "$file" "$name" >> "$WORK/names"
    bounded rpm -qp --provides "$WORK/verified/$file" > "$WORK/header-provides" || return 1
    bounded rpm -qp --requires "$WORK/verified/$file" > "$WORK/header-requires" || return 1
    bounded rpm -qp --list "$WORK/verified/$file" > "$WORK/header-files" || return 1
    # File-path dependencies are provided by the signed file list, not --provides.
    awk -v f="$file" 'NF {print f "|" $1}' "$WORK/header-provides" "$WORK/header-files" >> "$WORK/provides" || return 1
    awk -v f="$file" 'NF {print f "|" $0}' "$WORK/header-requires" >> "$WORK/requires" || return 1
    RPMS+=("$WORK/verified/$file")
    count=$((count + 1)); [ "$count" -le 2048 ] || return 1
  done < "$bundle/MANIFEST"
  [ "$count" -gt 0 ] || return 1
  actual=0
  for file in "$bundle/rpms"/*; do
    [ -f "$file" ] && [ ! -L "$file" ] || return 1
    actual=$((actual + 1))
  done
  [ "$actual" -eq "$count" ] || return 1
  for target in openssh-server openssh-clients jq; do
    if ! awk -F '|' -v t="$target" '$2 == t {found=1} END {exit !found}' "$WORK/names"; then
      installed_target "$target" || { warn "required RPM missing from bundle and image: $target"; return 1; }
    fi
  done
  # Reject unrelated signed packages; rpm --test checks exact dependency versions.
  bounded awk -F '|' '
    FILENAME == ARGV[1] {names[$1]=$2; if($2=="openssh-server" || $2=="openssh-clients" || $2=="jq") selected[$1]=1; next}
    FILENAME == ARGV[2] {p[++np]=$1; cap[np]=$2; next}
    FILENAME == ARGV[3] {r[++nr]=$1; req[nr]=$2; next}
    function contains(text, name, start, pos, before, after) {
      start=1
      while((pos=index(substr(text,start),name)) > 0) {
        pos+=start-1; before=pos==1 ? " " : substr(text,pos-1,1)
        after=substr(text,pos+length(name),1)
        if((before==" " || before=="(") && (after=="" || after==" " || after==")")) return 1
        start=pos+length(name)
      } return 0
    }
    END {
      changed=1
      while(changed) {changed=0; for(i=1;i<=nr;i++) if(selected[r[i]])
        for(j=1;j<=np;j++) if(!selected[p[j]] && contains(req[i],cap[j])) {selected[p[j]]=1; changed=1}}
      for(f in names) if(!selected[f]) {
        print "install-ssh-rpm: RPM outside requested dependency closure: " f > "/dev/stderr"
        exit 1
      }
    }' "$WORK/names" "$WORK/provides" "$WORK/requires" || return 1
  # No install, scripts, service actions, dependency downloads or downgrades.
  if ! bounded rpm -U --test --noscripts --notriggers "${RPMS[@]}" > "$WORK/transaction-test" 2>&1; then
    /bin/cat "$WORK/transaction-test" >&2
    return 1
  fi
}
make_bundle() {
  local bundle=$1 file base count=0
  mkdir -p "$bundle/rpms"
  : > "$bundle/MANIFEST"
  # YUM adds architecture/release directories; DNF uses one repository directory.
  for file in "$WORK/pm-cache"/packages/*.rpm "$WORK/pm-cache"/*/packages/*.rpm \
      "$WORK/pm-cache"/*/*/packages/*.rpm "$WORK/pm-cache"/*/*/*/packages/*.rpm; do
    safe_directory "${file%/*}" && [ ! -L "$file" ] || return 1
    base=${file##*/}; safe_name "$base" || return 1
    [ ! -e "$bundle/rpms/$base" ] || return 1
    bounded cp -P "$file" "$bundle/rpms/$base" || return 1
    printf 'rpm|%s\n' "$base" >> "$bundle/MANIFEST"
    count=$((count + 1))
  done
  [ "$count" -gt 0 ] || return 1
  printf '%s\n' "$SCHEMA" > "$bundle/COMPLETE"
}
cache_lookup() {
  local start now generation
  HIT=0
  safe_directory "$KEY" || { warn 'cache path contains a symlink/non-directory; using trusted local download'; return; }
  if bounded mkdir -p "$KEY" 2>/dev/null && safe_directory "$KEY"; then
    start=$(date +%s)
    while :; do
      safe_directory "$KEY" || break
      if bounded mkdir "$KEY/.lock" 2>/dev/null; then LOCK="$KEY/.lock"; break; fi
      now=$(date +%s)
      if [ "$((now - start))" -ge 15 ]; then warn 'cache lock busy; using local install without backfill'; break; fi
      bounded sleep 1 || fail 'deadline while waiting for cache lock'
    done
  else warn 'cache is not writable; local install will not be backfilled'; fi
  safe_directory "$KEY" || return 0
  for generation in "$KEY"/generation-*; do
    case "$generation" in "$KEY"/generation-*) ;; *) continue ;; esac
    safe_name "${generation##*/}" && safe_directory "$generation" || continue
    rm -rf "$WORK/snapshot"
    # Copy without dereferencing, then verify immutable local bytes only.
    if bounded cp -R -P "$generation" "$WORK/snapshot" && verify_bundle "$WORK/snapshot"; then
      HIT=1
      printf 'install-ssh-rpm: authenticated offline RPM cache hit\n'
      return 0
    fi
    warn "rejected incomplete, incompatible or unauthenticated RPM cache: ${generation##*/}"
  done
}
# Fresh offline images need the pinned vendor keys for package-manager checks.
for ((i=0; i<${#KEY_NAMES[@]}; i++)); do
  bounded rpmkeys --import "$WORK/keys/${KEY_NAMES[$i]}" || fail 'cannot import verified vendor key for yum/dnf checks'
done
cache_lookup
if [ "$HIT" = 0 ]; then
  # Transaction-test mode downloads dependencies without optional download plugins.
  pm_run --setopt=tsflags=test --setopt=keepcache=1 install openssh-server openssh-clients jq || fail 'Aliyun RPM dependency download failed (HTTPS/base CA trust required)'
  make_bundle "$WORK/download" || fail 'cannot capture the complete RPM dependency bundle'
  verify_bundle "$WORK/download" || fail 'downloaded RPM bundle failed pinned signatures/header closure/transaction verification'
fi
# Disable repositories so only the verified local dependency closure is installed.
pm_run --cacheonly '--disablerepo=*' --setopt=tsflags=test localinstall "${RPMS[@]}" || fail 'offline RPM dependency preflight failed'
# Skip package service actions; prepare only the required SSH account and loader cache.
pm_run --cacheonly '--disablerepo=*' --setopt=tsflags=noscripts,notriggers localinstall "${RPMS[@]}" || fail 'offline RPM dependency installation failed'
bounded /sbin/ldconfig || fail 'cannot refresh the installed library cache'
prepare_sshd
compatible || fail 'installed OpenSSH/jq versions or runtime tools are incompatible'
if [ "$HIT" = 0 ] && [ -n "$LOCK" ]; then
  if safe_directory "$KEY"; then PUBLISH=$(bounded mktemp -d "$KEY/.publish.XXXXXXXX") || PUBLISH=; fi
  if [ -n "$PUBLISH" ] && bounded cp -R -P "$WORK/download/." "$PUBLISH" &&
      safe_directory "$KEY" && bounded mv -T "$PUBLISH" "$KEY/generation-${PUBLISH##*.publish.}"; then
    PUBLISH=
    printf 'install-ssh-rpm: published authenticated RPM dependency cache\n'
  else warn 'trusted local installation succeeded, but cache backfill failed'; fi
fi
remaining >/dev/null || fail 'deadline before SSH runtime readiness'
printf 'install-ssh-rpm: OpenSSH server/clients and jq ready (no daemon launched)\n'
