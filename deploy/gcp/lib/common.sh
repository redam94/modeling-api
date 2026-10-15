# shellcheck shell=bash
# Shared helpers of deploy/gcp/*.sh. Sourced, never executed.
#
# Portable to bash 3.2 (macOS) and 5.x (CI): no associative arrays, no mapfile, no ${x,,}.
# Every command that changes something (or calls a cloud API) goes through `run`, which only
# prints it under --dry-run.

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
TF_ROOT="$REPO_ROOT/infra/terraform"
DRY_RUN=${DRY_RUN:-0}
ASSUME_YES=${ASSUME_YES:-0}

# The five images a release consists of (images/<name>/Dockerfile, target runtime).
RELEASE_IMAGES="server web base-bayes base-ml base-forecast"

log() { printf '==> %s\n' "$*" >&2; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

# Shell-quoted rendering of a command line (for logs and dry runs).
quote_cmd() {
  local out="" arg
  for arg in "$@"; do
    out="$out $(printf '%q' "$arg")"
  done
  printf '%s' "${out# }"
}

# run CMD...: execute, or only print under --dry-run.
run() {
  if [[ $DRY_RUN == 1 ]]; then
    printf '[dry-run] %s\n' "$(quote_cmd "$@")"
    return 0
  fi
  log "$(quote_cmd "$@")"
  "$@"
}

# require_tool NAME: fail unless NAME is on PATH (a dry run only warns).
require_tool() {
  if command -v "$1" >/dev/null 2>&1; then
    return 0
  fi
  if [[ $DRY_RUN == 1 ]]; then
    warn "$1 is not on PATH (ignored in a dry run)"
    return 0
  fi
  die "$1 is required but not on PATH"
}

# need_value FLAG VALUE: an option that takes a value got one.
need_value() {
  if [[ -z ${2:-} || ${2:0:2} == "--" ]]; then
    die "$1 needs a value"
  fi
}

valid_env() {
  [[ $1 =~ ^[a-z][a-z0-9-]{0,15}$ ]]
}

# env_dir ENV: the Terraform root of an environment (must exist).
env_dir() {
  local dir="$TF_ROOT/envs/$1"
  [[ -d $dir ]] || die "no Terraform environment '$1' (expected $dir)"
  printf '%s' "$dir"
}

# out_dir ENV: local scratch for an environment (images files, plans, outputs). Not committed.
out_dir() {
  local dir="${MAPI_DEPLOY_OUT:-$REPO_ROOT/deploy/gcp/.out}/$1"
  mkdir -p "$dir"
  printf '%s' "$dir"
}

# confirm PROMPT EXPECTED: the operator must type EXPECTED. --yes skips; a dry run never asks;
# without a terminal (and without --yes) the answer is no.
confirm() {
  local prompt=$1 expected=$2 answer=""
  if [[ $ASSUME_YES == 1 ]]; then
    log "$prompt (--yes)"
    return 0
  fi
  if [[ $DRY_RUN == 1 ]]; then
    printf '[dry-run] would ask: %s [type %s]\n' "$prompt" "$expected"
    return 0
  fi
  if [[ ! -t 0 ]]; then
    die "$prompt: no terminal to confirm on; pass --yes to proceed non-interactively"
  fi
  printf '%s\nType "%s" to continue: ' "$prompt" "$expected" >&2
  read -r answer
  [[ $answer == "$expected" ]] || die "not confirmed"
}

# backend_bucket ENV: the state bucket named in envs/ENV/backend.hcl.
backend_bucket() {
  local file
  file="$(env_dir "$1")/backend.hcl"
  if [[ ! -f $file && $DRY_RUN == 1 ]]; then
    printf '<state bucket>'
    return 0
  fi
  [[ -f $file ]] || die "$file is missing: run deploy/gcp/bootstrap.sh (or copy backend.hcl.example)"
  sed -n 's/^[[:space:]]*bucket[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$file" | head -n 1
}

# json_get FILE PATH: print a value from a JSON file; PATH is dot-separated (a.b.c).
# Strings print raw, everything else as JSON. Prints nothing when the path is absent.
json_get() {
  python3 -I -c '
import json, sys
value = json.load(open(sys.argv[1]))
for part in sys.argv[2].split("."):
    if isinstance(value, dict) and part in value:
        value = value[part]
    else:
        sys.exit(0)
print(value if isinstance(value, str) else json.dumps(value))
' "$1" "$2"
}

# tf_outputs ENV FILE: write `terraform output -json` (values unwrapped) of ENV to FILE.
# Needs an initialised working directory; returns non-zero when there is no state yet.
tf_outputs() {
  local raw
  raw=$(terraform -chdir="$(env_dir "$1")" output -json 2>/dev/null) || return 1
  [[ -n $raw && $raw != "{}" ]] || return 1
  printf '%s' "$raw" | python3 -I -c '
import json, sys
print(json.dumps({k: v["value"] for k, v in json.load(sys.stdin).items()}))
' >"$2"
}

# check_images_file FILE: a release images file names every image, each pinned by digest.
check_images_file() {
  python3 -I -c '
import json, re, sys
required = sys.argv[2].split()
images = json.load(open(sys.argv[1])).get("images", {})
missing = [k for k in required if k not in images]
if missing:
    sys.exit("images file lacks: " + ", ".join(missing))
bad = [k for k, v in images.items() if not re.search(r"@sha256:[0-9a-f]{64}$", v)]
if bad:
    sys.exit("not pinned by digest: " + ", ".join(bad))
' "$1" "$RELEASE_IMAGES"
}

utc_stamp() { date -u +%Y%m%dT%H%M%SZ; }
