#!/usr/bin/env bash
# Roll an environment back to an earlier release.
#
# Default: redeploy a recorded release (its images file) through deploy.sh, so Terraform state,
# services, the worker pool and the Job inventory all return to that release together.
# --traffic-only: the fast path for an incident; shift each service's traffic back to its
# previous ready revision with gcloud (seconds, no Terraform). The next deploy re-pins traffic.
#
# Database migrations are never rolled back (they are expand/contract: the previous release
# runs on the newer schema).
set -euo pipefail

# shellcheck source=lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: deploy/gcp/rollback.sh --env ENV (--previous | --release NAME | --images-file FILE)
         [--dry-run] [--yes]
       deploy/gcp/rollback.sh --env ENV --traffic-only [--services "api web plots events"]
         [--project ID] [--region REGION] [--dry-run] [--yes]
       deploy/gcp/rollback.sh --env ENV --list

Releases are the images files deploy.sh records in
gs://<state bucket>/mapi/ENV/releases/<UTC timestamp>-<git sha>.json.

Options:
  --env ENV            environment
  --list               list recorded releases, newest first
  --previous           redeploy the release before the newest one
  --release NAME       redeploy a named release (as printed by --list)
  --images-file FILE   redeploy from a local images file
  --traffic-only       shift service traffic to each service's previous ready revision
  --services "NAMES"   services for --traffic-only (default: "api events web plots")
  --project ID         control project (default: terraform output control_project)
  --region REGION      region (default: terraform output region)
  --dry-run            print every command instead of running it
  --yes                do not ask for confirmation
  -h, --help           show this help

--traffic-only does not touch the orchestrator worker pool or the Jobs; redeploy a release
for those.
EOF
}

env=""
mode=""
release=""
images_file=""
services="api events web plots"
project=""
region=""

set_mode() {
  [[ -z $mode ]] || die "choose one of --list, --previous, --release, --images-file, --traffic-only"
  mode=$1
}

while (($#)); do
  case $1 in
    --env) need_value "$1" "${2:-}"; env=$2; shift 2 ;;
    --list) set_mode list; shift ;;
    --previous) set_mode previous; shift ;;
    --release) need_value "$1" "${2:-}"; set_mode release; release=$2; shift 2 ;;
    --images-file) need_value "$1" "${2:-}"; set_mode file; images_file=$2; shift 2 ;;
    --traffic-only) set_mode traffic; shift ;;
    --services) need_value "$1" "${2:-}"; services=$2; shift 2 ;;
    --project) need_value "$1" "${2:-}"; project=$2; shift 2 ;;
    --region) need_value "$1" "${2:-}"; region=$2; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --yes) ASSUME_YES=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
done

[[ -n $env ]] || die "--env is required"
valid_env "$env" || die "--env must match ^[a-z][a-z0-9-]{0,15}$"
[[ -n $mode ]] || die "choose one of --list, --previous, --release, --images-file, --traffic-only"
[[ -z $release || $release =~ ^[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9]+\.json$ ]] || die "--release must look like 20261015T120000Z-abc123def456.json"

require_tool gcloud
require_tool python3
out=$(out_dir "$env")

# ------------------------------------------------------------------------ traffic-only path
if [[ $mode == traffic ]]; then
  if [[ -z $project || -z $region ]]; then
    if tf_outputs "$env" "$out/outputs.json"; then
      project=${project:-$(json_get "$out/outputs.json" control_project)}
      region=${region:-$(json_get "$out/outputs.json" region)}
    fi
  fi
  [[ -n $project && -n $region ]] || die "--project and --region are required (no terraform outputs for '$env')"
  confirm "Shift traffic of [$services] in '$env' back to their previous revisions?" "$env"
  for svc in $services; do
    name="mapi-$env-$svc"
    if [[ $DRY_RUN == 1 ]]; then
      run gcloud run services update-traffic "$name" --to-revisions "<previous revision of $name>=100" \
        --region "$region" --project "$project" --quiet
      continue
    fi
    previous=$(gcloud run revisions list --service "$name" --region "$region" --project "$project" \
      --filter='status.conditions.type=Ready AND status.conditions.status=True' \
      --sort-by='~metadata.creationTimestamp' --format='value(metadata.name)' --limit 2 | sed -n 2p)
    [[ -n $previous ]] || die "$name has no previous ready revision"
    run gcloud run services update-traffic "$name" --to-revisions "$previous=100" \
      --region "$region" --project "$project" --quiet
  done
  log "traffic rolled back; the next deploy.sh run moves it to the newest revision again"
  exit 0
fi

# ------------------------------------------------------------------------- release path
bucket=$(backend_bucket "$env")
prefix="gs://$bucket/mapi/$env/releases"

list_releases() {
  gcloud storage ls "$prefix/" 2>/dev/null | sed -n 's#.*/\([0-9]\{8\}T[0-9]\{6\}Z-[A-Za-z0-9]*\.json\)$#\1#p' | sort -r
}

case $mode in
  list)
    list_releases
    exit 0
    ;;
  previous)
    if [[ $DRY_RUN == 1 ]]; then
      release="<previous release>"
    else
      release=$(list_releases | sed -n 2p)
      [[ -n $release ]] || die "no previous release recorded under $prefix"
    fi
    ;;
esac

if [[ $mode != file ]]; then
  images_file="$out/rollback-$release"
  run gcloud storage cp "$prefix/$release" "$images_file"
fi
if [[ $DRY_RUN == 0 ]]; then
  check_images_file "$images_file" || die "$images_file is not a complete release"
fi

log "rolling '$env' back to ${release:-$images_file}"
deploy_args=(--env "$env" --images-file "$images_file" --skip-migrate)
[[ $DRY_RUN == 0 ]] || deploy_args+=(--dry-run)
[[ $ASSUME_YES == 0 ]] || deploy_args+=(--yes)
"$MAPI_REPO_ROOT/deploy/gcp/deploy.sh" "${deploy_args[@]}"
