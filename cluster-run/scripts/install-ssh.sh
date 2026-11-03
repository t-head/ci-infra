#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C
umask 077
CACHE=/mnt/wl_nas/devops/cache/openssh
APT_HELPER=/usr/lib/apt/apt-helper
WORK=
LOCK=
PUBLISH=
POLICY_ACTIVE=0

fail() { printf 'install-ssh: ERROR: %s\n' "$*" >&2; exit 1; }
warn() { printf 'install-ssh: WARNING: %s\n' "$*" >&2; }
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
restore_policy() {
  if [ "$POLICY_ACTIVE" = 1 ] || { [ -n "$WORK" ] &&
      { [ -e "$WORK/policy-rc.d.original" ] || [ -L "$WORK/policy-rc.d.original" ]; }; }; then
    rm -f /usr/sbin/policy-rc.d || return 1
    if [ -e "$WORK/policy-rc.d.original" ] || [ -L "$WORK/policy-rc.d.original" ]; then
      mv "$WORK/policy-rc.d.original" /usr/sbin/policy-rc.d || return 1
    fi
    POLICY_ACTIVE=0
  fi
}
cleanup() {
  local status=$? keep_work=0
  trap - EXIT
  if ! restore_policy; then
    warn "could not restore policy-rc.d; original retained in $WORK"
    status=1; keep_work=1
  fi
  # Remove only our unpublished staging directory and our lock, never cache hits.
  [ -z "$PUBLISH" ] || timeout --kill-after=1 2s rm -rf "$PUBLISH" || true
  [ -z "$LOCK" ] || timeout --kill-after=1 2s rmdir "$LOCK" 2>/dev/null || true
  if [ -n "$WORK" ] && [ "$keep_work" = 0 ]; then rm -rf "$WORK"; fi
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
# os-release is a trusted local system file, never a NAS manifest.
. /etc/os-release
case "${ID:-}" in debian|ubuntu) ;; *) fail "unsupported OS: ${ID:-unknown}" ;; esac
case "${VERSION_ID:-}" in ''|*[!0-9.]*) fail 'unsupported OS version' ;; esac
for tool in timeout sha256sum dpkg dpkg-query dpkg-deb apt-get awk sort; do
  command -v "$tool" >/dev/null || fail "required tool unavailable: $tool"
done
WORK=$(mktemp -d /tmp/ppu-ssh-install.XXXXXXXX) || fail 'cannot create local staging directory'
[ -w /usr/sbin ] && [ -w /var/lib/dpkg ] && [ -w /etc ] || fail 'root filesystem is not writable'
# Check an actual write as well as mode bits (read-only mounts can have writable bits).
probe=$(mktemp /usr/sbin/.ppu-ssh-write.XXXXXXXX) || fail 'root filesystem is not writable'
rm -f "$probe"

installed_version() {
  dpkg-query -W -f='${Status}|${Version}\n' "$1" 2>/dev/null |
    awk -F '|' '$1 == "install ok installed" { print $2 }'
}
compatible() {
  local server client jq_version
  server=$(installed_version openssh-server) || return 1
  client=$(installed_version openssh-client) || return 1
  jq_version=$(installed_version jq) || return 1
  [ -n "$server" ] && [ "$server" = "$client" ] && [ -n "$jq_version" ] || return 1
  dpkg --compare-versions "$server" ge 1:7.6 || return 1
  dpkg --compare-versions "$jq_version" ge 1.5 || return 1
  command -v sshd >/dev/null && command -v ssh >/dev/null && command -v ssh-keygen >/dev/null &&
    command -v jq >/dev/null && ssh -V >/dev/null 2>&1 && jq --version >/dev/null 2>&1
}
if compatible; then
  printf 'install-ssh: reusing compatible openssh-server/client and jq\n'
  exit 0
fi
command -v gpgv >/dev/null || fail 'gpgv is required for the signed cache verification chain'
[ -x "$APT_HELPER" ] || fail 'apt-helper cat-file support is required; offline verification unavailable'
KEYRING=/usr/share/keyrings/${ID}-archive-keyring.gpg
[ -s "$KEYRING" ] || fail "existing distro archive keyring unavailable: $KEYRING"
cp "$KEYRING" "$WORK/distro-keyring.gpg"
mkdir -p "$WORK/etc/sources.list.d" "$WORK/etc/apt.conf.d" "$WORK/lists/partial" \
  "$WORK/archives/partial" "$WORK/gnupg"
[ ! -f /etc/apt/sources.list ] || cp /etc/apt/sources.list "$WORK/etc/sources.list"
for source in /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
  [ -f "$source" ] || continue
  cp "$source" "$WORK/etc/sources.list.d/"
done
# Ignore global apt.conf (including hooks/insecure overrides). Conservatively
# reject trust/expiry overrides, including multiline Deb822 values, rather than
# trying to interpret every APT spelling. Commented entries are harmless.
files=( )
for source in "$WORK/etc/sources.list" "$WORK/etc/sources.list.d/"*.list "$WORK/etc/sources.list.d/"*.sources; do
  [ ! -f "$source" ] || files[${#files[@]}]="$source"
done
[ "${#files[@]}" -gt 0 ] || fail 'no existing APT sources'
awk '
  /^[[:space:]]*#/ { next }
  { s=tolower($0); if (s ~ /(^|[[:space:]\[])trusted[[:space:]:=]/ ||
       s ~ /allow-(insecure|weak|downgrade-to-insecure|unauthenticated)/ ||
       s ~ /allowunauthenticated/ || s ~ /check-(valid-until|date)/) bad=1 }
  END { exit bad }
' "${files[@]}" || fail 'insecure APT source options are not accepted'
cat > "$WORK/apt.conf" <<EOF
Dir::Etc "$WORK/etc";
Dir::Etc::main "/dev/null";
Dir::Etc::parts "apt.conf.d";
Dir::Etc::trusted "$WORK/distro-keyring.gpg";
Dir::Etc::trustedparts "apt.conf.d";
Dir::State::lists "$WORK/lists";
Dir::Cache::archives "$WORK/archives";
Dir::Cache::pkgcache "";
Dir::Cache::srcpkgcache "";
APT::Install-Recommends "false";
APT::Install-Suggests "false";
APT::Get::AllowUnauthenticated "false";
Acquire::AllowInsecureRepositories "false";
Acquire::AllowWeakRepositories "false";
Acquire::AllowDowngradeToInsecureRepositories "false";
Acquire::Check-Valid-Until "true";
Acquire::Check-Date "true";
Acquire::Retries "0";
APT::Get::List-Cleanup "false";
// Private 0700 staging is intentionally inaccessible to _apt, not world-readable.
APT::Sandbox::User "root";
EOF
export APT_CONFIG="$WORK/apt.conf"
ARCH=$(dpkg --print-architecture)
case "$ARCH" in ''|*[!a-z0-9-]*) fail 'invalid dpkg architecture' ;; esac
# Capture the source/keyring AND complete base package state BEFORE installation.
# The manifest cannot select its cache key, nor introduce another trust root.
{
  printf '%s\n' 'signed-openssh-cache-v1' "$ID" "$VERSION_ID" "$ARCH"
  for source in "$WORK/etc/sources.list" "$WORK/etc/sources.list.d/"*.list "$WORK/etc/sources.list.d/"*.sources; do
    [ -f "$source" ] || continue
    printf '%s\n' "${source#"$WORK/etc/"}"
    sha256sum "$source" | awk '{print $1}'
  done
  sha256sum "$WORK/distro-keyring.gpg" | awk '{print $1}'
  dpkg-query -W -f='${binary:Package}|${Version}|${Architecture}|${Status}\n' | sort
} > "$WORK/fingerprint"
FINGERPRINT=$(sha256sum "$WORK/fingerprint" | awk '{print $1}')
KEY="$CACHE/$ID/$VERSION_ID/$ARCH/$FINGERPRINT"

apt_run() {
  local seconds
  seconds=$(remaining) || return 124
  bounded apt-get -o "DPkg::Lock::Timeout=$seconds" -o APT::Get::allow-Downgrades=false "$@"
}
safe_name() { case "$1" in ''|.|..|*[!a-zA-Z0-9._%+:~-]*) return 1 ;; esac; }
safe_relative() {
  case "$1" in ''|/*|*'//'*|*[!a-zA-Z0-9._%+:~/-]*) return 1 ;; esac
  case "/$1/" in */../*|*/./*) return 1 ;; esac
}
hash_matches() {
  local hash size file=$1 expected=$2 expected_size=$3
  case "$expected" in *[!0-9a-f]*|'') return 1 ;; esac
  [ "${#expected}" = 64 ] || return 1
  case "$expected_size" in ''|*[!0-9]*) return 1 ;; esac
  hash=$(bounded sha256sum "$file") || return 1
  size=$(wc -c < "$file")
  [ "${hash%% *}" = "$expected" ] && [ "$size" -eq "$expected_size" ]
}
# Verify a local snapshot, never files read repeatedly from writable NAS.
# MANIFEST is data only: index|id|kind|release-basename|index-basename|MetaKey,
# deb|basename. All hashes come from signed metadata, not this manifest.
verify_bundle() {
  local bundle=$1 tag ident kind release index metakey extra base suffix prefix line hash size filename
  local count=0 deb_count=0 package version architecture fields record found
  local version_match arch_match path
  [ -d "$bundle" ] && [ ! -L "$bundle" ] || return 1
  [ -f "$bundle/COMPLETE" ] && [ -f "$bundle/MANIFEST" ] || return 1
  [ "$(wc -c < "$bundle/COMPLETE")" -le 64 ] || return 1
  [ "$(< "$bundle/COMPLETE")" = signed-openssh-cache-v1 ] || return 1
  [ "$(wc -c < "$bundle/MANIFEST")" -le 1048576 ] || return 1
  [ -z "$(find "$bundle" ! -type f ! -type d -print -quit)" ] || return 1
  mkdir -p "$WORK/verified"
  : > "$WORK/records"
  : > "$WORK/seen"
  while IFS='|' read -r tag ident kind release index metakey extra; do
    [ -z "$extra" ] || return 1
    case "$tag" in
      index)
        count=$((count + 1)); [ "$count" -le 512 ] || return 1
        case "$ident" in ''|*[!0-9]*) return 1 ;; esac
        safe_name "$release" && safe_name "$index" && safe_relative "$metakey" || return 1
        case "$metakey" in */binary-"$ARCH"/Packages|*/binary-all/Packages) ;; *) return 1 ;; esac
        base=$index
        case "$base" in *.lz4|*.gz|*.xz|*.bz2|*.zst) base=${base%.*} ;; esac
        suffix=${metakey//\//_}
        case "$base" in *_"$suffix") prefix=${base%"$suffix"} ;; *) return 1 ;; esac
        awk -F '|' -v b="$base" -v i="$ident" '$1 == b || $3 == i {bad=1} END {exit bad}' \
          "$WORK/seen" || return 1
        case "$kind" in
          inrelease)
            [ "$release" = "${prefix}InRelease" ] || return 1
            bounded gpgv --homedir "$WORK/gnupg" --keyring "$WORK/distro-keyring.gpg" \
              --output "$WORK/verified/$ident.release" "$bundle/metadata/$release" || return 1
            ;;
          detached)
            [ "$release" = "${prefix}Release" ] || return 1
            bounded gpgv --homedir "$WORK/gnupg" --keyring "$WORK/distro-keyring.gpg" \
              "$bundle/metadata/$release.gpg" "$bundle/metadata/$release" || return 1
            cp "$bundle/metadata/$release" "$WORK/verified/$ident.release" || return 1
            ;;
          *) return 1 ;;
        esac
        # Reject expired/future signed metadata just as online APT does.
        local timestamp epoch now
        now=$(date +%s)
        timestamp=$(awk '/^Valid-Until: / {sub(/^Valid-Until: /, ""); print}' "$WORK/verified/$ident.release")
        if [ -n "$timestamp" ]; then
          epoch=$(date -u -d "$timestamp" +%s) || return 1
          [ "$epoch" -ge "$now" ] || return 1
        fi
        timestamp=$(awk '/^Date: / {sub(/^Date: /, ""); print}' "$WORK/verified/$ident.release")
        [ -n "$timestamp" ] || return 1
        epoch=$(date -u -d "$timestamp" +%s) || return 1
        [ "$epoch" -le "$((now + 10))" ] || return 1
        line=$(awk -v name="$metakey" '
          /^SHA256:$/ {inside=1; next}
          /^[^ ]/ {inside=0}
          inside && $3 == name {n++; value=$1 " " $2}
          END {if(n == 1) print value; else exit 1}
        ' "$WORK/verified/$ident.release") || return 1
        read -r hash size extra <<< "$line"
        bounded "$APT_HELPER" cat-file "$bundle/metadata/$index" > "$WORK/verified/$ident.packages" || return 1
        hash_matches "$WORK/verified/$ident.packages" "$hash" "$size" || return 1
        # Only authenticated/decompressed Packages may become local APT indexes.
        cp "$WORK/verified/$ident.packages" "$WORK/lists/$base" || return 1
        cp "$bundle/metadata/$release" "$WORK/lists/$release" || return 1
        if [ "$kind" = detached ]; then
          cp "$bundle/metadata/$release.gpg" "$WORK/lists/$release.gpg" || return 1
        fi
        printf '%s|%s|%s\n' "$base" "$metakey" "$ident" >> "$WORK/seen" || return 1
        awk '
          BEGIN {RS=""; FS="\n"}
          {p=v=a=f=h=s=""; for(i=1;i<=NF;i++) {
            if($i ~ /^Package: /) p=substr($i,10)
            if($i ~ /^Version: /) v=substr($i,10)
            if($i ~ /^Architecture: /) a=substr($i,15)
            if($i ~ /^Filename: /) f=substr($i,11)
            if($i ~ /^SHA256: /) h=substr($i,9)
            if($i ~ /^Size: /) s=substr($i,7)
          } if(p && v && a && f && h && s) print p "|" v "|" a "|" f "|" h "|" s}
        ' "$WORK/verified/$ident.packages" >> "$WORK/records" || return 1
        ;;
      deb)
        [ -z "$kind$release$index$metakey" ] && safe_name "$ident" || return 1
        case "$ident" in *.deb) ;; *) return 1 ;; esac
        deb_count=$((deb_count + 1)); [ "$deb_count" -le 512 ] || return 1
        ;;
      *) return 1 ;;
    esac
  done < "$bundle/MANIFEST"
  [ "$count" -gt 0 ] && [ "$deb_count" -gt 0 ] || return 1
  # Bind list names/MetaKeys and signed Suite/Codename to the EXISTING sources.
  # indextargets reads local state; it does not perform network requests.
  apt_run indextargets --format '$(FILENAME)|$(METAKEY)|$(RELEASE)' > "$WORK/targets" || return 1
  while IFS='|' read -r base metakey ident; do
    line=$(awk -F '|' -v b="$base" -v m="$metakey" '
      {n=$1; sub(/^.*\//,"",n); sub(/\.(lz4|gz|xz|bz2|zst)$/, "",n)}
      n == b && $2 == m {print $3; found=1; exit} END {if(!found) exit 1}
    ' "$WORK/targets") || return 1
    [ -n "$line" ] || return 1
    awk -v suite="$line" '$1 == "Suite:" || $1 == "Codename:" {if($2 == suite) ok=1} END {exit !ok}' \
      "$WORK/verified/$ident.release" || return 1
  done < "$WORK/seen"
  while IFS='|' read -r tag filename extra; do
    [ "$tag" = deb ] || continue
    fields=$(bounded dpkg-deb -f "$bundle/debs/$filename" Package Version Architecture) || return 1
    package=$(printf '%s\n' "$fields" | awk '$1 == "Package:" {print $2}')
    version=$(printf '%s\n' "$fields" | awk '$1 == "Version:" {print $2}')
    architecture=$(printf '%s\n' "$fields" | awk '$1 == "Architecture:" {print $2}')
    case "$architecture" in "$ARCH"|all) ;; *) return 1 ;; esac
    found=0
    # Filter large distro indexes in awk, not millions of Bash read iterations.
    awk -F '|' -v p="$package" -v v="$version" -v a="$architecture" \
      '$1 == p && $2 == v && $3 == a {print}' "$WORK/records" > "$WORK/matches" || return 1
    while IFS='|' read -r record version_match arch_match path hash size; do
      safe_relative "$path" || return 1
      if hash_matches "$bundle/debs/$filename" "$hash" "$size"; then found=1; break; fi
    done < "$WORK/matches"
    [ "$found" = 1 ] || return 1
    cp "$bundle/debs/$filename" "$WORK/archives/$filename" || return 1
  done < "$bundle/MANIFEST"
  # Resolve the requested packages and their entire closure, before touching dpkg.
  # Missing archives fail here without network or partial system installation.
  apt_run --yes --no-remove --no-download --download-only install openssh-server openssh-client jq
}
make_bundle() {
  local bundle=$1 filename metakey unused base suffix prefix release kind n=0 debs=0
  mkdir -p "$bundle/metadata" "$bundle/debs"
  : > "$bundle/MANIFEST"
  apt_run indextargets --format '$(FILENAME)|$(METAKEY)' > "$WORK/download-targets" || return 1
  while IFS='|' read -r filename metakey unused; do
    case "$metakey" in */binary-"$ARCH"/Packages|*/binary-all/Packages) ;; *) continue ;; esac
    [ -f "$filename" ] || continue
    case "$filename" in "$WORK/lists/"*) ;; *) return 1 ;; esac
    base=${filename##*/}; suffix=${metakey//\//_}; prefix=$base
    case "$prefix" in *.lz4|*.gz|*.xz|*.bz2|*.zst) prefix=${prefix%.*} ;; esac
    case "$prefix" in *_"$suffix") prefix=${prefix%"$suffix"} ;; *) return 1 ;; esac
    rm -f "$WORK/probe.release"
    if [ -f "$WORK/lists/${prefix}InRelease" ]; then
      release=${prefix}InRelease; kind=inrelease
      if ! bounded gpgv --homedir "$WORK/gnupg" --keyring "$WORK/distro-keyring.gpg" \
          --output "$WORK/probe.release" "$WORK/lists/$release"; then
        warn 'ignoring auxiliary index not signed by the distro archive keyring'
        continue
      fi
    else
      release=${prefix}Release; kind=detached
      if ! bounded gpgv --homedir "$WORK/gnupg" --keyring "$WORK/distro-keyring.gpg" \
          "$WORK/lists/$release.gpg" "$WORK/lists/$release"; then
        warn 'ignoring auxiliary index not signed by the distro archive keyring'
        continue
      fi
      cp "$WORK/lists/$release.gpg" "$bundle/metadata/$release.gpg" || return 1
    fi
    # Auxiliary/vendor indexes are omitted, but EVERY downloaded deb below must
    # still match a signed distro Packages stanza in verify_bundle.
    cp "$WORK/lists/$release" "$bundle/metadata/$release" || return 1
    cp "$filename" "$bundle/metadata/$base" || return 1
    n=$((n + 1))
    printf 'index|%s|%s|%s|%s|%s\n' "$n" "$kind" "$release" "$base" "$metakey" >> "$bundle/MANIFEST"
  done < "$WORK/download-targets"
  for filename in "$WORK/archives/"*.deb; do
    [ -f "$filename" ] || continue
    cp "$filename" "$bundle/debs/" || return 1
    printf 'deb|%s\n' "${filename##*/}" >> "$bundle/MANIFEST"
    debs=$((debs + 1))
  done
  [ "$n" -gt 0 ] && [ "$debs" -gt 0 ] || return 1
  printf 'signed-openssh-cache-v1\n' > "$bundle/COMPLETE"
}

# Bounded NAS lock. A busy/unwritable NAS never prevents a trusted local install.
if bounded mkdir -p "$KEY" 2>/dev/null; then
  lock_start=$(date +%s)
  while :; do
    if bounded mkdir "$KEY/.lock" 2>/dev/null; then LOCK="$KEY/.lock"; break; fi
    now=$(date +%s)
    if [ "$((now - lock_start))" -ge 15 ]; then warn 'cache lock busy; using local APT without backfill'; break; fi
    remaining >/dev/null || fail 'deadline while waiting for cache lock'
    bounded sleep 1 || fail 'deadline while waiting for cache lock'
  done
else
  warn 'cache is not writable; trusted local install will not be backfilled'
fi
HIT=0
# Read immutable COMPLETE generations even if NAS is read-only or its lock is busy.
if ! bounded find "$KEY" -mindepth 1 -maxdepth 1 -type d -name 'generation-*' \
    -print > "$WORK/generations" 2>/dev/null; then
  warn 'cache discovery unavailable; using trusted APT'
fi
while IFS= read -r generation; do
  case "$generation" in "$KEY"/generation-*) ;; *) continue ;; esac
  safe_name "${generation##*/}" || continue
  rm -rf "$WORK/snapshot" "$WORK/lists" "$WORK/archives" "$WORK/verified"
  mkdir -p "$WORK/lists/partial" "$WORK/archives/partial"
  # Copy the directory itself without following a replaced top-level symlink.
  if bounded cp -R -P "$generation" "$WORK/snapshot" && verify_bundle "$WORK/snapshot"; then
    HIT=1
    printf 'install-ssh: authenticated offline cache hit\n'
    break
  fi
  warn "rejected incomplete, incompatible or unauthenticated cache: ${generation##*/}"
done < "$WORK/generations"
if [ "$HIT" = 0 ]; then
  remaining >/dev/null || fail 'deadline before trusted APT download'
  rm -rf "$WORK/lists" "$WORK/archives" "$WORK/verified" "$WORK/snapshot"
  mkdir -p "$WORK/lists/partial" "$WORK/archives/partial"
  apt_run -o APT::Update::Error-Mode=any update || fail 'signed APT update failed'
  apt_run --yes --no-remove --download-only install openssh-server openssh-client jq || fail 'APT dependency download failed'
  make_bundle "$WORK/download" || fail 'cannot preserve signed APT metadata/dependency bundle for verification'
  # Apply precisely the same verification chain to downloads and hits.
  rm -rf "$WORK/lists" "$WORK/archives"
  mkdir -p "$WORK/lists/partial" "$WORK/archives/partial"
  verify_bundle "$WORK/download" || fail 'downloaded bundle failed distro signature/Packages/deb verification'
fi

# Do not start/stop an existing sshd; preserve the exact original policy (even a symlink).
if [ -e /usr/sbin/policy-rc.d ] || [ -L /usr/sbin/policy-rc.d ]; then
  mv /usr/sbin/policy-rc.d "$WORK/policy-rc.d.original"
fi
POLICY_ACTIVE=1
printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
chmod 755 /usr/sbin/policy-rc.d
export DEBIAN_FRONTEND=noninteractive
apt_run --yes --no-remove --no-download -o Dpkg::Options::=--force-confold \
  install openssh-server openssh-client jq || fail 'offline dependency installation failed'
restore_policy || fail 'cannot restore original policy-rc.d'
compatible || fail 'installed OpenSSH/jq versions or runtime tools are incompatible'
if [ "$HIT" = 0 ] && [ -n "$LOCK" ]; then
  # Full local verification and successful installation precede atomic publication.
  # Never overwrite a generation, and never recompute the key after dpkg changes.
  PUBLISH=$(bounded mktemp -d "$KEY/.publish.XXXXXXXX") || PUBLISH=
  if [ -n "$PUBLISH" ] && bounded cp -R -P "$WORK/download/." "$PUBLISH" &&
      bounded mv "$PUBLISH" "$KEY/generation-${PUBLISH##*.publish.}"; then
    PUBLISH=
    printf 'install-ssh: published authenticated dependency cache\n'
  else
    warn 'trusted local installation succeeded, but cache backfill failed'
  fi
fi
remaining >/dev/null || fail 'deadline before SSH runtime readiness'
printf 'install-ssh: OpenSSH server/client and jq ready (no daemon launched)\n'
