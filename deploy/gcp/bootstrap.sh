#!/usr/bin/env bash
# Prepare existing GCP projects for the first `terraform apply` of an environment:
# enable the APIs each project role needs and create the Terraform state bucket.
# Idempotent. Creates no project (projects come from your organisation's vending process).
set -euo pipefail

# shellcheck source=lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: deploy/gcp/bootstrap.sh --env ENV --region REGION \
         --control-project ID --data-project ID --build-project ID \
         --exec-project POOL=ID [--exec-project POOL=ID ...] \
         --state-project ID [--state-bucket NAME] [--state-location LOC] \
         [--dry-run] [--yes]

Prepares existing projects for infra/terraform/envs/ENV:

  1. enables the APIs of each project role (the same lists as modules/stack/apis.tf);
  2. creates the Terraform state bucket (uniform access, public access prevention,
     object versioning) in the state project, if it does not exist;
  3. writes infra/terraform/envs/ENV/backend.hcl (bucket + prefix mapi/ENV).

Options:
  --env ENV               dev, staging or prod (any name matching ^[a-z][a-z0-9-]{0,15}$)
  --region REGION         default location for the state bucket (e.g. europe-west4)
  --control-project ID    trusted control-plane project
  --data-project ID       bucket project (inside the VPC-SC perimeter)
  --build-project ID      Artifact Registry / CI identity project
  --exec-project POOL=ID  an exec project and its pool name (paid, public); repeatable
  --state-project ID      project that holds the state bucket (often the build project)
  --state-bucket NAME     state bucket (default: <state-project>-mapi-tfstate)
  --state-location LOC    state bucket location (default: --region)
  --dry-run               print every command instead of running it
  --yes                   do not ask for confirmation
  -h, --help              show this help

Requires: gcloud (authenticated as a project owner or equivalent).
EOF
}

# Keep in sync with infra/terraform/modules/stack/apis.tf (tests/infra checks it).
APIS_CONTROL=(
  accesscontextmanager.googleapis.com
  cloudkms.googleapis.com
  cloudresourcemanager.googleapis.com
  cloudtrace.googleapis.com
  compute.googleapis.com
  iam.googleapis.com
  iamcredentials.googleapis.com
  identitytoolkit.googleapis.com
  logging.googleapis.com
  monitoring.googleapis.com
  pubsub.googleapis.com
  run.googleapis.com
  secretmanager.googleapis.com
  servicenetworking.googleapis.com
  serviceusage.googleapis.com
  sqladmin.googleapis.com
)
APIS_EXEC=(
  binaryauthorization.googleapis.com
  cloudresourcemanager.googleapis.com
  compute.googleapis.com
  dns.googleapis.com
  iam.googleapis.com
  logging.googleapis.com
  run.googleapis.com
  serviceusage.googleapis.com
)
APIS_DATA=(
  cloudkms.googleapis.com
  cloudresourcemanager.googleapis.com
  iam.googleapis.com
  serviceusage.googleapis.com
  storage.googleapis.com
)
APIS_BUILD=(
  artifactregistry.googleapis.com
  binaryauthorization.googleapis.com
  cloudkms.googleapis.com
  cloudresourcemanager.googleapis.com
  containeranalysis.googleapis.com
  containerscanning.googleapis.com
  iam.googleapis.com
  iamcredentials.googleapis.com
  serviceusage.googleapis.com
  sts.googleapis.com
)

env=""
region=""
control=""
data=""
build=""
state_project=""
state_bucket=""
state_location=""
exec_specs=()

while (($#)); do
  case $1 in
    --env) need_value "$1" "${2:-}"; env=$2; shift 2 ;;
    --region) need_value "$1" "${2:-}"; region=$2; shift 2 ;;
    --control-project) need_value "$1" "${2:-}"; control=$2; shift 2 ;;
    --data-project) need_value "$1" "${2:-}"; data=$2; shift 2 ;;
    --build-project) need_value "$1" "${2:-}"; build=$2; shift 2 ;;
    --exec-project) need_value "$1" "${2:-}"; exec_specs+=("$2"); shift 2 ;;
    --state-project) need_value "$1" "${2:-}"; state_project=$2; shift 2 ;;
    --state-bucket) need_value "$1" "${2:-}"; state_bucket=$2; shift 2 ;;
    --state-location) need_value "$1" "${2:-}"; state_location=$2; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --yes) ASSUME_YES=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
done

[[ -n $env ]] || die "--env is required"
valid_env "$env" || die "--env must match ^[a-z][a-z0-9-]{0,15}$"
[[ -n $region ]] || die "--region is required"
[[ -n $control && -n $data && -n $build ]] || die "--control-project, --data-project and --build-project are required"
((${#exec_specs[@]} > 0)) || die "at least one --exec-project POOL=ID is required"
[[ -n $state_project ]] || die "--state-project is required"
state_bucket=${state_bucket:-"$state_project-mapi-tfstate"}
state_location=${state_location:-$region}
env_root=$(env_dir "$env")

for spec in "${exec_specs[@]}"; do
  [[ $spec =~ ^[a-z][a-z0-9]{0,9}=[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] ||
    die "--exec-project must be POOL=PROJECT_ID (got '$spec')"
done

require_tool gcloud

log "environment $env: control=$control data=$data build=$build exec=${exec_specs[*]}"
log "state bucket gs://$state_bucket in $state_project ($state_location)"
confirm "Enable APIs and create the state bucket for '$env'?" "$env"

enable_apis() {
  local project=$1
  shift
  run gcloud services enable "$@" --project "$project" --quiet
}

enable_apis "$control" "${APIS_CONTROL[@]}"
enable_apis "$data" "${APIS_DATA[@]}"
enable_apis "$build" "${APIS_BUILD[@]}"
for spec in "${exec_specs[@]}"; do
  enable_apis "${spec#*=}" "${APIS_EXEC[@]}"
done
run gcloud services enable storage.googleapis.com --project "$state_project" --quiet

# State bucket: private, versioned (state history), never public. Reading it means reading
# the infrastructure's shape, so only operators and the CI identities get access. The describe
# is read-only, so a dry run makes it too: its preview then matches what a real run would do.
if gcloud storage buckets describe "gs://$state_bucket" --project "$state_project" >/dev/null 2>&1; then
  log "state bucket gs://$state_bucket exists"
else
  [[ $DRY_RUN == 0 ]] || log "state bucket gs://$state_bucket not found (or not readable with these credentials)"
  run gcloud storage buckets create "gs://$state_bucket" \
    --project "$state_project" \
    --location "$state_location" \
    --default-storage-class STANDARD \
    --uniform-bucket-level-access \
    --public-access-prevention
fi
run gcloud storage buckets update "gs://$state_bucket" --versioning

backend="$env_root/backend.hcl"
content=$(printf 'bucket = "%s"\nprefix = "mapi/%s"\n' "$state_bucket" "$env")
if [[ $DRY_RUN == 1 ]]; then
  printf '[dry-run] would write %s:\n%s\n' "$backend" "$content"
else
  printf '%s\n' "$content" >"$backend"
  log "wrote $backend"
fi

cat >&2 <<EOF

Next steps:
  1. cp $env_root/terraform.tfvars.example $env_root/terraform.tfvars   # fill in
  2. deploy/gcp/deploy.sh --env $env --foundation      # registry, network, database, buckets
  3. deploy/gcp/build-push.sh --env $env               # images, pinned by digest
  4. deploy/gcp/deploy.sh --env $env                   # services, jobs, migrations, smoke test
EOF
