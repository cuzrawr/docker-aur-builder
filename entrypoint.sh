#!/usr/bin/env bash
# Initialize bind-mounted state after mounts have hidden the image's /cache.
set -euo pipefail

die() { printf '==> ERROR: %s\n' "$*" >&2; exit 1; }

[[ "${AUR_BUILDER_CONTAINER:-}" == 1 && ( -e /.dockerenv || -e /run/.containerenv ) ]] \
  || die "this helper must run inside the builder container; use ./run.sh"
(( EUID != 0 )) || die "builds must run as the non-root builder user"

export GNUPGHOME="${GNUPGHOME:-/cache/gnupg}"
for dir in /build /cache /cache/pacman /cache/yay /cache/logs \
  /cache/logs/makepkg /cache/locks /cache/makepkg /cache/makepkg/srcdest \
  /cache/ccache; do
  [[ ! -L "$dir" ]] || die "refusing symlinked state directory: $dir"
  mkdir -p -- "$dir" || die "cannot create $dir; check the bind mount's ownership"
  [[ -O "$dir" && -w "$dir" ]] \
    || die "$dir must be owned and writable by builder ($(id -u):$(id -g)); check host directory ownership"
done
[[ ! -L "$GNUPGHOME" ]] || die "refusing symlinked state directory: $GNUPGHOME"
(umask 077; mkdir -p -- "$GNUPGHOME") || die "cannot create $GNUPGHOME; check the bind mount's ownership"
[[ -O "$GNUPGHOME" && -w "$GNUPGHOME" ]] \
  || die "$GNUPGHOME must be owned and writable by builder ($(id -u):$(id -g))"
chmod 700 -- "$GNUPGHOME"
timeout --kill-after=5s 15s gpg --batch --list-keys >/dev/null \
  || die "cannot initialize the source-verification keyring at $GNUPGHOME"
timeout --kill-after=5s 15s gpgconf --launch dirmngr \
  || die "cannot start dirmngr using $GNUPGHOME"

[[ $# -gt 0 ]] || set -- bash -l
exec "$@"
