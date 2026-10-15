#!/usr/bin/env bash
# Deploy an environment: terraform plan -> confirmation -> migrations -> terraform apply ->
# release record -> smoke test. Never applies without a reviewed plan.
set -euo pipefail

# shellcheck source=lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: deploy/gcp/deploy.sh --env ENV [--images-file FILE | --foundation]
         [--plan-only] [--plan-file FILE] [--apply-plan FILE]
         [--var-file FILE]... [--backend-config FILE]
         [--skip-migrate] [--skip-smoke] [--rollback-of ID] [--dry-run] [--yes]

Release flow (default):
  1. terraform init (state in the bucket named by envs/ENV/backend.hcl)
  2. terraform plan with envs/ENV/terraform.tfvars and the images file -> a saved plan
  3. confirmation: type the environment name (skipped with --yes)
  4. migrations with the new server image (migrate.sh), BEFORE the new API rolls out;
     on the very first release the migrate job does not exist yet, so they run after apply
  5. terraform apply of exactly the saved plan
  6. release record: the images file is copied to gs://<state bucket>/mapi/ENV/releases/
     as <UTC stamp>-<release id>.json, the id naming the images (rollback.sh uses these)
  7. smoke test: API /healthz and /readyz, web app, plot sandbox, events not public

Modes:
  --foundation         no images: everything except the Cloud Run workloads and Jobs
                       (registry, networks, database, secrets, buckets, perimeter, CI)
  --plan-only          stop after step 2 (CI: plan job without approval)
  --apply-plan FILE    skip steps 1-3 planning and apply FILE (CI: approved apply job)

Options:
  --env ENV              environment
  --images-file FILE     images file from build-push.sh
                         (default: deploy/gcp/.out/ENV/images.tfvars.json)
  --plan-file FILE       where to save the plan (default: deploy/gcp/.out/ENV/tfplan)
  --var-file FILE        extra -var-file (repeatable)
  --backend-config FILE  backend config (default: envs/ENV/backend.hcl)
  --skip-migrate         do not run migrations (rollbacks: schema changes are forward-only)
  --skip-smoke           do not run the smoke test (e.g. while a new certificate provisions)
  --rollback-of ID       record this apply as a rollback to release ID (set by rollback.sh)
  --dry-run              print every command instead of running it
  --yes                  do not ask for confirmation
  -h, --help             show this help

Requires: terraform >= 1.11, gcloud, python3, curl.
EOF
}

env=""
images_file=""
foundation=0
plan_only=0
plan_file=""
apply_plan=""
backend_config=""
skip_migrate=0
skip_smoke=0
rollback_of=""
var_files=()

while (($#)); do
  case $1 in
    --env) need_value "$1" "${2:-}"; env=$2; shift 2 ;;
    --images-file) need_value "$1" "${2:-}"; images_file=$2; shift 2 ;;
    --foundation) foundation=1; shift ;;
    --plan-only) plan_only=1; shift ;;
    --plan-file) need_value "$1" "${2:-}"; plan_file=$2; shift 2 ;;
    --apply-plan) need_value "$1" "${2:-}"; apply_plan=$2; shift 2 ;;
    --var-file) need_value "$1" "${2:-}"; var_files+=("$2"); shift 2 ;;
    --backend-config) need_value "$1" "${2:-}"; backend_config=$2; shift 2 ;;
    --skip-migrate) skip_migrate=1; shift ;;
    --skip-smoke) skip_smoke=1; shift ;;
    --rollback-of) need_value "$1" "${2:-}"; rollback_of=$2; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --yes) ASSUME_YES=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
done

[[ -n $env ]] || die "--env is required"
[[ -z $rollback_of || $rollback_of =~ ^[A-Za-z0-9]+$ ]] || die "--rollback-of must be a release id"
valid_env "$env" || die "--env must match ^[a-z][a-z0-9-]{0,15}$"
[[ $plan_only == 0 || -z $apply_plan ]] || die "--plan-only and --apply-plan exclude each other"
[[ $foundation == 0 || -z $images_file ]] || die "--foundation deploys no images; drop --images-file"

root=$(env_dir "$env")
out=$(out_dir "$env")
backend_config=${backend_config:-"$root/backend.hcl"}
plan_file=${plan_file:-"$out/tfplan"}
if [[ $foundation == 0 ]]; then
  images_file=${images_file:-"$out/images.tfvars.json"}
fi

require_tool terraform
require_tool python3
require_tool gcloud
require_tool curl

if [[ $foundation == 0 ]]; then
  if [[ -f $images_file ]]; then
    check_images_file "$images_file" || die "$images_file is not a complete release (run build-push.sh)"
  elif [[ $DRY_RUN == 1 ]]; then
    warn "$images_file does not exist (ignored in a dry run)"
  else
    die "$images_file does not exist: run deploy/gcp/build-push.sh --env $env (or pass --foundation)"
  fi
fi
[[ -f $backend_config || $DRY_RUN == 1 ]] || die "$backend_config is missing: run deploy/gcp/bootstrap.sh"

tf() { run terraform -chdir="$root" "$@"; }

# --------------------------------------------------------------------------------- 1. init
tf init -input=false -reconfigure "-backend-config=$backend_config"

# ---------------------------------------------------------------------------- 2. plan
if [[ -z $apply_plan ]]; then
  plan_args=(-input=false "-out=$plan_file")
  if [[ -f "$root/terraform.tfvars" ]]; then
    plan_args+=("-var-file=$root/terraform.tfvars")
  else
    warn "$root/terraform.tfvars not found: variables must come from TF_VAR_* or --var-file"
  fi
  if [[ $foundation == 0 ]]; then
    plan_args+=("-var-file=$images_file")
  fi
  for vf in ${var_files[@]+"${var_files[@]}"}; do
    plan_args+=("-var-file=$vf")
  done
  tf plan "${plan_args[@]}"
  if [[ $plan_only == 1 ]]; then
    log "plan saved to $plan_file (apply it with: deploy.sh --env $env --apply-plan $plan_file)"
    exit 0
  fi
  apply_plan=$plan_file
fi
[[ -f $apply_plan || $DRY_RUN == 1 ]] || die "plan file $apply_plan does not exist"

# --------------------------------------------------------------------------- 3. confirm
confirm "Apply the plan above to '$env'?" "$env"

# ------------------------------------------------------------------- 4. migrations (before)
outputs="$out/outputs.json"
migrate_after=0
migrate_args=(--env "$env" --yes)
[[ $DRY_RUN == 0 ]] || migrate_args+=(--dry-run)
server_image=""
if [[ $foundation == 0 && $skip_migrate == 0 ]]; then
  if [[ -f $images_file ]]; then
    server_image=$(json_get "$images_file" images.server)
    migrate_args+=(--image "$server_image")
  fi
  if [[ $DRY_RUN == 0 ]] && tf_outputs "$env" "$outputs" && [[ $(json_get "$outputs" services_deployed) == "true" ]]; then
    "$REPO_ROOT/deploy/gcp/migrate.sh" "${migrate_args[@]}" \
      --project "$(json_get "$outputs" control_project)" --region "$(json_get "$outputs" region)"
  elif [[ $DRY_RUN == 1 ]]; then
    "$REPO_ROOT/deploy/gcp/migrate.sh" "${migrate_args[@]}" --project "<control project>" --region "<region>"
  else
    log "first release: the migrate job is created by this apply; migrations run right after it"
    migrate_after=1
  fi
fi

# -------------------------------------------------------------------------------- 5. apply
tf apply -input=false "$apply_plan"

if [[ $DRY_RUN == 0 ]]; then
  tf_outputs "$env" "$outputs" || die "no terraform outputs after apply"
fi

if [[ $migrate_after == 1 ]]; then
  "$REPO_ROOT/deploy/gcp/migrate.sh" "${migrate_args[@]}" \
    --project "$(json_get "$outputs" control_project)" --region "$(json_get "$outputs" region)"
fi

if [[ $foundation == 1 ]]; then
  log "foundation applied. Next: deploy/gcp/build-push.sh --env $env, then deploy/gcp/deploy.sh --env $env"
  exit 0
fi
if [[ $DRY_RUN == 0 && $(json_get "$outputs" services_deployed) != "true" ]]; then
  die "the apply did not include the Cloud Run workloads (images missing from the plan?)"
fi

# ----------------------------------------------------------------------- 6. release record
if [[ -f $images_file || $DRY_RUN == 1 ]]; then
  bucket=$(backend_bucket "$env" "$backend_config")
  # Named after what was deployed (not the operator's checkout), so a retried deploy of the
  # same images is the same release; rollbacks are marked (rollback.sh --previous replays them)
  if [[ -n $rollback_of ]]; then
    release="$(utc_stamp)-$rollback_of-rollback.json"
  elif [[ -f $images_file ]]; then
    release="$(utc_stamp)-$(python3 -I "$REPO_ROOT/deploy/gcp/lib/releases.py" id "$images_file").json"
  else
    release="$(utc_stamp)-RELEASEID.json"
  fi
  run gcloud storage cp "$images_file" "gs://$bucket/mapi/$env/releases/$release"
  log "release recorded as $release"
fi

# ---------------------------------------------------------------------------- 7. smoke test
smoke() {
  local api web plots events code
  api=$(json_get "$outputs" urls.api)
  web=$(json_get "$outputs" urls.web)
  plots=$(json_get "$outputs" urls.plots)
  events=$(json_get "$outputs" urls.run_app.events)
  local curl_opts=(--silent --show-error --fail --max-time 10 --retry 10 --retry-delay 6 --retry-all-errors)

  log "smoke: $api/healthz"
  curl "${curl_opts[@]}" -o /dev/null "$api/healthz"
  log "smoke: $api/readyz"
  curl "${curl_opts[@]}" -o /dev/null "$api/readyz"
  log "smoke: $web/ (with a Content-Security-Policy)"
  # Headers into a variable first: `curl | grep -q` would fail under pipefail when grep
  # exits early and curl gets SIGPIPE.
  local headers
  headers=$(curl "${curl_opts[@]}" -D - -o /dev/null "$web/")
  grep -qi '^content-security-policy:' <<<"$headers" ||
    die "the web app answers without a Content-Security-Policy header"
  log "smoke: $plots/"
  curl "${curl_opts[@]}" -o /dev/null "$plots/"
  log "smoke: $events/internal/events is not reachable from outside"
  code=$(curl --silent --max-time 10 -o /dev/null -w '%{http_code}' -X POST "$events/internal/events" || true)
  case $code in
    2??) die "the events service accepted an unauthenticated request from the internet (HTTP $code)" ;;
    *) log "events service refused the request (HTTP $code)" ;;
  esac
  log "smoke test passed"
}

if [[ $skip_smoke == 1 ]]; then
  log "smoke test skipped"
elif [[ $DRY_RUN == 1 ]]; then
  printf '[dry-run] would smoke-test /healthz, /readyz, the web app (CSP), the plot sandbox, and that events refuses public requests\n'
else
  smoke
fi
log "deployed '$env'"
