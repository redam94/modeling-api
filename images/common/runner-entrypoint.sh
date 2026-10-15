#!/bin/sh
# Entrypoint of the runner images (base-bayes, base-ml, base-forecast, worker).
#
# The compile caches (PyTensor compiledir, Numba cache) are baked read-only into
# $MAPI_CACHE_SEED at build time. PyTensor needs a *writable* compiledir (it takes a lock
# file there and adds new modules), so at start-up we build a symlink farm of the seed under
# $MAPI_CACHE_DIR (default /tmp/mapi-cache): directories are real, files are symlinks to
# the baked objects. That costs a few KiB of RAM (Cloud Run's filesystem lives in memory),
# works with a read-only root filesystem as long as /tmp is writable, and PyTensor accepts
# relocated entries (same leaf directory names / same realpath).
#
# Then it execs the given command (default: mapi-runner) so the runner's supervisor is the
# direct child of tini (PID 1) and receives SIGTERM itself.
set -eu

seed="${MAPI_CACHE_SEED:-/opt/mapi/cache}"
cache="${MAPI_CACHE_DIR:-/tmp/mapi-cache}"
home="${HOME:-/tmp/mapi-home}"

mkdir -p "$home" 2>/dev/null || true

if [ ! -e "$cache/.seeded" ]; then
  if mkdir -p "$cache" 2>/dev/null; then
    if [ -d "$seed" ] && cp -as "$seed/." "$cache/" 2>/dev/null; then
      : > "$cache/.seeded"
    else
      echo "mapi-entrypoint: compile cache seed unavailable; PyTensor/Numba will compile from scratch" >&2
    fi
  else
    echo "mapi-entrypoint: $cache is not writable; mount a tmpfs on /tmp (read-only rootfs?)" >&2
  fi
fi

# `docker run IMAGE` runs the runner; `docker run IMAGE --flag ...` passes flags to it;
# `docker run IMAGE some-command ...` runs that command (e.g. `mapi-server worker`).
if [ "$#" -eq 0 ]; then
  set -- mapi-runner
else
  case "$1" in
    -*) set -- mapi-runner "$@" ;;
  esac
fi
exec "$@"
