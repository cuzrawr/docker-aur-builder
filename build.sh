#!/usr/bin/env bash
# build <pkg> [pkg...] -- resolve and build AUR targets together, inside Docker.
set -euo pipefail
export LC_ALL=C

log() { printf '==> %s\n' "$*"; }
die() { printf '==> ERROR: %s\n' "$*" >&2; exit 1; }

[[ "${AUR_BUILDER_CONTAINER:-}" == 1 && ( -e /.dockerenv || -e /run/.containerenv ) ]] \
  || die "refusing to install packages on the host; use ./run.sh build <pkg> [pkg...]"
[[ $# -ge 1 ]] || { echo "usage: build <pkgname> [pkgname...]" >&2; exit 2; }
for pkg in "$@"; do
  [[ "$pkg" =~ ^[a-zA-Z0-9@_+][a-zA-Z0-9@._+-]*$ ]] \
    || die "invalid package name: $pkg"
done

export GNUPGHOME="${GNUPGHOME:-/cache/gnupg}"
aurbuilder-entrypoint true
CACHE=/cache
OUT=/build
YAY_FLAGS=(--aur --noconfirm --answerclean None --answerdiff None --answeredit None --answerupgrade None)

# Different packages still share pacman, yay and GnuPG state in this container.
[[ ! -L "$CACHE/locks/build.lock" ]] || die "refusing symlinked build lock"
exec {build_lock}>>"$CACHE/locks/build.lock"
flock -n "$build_lock" || die "another build is using this cache; wait for it to finish"
sudo -n pacman --version >/dev/null \
  || die "container sudo is unavailable; check its sudoers/privilege policy"

SYSTEM_LOG="$(mktemp "$CACHE/logs/system-$(date +%Y%m%d-%H%M%S)-XXXXXX.log")"
log "syncing the container (full upgrade; log: $SYSTEM_LOG)"
if ! retry 3 sudo -n pacman -Syu --noconfirm 2>&1 | tee "$SYSTEM_LOG"; then
  die "container upgrade failed; see $SYSTEM_LOG. Fix the reported network, mirror or keyring error before building"
fi

BUILD_LOG="$(mktemp "$CACHE/logs/${1}-$(date +%Y%m%d-%H%M%S)-XXXXXX.log")"
log "building $* (log: $BUILD_LOG)"

run_yay() {
  local -a status
  yay -S "${YAY_FLAGS[@]}" "$@" 2>&1 | tee -a "$BUILD_LOG"
  status=("${PIPESTATUS[@]}")
  (( status[1] == 0 )) || die "cannot write build log $BUILD_LOG"
  return "${status[0]}"
}

import_missing_keys() {
  local key server imported
  local -a keys=()
  # Both makepkg's error and yay's earlier key-import failure are recoverable.
  # Fetch only full fingerprints; never bypass signature verification or trust
  # a short key ID. Do not source PKGBUILDs just to discover their keys.
  mapfile -t keys < <(
    # Keep terminal color in the log, but remove SGR formatting for parsing.
    sed -E 's/\x1B\[[0-9;]*m//g' "$BUILD_LOG" | sed -nE \
      -e 's/.*unknown public key ([0-9A-Fa-f]{40}|[0-9A-Fa-f]{64})([^0-9A-Fa-f].*|$)/\1/p' \
      -e 's/^[[:space:]]*->[[:space:]]*([0-9A-Fa-f]{40}|[0-9A-Fa-f]{64}), required by:.*/\1/p' \
      | sort -u
  )
  (( ${#keys[@]} > 0 )) || return 1
  for key in "${keys[@]}"; do
    imported=0
    for server in hkps://keyserver.ubuntu.com hkps://keys.openpgp.org; do
      log "importing source key $key from $server" | tee -a "$BUILD_LOG"
      if timeout --kill-after=5s 45s gpg --batch --keyserver "$server" --recv-keys "$key" 2>&1 | tee -a "$BUILD_LOG"; then
        imported=1
        break
      fi
    done
    (( imported )) || return 1
  done
}

keys_retried=0
build_targets() {
  if run_yay "$@"; then
    return 0
  fi
  if grep -q 'did not pass the validity check' "$BUILD_LOG"; then
    die "source verification failed; checksums/signatures were not bypassed. See $BUILD_LOG"
  fi
  if (( keys_retried == 0 )) && grep -Eq 'unknown public key|problem importing keys' "$BUILD_LOG"; then
    keys_retried=1
    import_missing_keys \
      || die "could not import the required source key; see $BUILD_LOG. Use ./run.sh and gpg inside the container to diagnose"
    log "retrying after importing source keys" | tee -a "$BUILD_LOG"
    run_yay "$@" && return 0
  fi
  die "build failed; see $BUILD_LOG (use ./run.sh for an interactive container)"
}

artifacts=()
missing=()
collect_artifacts() {
  local pkg installed archive metadata found
  artifacts=()
  missing=()
  shopt -s nullglob
  for pkg in "$@"; do
    found=0
    if installed="$(pacman -Q -- "$pkg" 2>/dev/null)"; then
      for archive in "$OUT/$pkg-"*.pkg.tar "$OUT/$pkg-"*.pkg.tar.*; do
        [[ -f "$archive" && ! -L "$archive" && "$archive" != *.sig ]] || continue
        metadata="$(pacman -Qp -- "$archive" 2>/dev/null)" || continue
        if [[ "$metadata" == "$installed" ]]; then
          artifacts+=("$archive")
          found=1
        fi
      done
    fi
    (( found )) || missing+=("$pkg")
  done
  shopt -u nullglob
}

# One dependency graph and one yay invocation for the normal case. Existing
# target archives may be reused; unrelated files in /build are never installed.
build_targets --needed "$@"
collect_artifacts "$@"
if (( ${#missing[@]} > 0 )); then
  log "missing output for ${missing[*]}; rebuilding those targets once"
  # --needed would skip an installed target even if its output was deleted.
  build_targets --rebuild "${missing[@]}"
  collect_artifacts "$@"
fi
(( ${#missing[@]} == 0 )) \
  || die "no package archive matching the installed version for ${missing[*]}; see $BUILD_LOG"

log "done -- requested package(s) are in ./build/"
printf '%s\n' "${artifacts[@]}" | sort -u
