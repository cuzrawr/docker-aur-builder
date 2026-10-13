#!/usr/bin/env bash
# retry <max-attempts> <command...>
#
# pacman's mirrors (fastly/geo.mirror.pkgbuild.com) occasionally hiccup
# with a slow or reset connection -- retrying almost always recovers, so
# treat that as an expected, handled case rather than a hard failure.
set -uo pipefail

[[ $# -ge 2 && "$1" =~ ^[1-9][0-9]*$ ]] \
  || { echo "usage: retry <positive max-attempts> <command...>" >&2; exit 2; }
max="$1"
shift
n=1
while true; do
  if "$@"; then
    exit 0
  else
    status=$?
  fi
  # Cancellation and a missing command cannot be fixed by a network retry.
  case "$status" in 126|127|130|143) exit "$status" ;; esac
  if (( n >= max )); then
    echo "==> giving up after $n attempts: $*" >&2
    exit "$status"
  fi
  echo "==> attempt $n/$max failed, retrying in $((n * 5))s: $*" >&2
  sleep "$((n * 5))"
  n=$((n + 1))
done
