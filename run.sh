#!/usr/bin/env bash
# ./run.sh build <pkg> [pkg...]  build one or more AUR packages; results land in ./build
# ./run.sh                   interactive shell in the build container (build, yay, makepkg all ready)
# ./run.sh clean [...]       wipe the cache -- see ./run.sh clean --help
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
IMAGE="${AUR_BUILDER_IMAGE:-aurbuilder}"
CACHE_DIR="$ROOT/cache"
BUILD_DIR="$ROOT/build"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

validate_package() {
  [[ "$1" =~ ^[a-zA-Z0-9@_+][a-zA-Z0-9@._+-]*$ ]] \
    || die "invalid package name: $1"
}

reject_symlink() {
  [[ ! -L "$1" ]] || die "refusing symlinked path: $1"
}

lock_workspace() {
  # Keep the lock outside /cache so clean cannot delete an active lock inode.
  reject_symlink "$ROOT/.aurbuilder.lock"
  exec {workspace_lock}>>"$ROOT/.aurbuilder.lock"
  flock -n "$workspace_lock" \
    || die "this workspace is in use by another build/shell/cleanup; close it first"
}

detect_engine() {
  if [[ -n "${CONTAINER_ENGINE:-}" ]]; then
    printf '%s' "$CONTAINER_ENGINE"; return 0
  elif command -v podman &>/dev/null; then
    printf podman; return 0
  elif command -v docker &>/dev/null; then
    printf docker; return 0
  fi
  return 1
}

prepare_engine() {
  ENGINE="$(detect_engine)" \
    || die "neither podman nor docker is installed (or set CONTAINER_ENGINE=docker|podman)"
  "$ENGINE" info >/dev/null \
    || die "cannot reach $ENGINE; start its service or check your engine permissions"
}

usage() {
  cat <<EOF
Usage:
  $(basename "$0") build <pkg> [pkg...]   build one or more AUR packages in one container
                                         (yay resolves dependencies for all targets together)
                                         results land in ./build/
  $(basename "$0")                        drop into a shell (build, yay, makepkg all work)
  $(basename "$0") clean [-y] [--image] [--pkg <name>]
                                          wipe ./cache; --image also drops the image;
                                          --pkg <name> clears cache for one package
  $(basename "$0") help                   show this message

CONTAINER_ENGINE=docker|podman forces which engine (default: podman if present, else docker).
AUR_BUILDER_IMAGE selects the image tag (default: aurbuilder).
Host pacman/makepkg configuration is copied on every launch.
EOF
}

ensure_dirs() {
  local dir
  for dir in \
    "$CACHE_DIR" \
    "$CACHE_DIR/pacman" \
    "$CACHE_DIR/yay" \
    "$CACHE_DIR/logs" \
    "$CACHE_DIR/logs/makepkg" \
    "$CACHE_DIR/locks" \
    "$CACHE_DIR/makepkg" \
    "$CACHE_DIR/makepkg/srcdest" \
    "$CACHE_DIR/ccache" \
    "$BUILD_DIR"; do
    reject_symlink "$dir"
    mkdir -p -- "$dir"
    [[ -O "$dir" && -w "$dir" ]] \
      || die "$dir must be owned and writable by your user ($(id -u):$(id -g))"
  done
  reject_symlink "$CACHE_DIR/gnupg"
  (umask 077; mkdir -p -- "$CACHE_DIR/gnupg")
  [[ -O "$CACHE_DIR/gnupg" && -w "$CACHE_DIR/gnupg" ]] \
    || die "$CACHE_DIR/gnupg must be owned and writable by your user"
  chmod 700 -- "$CACHE_DIR/gnupg"
}

sync_host_configs() {
  # Copied into the build context so the Dockerfile can layer your own
  # pacman.conf/makepkg.conf/mirrorlist into the image -- see the Dockerfile
  # for what gets forced (CacheDir, PKGDEST, PACMAN_AUTH, IgnorePkg strip)
  # versus left as yours (repos, mirrors, Color, CFLAGS, PACKAGER, MAKEFLAGS).
  local config fragment user_config
  for config in pacman.conf.host makepkg.conf.host mirrorlist.host makepkg.conf.user.host; do
    reject_symlink "$ROOT/$config"
  done
  if [[ -f /etc/pacman.conf ]]; then
    cp /etc/pacman.conf "$ROOT/pacman.conf.host"
  else
    : > "$ROOT/pacman.conf.host"
    echo "note: no /etc/pacman.conf on this host -- using the image's built-in default" >&2
  fi
  if [[ -f /etc/makepkg.conf ]]; then
    cp /etc/makepkg.conf "$ROOT/makepkg.conf.host"
  else
    : > "$ROOT/makepkg.conf.host"
    echo "note: no /etc/makepkg.conf on this host -- using the image's built-in default" >&2
  fi
  if [[ -f /etc/pacman.d/mirrorlist ]]; then
    cp /etc/pacman.d/mirrorlist "$ROOT/mirrorlist.host"
  else
    : > "$ROOT/mirrorlist.host"
    echo "note: no /etc/pacman.d/mirrorlist on this host -- using the image's built-in default" >&2
  fi

  # makepkg loads these after makepkg.conf. Leaving the image's copies here
  # can silently override host flags (notably RUSTFLAGS).
  reject_symlink "$ROOT/makepkg.conf.d.host"
  mkdir -p "$ROOT/makepkg.conf.d.host"
  shopt -s nullglob
  for fragment in "$ROOT/makepkg.conf.d.host/"*.conf; do
    reject_symlink "$fragment"
    rm -f -- "$fragment"
  done
  for fragment in /etc/makepkg.conf.d/*.conf; do
    cp -L -- "$fragment" "$ROOT/makepkg.conf.d.host/"
  done
  shopt -u nullglob

  # Preserve makepkg's normal user-config precedence as well.
  user_config="${XDG_CONFIG_HOME:-$HOME/.config}/pacman/makepkg.conf"
  [[ -f "$user_config" ]] || user_config="$HOME/.makepkg.conf"
  if [[ -f "$user_config" ]]; then
    cp -L -- "$user_config" "$ROOT/makepkg.conf.user.host"
  else
    : > "$ROOT/makepkg.conf.user.host"
  fi
}

ensure_image() {
  # Always (re)build. This is a no-op in well under a second when nothing
  # changed, courtesy of the engine's own layer cache -- and a full
  # from-scratch rebuild when the image/cache was purged, which is exactly
  # the self-healing this needs, for free, instead of a hand-rolled version.
  "$ENGINE" build \
    --build-arg UID="$(id -u)" \
    --build-arg GID="$(id -g)" \
    -t "$IMAGE" \
    "$ROOT"
}

run_container() {
  # Pacman uses the image's scoped sudoers; keep this override for engines
  # whose default no-new-privileges policy otherwise blocks container sudo.
  local -a run_args=(--rm --init --cap-drop=NET_RAW --pids-limit 4096 --security-opt no-new-privileges=false)
  [[ -t 0 ]] && run_args+=(-i)
  [[ -t 1 ]] && run_args+=(-t)
  [[ "${ENGINE##*/}" == podman ]] && run_args+=(--userns=keep-id)
  "$ENGINE" run "${run_args[@]}" \
    -v "$BUILD_DIR:/build" \
    -v "$CACHE_DIR:/cache" \
    "$IMAGE" "$@"
}

prepare_workspace() {
  (( EUID != 0 )) || die "run ./run.sh as your normal user, not with sudo"
  prepare_engine
  lock_workspace
  ensure_dirs
  sync_host_configs
  ensure_image
}

cmd_clean() {
  local wipe_image=0 only_pkg="" assume_yes=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --image) wipe_image=1 ;;
      --pkg)
        [[ $# -ge 2 ]] || die "--pkg needs a package name"
        validate_package "$2"
        only_pkg="$2"
        shift
        ;;
      -y|--yes) assume_yes=1 ;;
      -h|--help) usage; exit 0 ;;
      *) echo "clean: unknown option '$1'" >&2; exit 2 ;;
    esac
    shift
  done

  (( EUID != 0 )) || die "run ./run.sh as your normal user, not with sudo"
  lock_workspace
  reject_symlink "$CACHE_DIR"
  local target="$CACHE_DIR"
  if [[ -n "$only_pkg" ]]; then
    reject_symlink "$CACHE_DIR/yay"
    target="$CACHE_DIR/yay/$only_pkg"
    reject_symlink "$target"
  fi
  [[ $wipe_image -eq 0 ]] || prepare_engine

  if [[ ! -d "$target" ]]; then
    echo "nothing to clean at $target"
  else
    if [[ $assume_yes -eq 0 ]]; then
      read -r -p "Remove everything under $target ? [y/N] " reply || reply=""
      [[ "$reply" =~ ^[Yy]$ ]] || { echo "aborted"; exit 0; }
    fi
    find "$target" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
    echo "cleaned $target"
  fi

  if [[ $wipe_image -eq 1 ]]; then
    if "$ENGINE" image inspect "$IMAGE" >/dev/null 2>&1; then
      "$ENGINE" image rm "$IMAGE"
      echo "removed image '$IMAGE' -- the next run rebuilds it from scratch"
    else
      echo "image '$IMAGE' is already absent -- the next run rebuilds it"
    fi
  fi
}

case "${1:-}" in
  clean)
    shift
    cmd_clean "$@"
    ;;
  help|-h|--help)
    usage
    ;;
  build)
    shift
    [[ $# -ge 1 ]] || { usage; exit 2; }
    for pkg in "$@"; do validate_package "$pkg"; done
    prepare_workspace
    run_container build "$@"
    ;;
  "")
    prepare_workspace
    run_container
    ;;
  *)
    echo "unknown command: '$1'" >&2
    usage
    exit 2
    ;;
esac
