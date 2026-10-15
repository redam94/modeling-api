#!/usr/bin/env bash
# Run the database migrations of a release: point the mapi-ENV-migrate Cloud Run job at the
# release's server image, execute it and wait. Migrations are expand/contract: the previous
# release must keep working on the migrated schema, because the new API rolls out afterwards.
set -euo pipefail

# shellcheck source=lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: deploy/gcp/migrate.sh --env ENV [--project ID] [--region REGION] [--image IMAGE]
         [--job NAME] [--dry-run] [--yes]

Executes the migrate job (`mapi-server migrate`: Alembic, procrastinate schema) and waits for
it. Exits non-zero if the migration fails; the running release is untouched in that case.

Options:
  --env ENV        environment
  --project ID     control project (default: terraform output control_project)
  --region REGION  region (default: terraform output region)
  --image IMAGE    server image to migrate with, pinned by digest (default: the job's
                   current image)
  --job NAME       job name (default: mapi-ENV-migrate)
  --dry-run        print every command instead of running it
  --yes            do not ask for confirmation
  -h, --help       show this help

Requires: gcloud (roles/run.developer on the control project, or the deployer identity).
EOF
}

env=""
project=""
region=""
image=""
job=""

while (($#)); do
  case $1 in
    --env) need_value "$1" "${2:-}"; env=$2; shift 2 ;;
    --project) need_value "$1" "${2:-}"; project=$2; shift 2 ;;
    --region) need_value "$1" "${2:-}"; region=$2; shift 2 ;;
    --image) need_value "$1" "${2:-}"; image=$2; shift 2 ;;
    --job) need_value "$1" "${2:-}"; job=$2; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --yes) ASSUME_YES=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
done

[[ -n $env ]] || die "--env is required"
valid_env "$env" || die "--env must match ^[a-z][a-z0-9-]{0,15}$"
job=${job:-"mapi-$env-migrate"}
if [[ -n $image && ! $image =~ @sha256:[0-9a-f]{64}$ ]]; then
  die "--image must be pinned by digest (...@sha256:<64 hex>)"
fi

require_tool gcloud

if [[ -z $project || -z $region ]]; then
  outputs="$(out_dir "$env")/outputs.json"
  if tf_outputs "$env" "$outputs"; then
    project=${project:-$(json_get "$outputs" control_project)}
    region=${region:-$(json_get "$outputs" region)}
  fi
fi
[[ -n $project && -n $region ]] || die "--project and --region are required (no terraform outputs for '$env')"

confirm "Run database migrations for '$env' ($project, job $job)?" "$env"

if [[ -n $image ]]; then
  run gcloud run jobs update "$job" --image "$image" --region "$region" --project "$project" --quiet
fi

if ! run gcloud run jobs execute "$job" --region "$region" --project "$project" --wait; then
  cat >&2 <<EOF
error: the migration failed. The previous release is still serving. Inspect it with:
  gcloud run jobs executions list --job $job --region $region --project $project --limit 1
  gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="$job"' \\
    --project $project --limit 100 --freshness 1h
EOF
  exit 1
fi
if [[ $DRY_RUN == 1 ]]; then
  log "dry run: no migration was run"
else
  log "migrations applied"
fi
