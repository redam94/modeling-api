#!/usr/bin/env bash
# Build the release images, push them to Artifact Registry and write their digests to an images
# file for deploy.sh. Deploys always reference digests, never tags.
set -euo pipefail

# shellcheck source=lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

usage() {
  cat <<EOF
Usage: deploy/gcp/build-push.sh --env ENV [--repository REPO] [--images "NAMES"]
         [--tag TAG] [--out FILE] [--platform PLATFORM] [--allow-dirty] [--dry-run] [--yes]

Builds images/<name>/Dockerfile (target runtime) for each release image, pushes it as
REPO/<name>:TAG and records REPO/<name>@sha256:<digest> in an images file:

  {"images": {"server": "...@sha256:...", "web": "...", "base-bayes": "...", ...}}

The registry's tags are immutable: a tag that already exists is not rebuilt; its digest is
looked up and reused (re-running for the same commit is safe).

Options:
  --env ENV            environment (selects the default repository and output file)
  --repository REPO    REGION-docker.pkg.dev/PROJECT/REPO (default: terraform output
                       image_repository of the environment)
  --images "NAMES"     subset to build (default: "$RELEASE_IMAGES")
  --tag TAG            image tag (default: <version>-<git short sha>)
  --out FILE           images file (default: deploy/gcp/.out/ENV/images.tfvars.json)
  --platform PLATFORM  target platform (default: linux/amd64, what Cloud Run runs)
  --allow-dirty        build from a working tree with uncommitted changes
  --no-attestations    skip the SBOM and provenance attestations (pushes a plain manifest)
  --dry-run            print every command instead of running it
  --yes                do not ask for confirmation
  -h, --help           show this help

Requires: docker with buildx, gcloud, git, python3.
EOF
}

env=""
repository=""
images="$RELEASE_IMAGES"
tag=""
out=""
platform="linux/amd64"
allow_dirty=0
attestations=1

while (($#)); do
  case $1 in
    --env) need_value "$1" "${2:-}"; env=$2; shift 2 ;;
    --repository) need_value "$1" "${2:-}"; repository=$2; shift 2 ;;
    --images) need_value "$1" "${2:-}"; images=$2; shift 2 ;;
    --tag) need_value "$1" "${2:-}"; tag=$2; shift 2 ;;
    --out) need_value "$1" "${2:-}"; out=$2; shift 2 ;;
    --platform) need_value "$1" "${2:-}"; platform=$2; shift 2 ;;
    --allow-dirty) allow_dirty=1; shift ;;
    --no-attestations) attestations=0; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --yes) ASSUME_YES=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
done

[[ -n $env ]] || die "--env is required"
valid_env "$env" || die "--env must match ^[a-z][a-z0-9-]{0,15}$"
for name in $images; do
  case " $RELEASE_IMAGES " in
    *" $name "*) ;;
    *) die "unknown image '$name' (release images: $RELEASE_IMAGES)" ;;
  esac
done

require_tool git
require_tool python3
require_tool docker
require_tool gcloud

if [[ -z $repository ]]; then
  outputs="$(out_dir "$env")/outputs.json"
  if tf_outputs "$env" "$outputs"; then
    repository=$(json_get "$outputs" image_repository)
  fi
  [[ -n $repository ]] || die "--repository is required (no terraform output image_repository for '$env'; run deploy.sh --foundation first)"
fi
[[ $repository =~ ^[a-z0-9-]+-docker\.pkg\.dev/[a-z][a-z0-9-]+/[a-z0-9._-]+$ ]] ||
  die "--repository must look like REGION-docker.pkg.dev/PROJECT/REPO"
registry=${repository%%/*}

cd "$REPO_ROOT"
if [[ $allow_dirty == 0 && -n $(git status --porcelain --untracked-files=no) ]]; then
  die "the working tree has uncommitted changes; commit them or pass --allow-dirty"
fi
version=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -n 1)
vcs_ref=$(git rev-parse --short=12 HEAD)
tag=${tag:-"$version-$vcs_ref"}
[[ $tag =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || die "invalid tag '$tag'"
build_date=$(date -u +%Y-%m-%dT%H:%M:%SZ)
out=${out:-"$(out_dir "$env")/images.tfvars.json"}
meta_dir=$(mktemp -d)
trap 'rm -rf "$meta_dir"' EXIT

log "building [$images] as $repository/<name>:$tag for $platform"
confirm "Build and push to $repository?" "yes"

run gcloud auth configure-docker "$registry" --quiet

# existing_digest NAME: digest of REPO/NAME:TAG if that tag was already pushed.
existing_digest() {
  [[ $DRY_RUN == 0 ]] || return 1
  gcloud artifacts docker images describe "$repository/$1:$tag" \
    --format='value(image_summary.digest)' 2>/dev/null
}

record=""
for name in $images; do
  ref="$repository/$name:$tag"
  if digest=$(existing_digest "$name") && [[ -n $digest ]]; then
    log "$ref exists ($digest); not rebuilding"
  else
    run docker buildx build \
      --platform "$platform" \
      --file "images/$name/Dockerfile" \
      --target runtime \
      --build-arg "VERSION=$version" \
      --build-arg "VCS_REF=$vcs_ref" \
      --build-arg "BUILD_DATE=$build_date" \
      --label "org.opencontainers.image.revision=$vcs_ref" \
      --provenance="$([[ $attestations == 1 ]] && echo true || echo false)" \
      --sbom="$([[ $attestations == 1 ]] && echo true || echo false)" \
      --tag "$ref" \
      --metadata-file "$meta_dir/$name.json" \
      --push \
      .
    if [[ $DRY_RUN == 1 ]]; then
      digest="sha256:<digest of $name>"
    else
      digest=$(json_get "$meta_dir/$name.json" containerimage.digest)
    fi
  fi
  [[ $DRY_RUN == 1 || $digest =~ ^sha256:[0-9a-f]{64}$ ]] || die "no digest for $ref"
  record="$record$name=$repository/$name@$digest
"
done

# Merge into an existing images file, so building a subset keeps the other digests.
json=$(printf '%s' "$record" | python3 -I -c '
import json, os, sys
path = sys.argv[1]
images = {}
if os.path.exists(path):
    images = json.load(open(path)).get("images", {})
for line in sys.stdin.read().splitlines():
    if line:
        name, ref = line.split("=", 1)
        images[name] = ref
print(json.dumps({"images": dict(sorted(images.items()))}, indent=2))
' "$out")

if [[ $DRY_RUN == 1 ]]; then
  printf '[dry-run] would write %s:\n%s\n' "$out" "$json"
else
  printf '%s\n' "$json" >"$out"
  log "wrote $out"
fi
