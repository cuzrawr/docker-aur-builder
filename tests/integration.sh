#!/usr/bin/env bash
# Real, networked tests. Every writable mount is a new disposable project copy.
set -euo pipefail
[[ $# -ge 1 ]] || { printf 'Usage: %s <aur-package> [aur-package...]\n' "$0" >&2; exit 2; }
for pkg in "$@"; do
  [[ "$pkg" =~ ^[a-zA-Z0-9@_+][a-zA-Z0-9@._+-]*$ ]] \
    || { printf 'ERROR: invalid package name: %s\n' "$pkg" >&2; exit 2; }
done
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
ENGINE="${CONTAINER_ENGINE:-docker}"
TEST_DIR="$ROOT/$(basename "$ROOT")tmp"
mkdir -p "$TEST_DIR"
WORK="$(mktemp -d "$TEST_DIR/integration-XXXXXX")"
export CONTAINER_ENGINE="$ENGINE"
AUR_BUILDER_IMAGE="aurbuilder-$(basename "$WORK" | tr '[:upper:]' '[:lower:]')"
export AUR_BUILDER_IMAGE
trap 'printf "Integration logs and artifacts: %s\n" "$WORK"' EXIT
cp "$ROOT"/{Dockerfile,run.sh,build.sh,entrypoint.sh,retry.sh,makepkg-container.conf,yay-config.json,.dockerignore} "$WORK/"
mkdir -p "$WORK/logs"
"$WORK/run.sh" build "$@" 2>&1 | tee "$WORK/logs/fresh.log"

# Exercise repeated builds within one container, where --needed can skip targets.
run_args=(--rm --init --cap-drop=NET_RAW --pids-limit 4096 --security-opt no-new-privileges=false)
[[ "${ENGINE##*/}" != podman ]] || run_args+=(--userns=keep-id)
# Expansion of the following script belongs to the container shell.
# shellcheck disable=SC2016
"$ENGINE" run "${run_args[@]}" -v "$WORK/cache:/cache" -v "$WORK/build:/build" \
  "$AUR_BUILDER_IMAGE" bash -euo pipefail -c '
    build "$@"
    pacman -Q -- "$@"
    shopt -s nullglob
    archives=()
    installed="$(pacman -Q -- "$1")"
    for archive in /build/"$1"-*.pkg.tar /build/"$1"-*.pkg.tar.*; do
      [[ "$archive" != *.sig ]] || continue
      [[ "$(pacman -Qp -- "$archive")" == "$installed" ]] || continue
      bsdtar -xOf "$archive" .PKGINFO >/dev/null
      archives+=("$archive")
    done
    (( ${#archives[@]} > 0 ))
    before="$(sha256sum "${archives[@]}")"
    build "$1"
    [[ "$(sha256sum "${archives[@]}")" == "$before" ]]
    rm -- "${archives[@]}"
    build "$1" | tee /cache/missing-output.log
    grep -Fq "missing output for $1; rebuilding" /cache/missing-output.log
  ' integration "$@" 2>&1 | tee "$WORK/logs/reuse.log"

cp "$WORK/cache/missing-output.log" "$WORK/logs/"
mkdir -p "$WORK/cache/yay/"{aurbuilder-clean-probe,aurbuilder-retain-probe}
touch "$WORK/cache/yay/"{aurbuilder-clean-probe,aurbuilder-retain-probe}/keep
"$WORK/run.sh" clean -y --pkg aurbuilder-clean-probe
[[ ! -e "$WORK/cache/yay/aurbuilder-clean-probe/keep" && -f "$WORK/cache/yay/aurbuilder-retain-probe/keep" ]]
[[ -f "$WORK/cache/gnupg/pubring.kbx" ]]
"$WORK/run.sh" clean -y --image
[[ -z "$(find "$WORK/cache" -mindepth 1 -print -quit)" ]]
[[ -n "$(find "$WORK/build" -name '*.pkg.tar*' -print -quit)" ]]
"$WORK/run.sh" </dev/null > "$WORK/logs/recreated.log" 2>&1
[[ -f "$WORK/cache/gnupg/pubring.kbx" ]]
[[ "$(stat -c %a "$WORK/cache/gnupg")" == 700 ]]
"$ENGINE" image inspect "$AUR_BUILDER_IMAGE" --format '{{.Id}}' > "$WORK/logs/image-id.txt"
"$ENGINE" image rm "$AUR_BUILDER_IMAGE" >/dev/null
printf 'PASS: selected package builds, package metadata, cached reuse, missing output, cleanup, image recreation\n'
