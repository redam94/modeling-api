"""Static checks of infra/terraform: the security invariants hold in the HCL itself, and the
names the platform relies on agree with the mapi_spec contracts.

These run everywhere (no terraform binary, no providers). The mocked-apply suites under
``infra/terraform/modules/*/tests`` check the same properties on evaluated values; see
``test_terraform.py``.
"""

from __future__ import annotations

import re

import pytest
from mapi_spec.api import ENDPOINTS
from mapi_spec.resources import RESOURCE_CLASSES, ResourceClass
from mapi_spec.run import ENV_BOOTSTRAP_URL, ENV_NONCE, ENV_RUN_ID
from mapi_spec.storage import BucketPurpose, bucket_name

from .conftest import (
    DEPLOY,
    ENVS,
    MODULES,
    TF_ROOT,
    Block,
    parse_blocks,
    resources,
    strip_comments,
    tf_files,
)

GIB = 1024**3


def _module_blocks(module: str) -> list[Block]:
    return [b for p in tf_files("modules", module) for b in parse_blocks(p)]


def _one(blocks: list[Block], rtype: str, name: str) -> Block:
    found = [b for b in blocks if b.kind == "resource" and b.type == rtype and b.name == name]
    assert len(found) == 1, f"expected one {rtype}.{name}, found {len(found)}"
    return found[0]


# ------------------------------------------------------------------------------ layout


def test_layout() -> None:
    for module in MODULES:
        d = TF_ROOT / "modules" / module
        for f in ("versions.tf", "variables.tf", "outputs.tf"):
            assert (d / f).is_file(), f"modules/{module}/{f} is missing"
        assert list((d / "tests").glob("*.tftest.hcl")), f"modules/{module} has no terraform tests"
    for env in ENVS:
        d = TF_ROOT / "envs" / env
        for f in ("main.tf", "variables.tf", "outputs.tf", "versions.tf", ".terraform.lock.hcl"):
            assert (d / f).is_file(), f"envs/{env}/{f} is missing"
        for f in ("terraform.tfvars.example", "backend.hcl.example"):
            assert (d / f).is_file(), f"envs/{env}/{f} is missing"


@pytest.mark.parametrize("env", ENVS)
def test_env_backend_and_provider_pins(env: str) -> None:
    versions = (TF_ROOT / "envs" / env / "versions.tf").read_text()
    assert re.search(r'backend\s+"gcs"\s*\{\s*\}', versions), "state lives in GCS (partial config)"
    lock = (TF_ROOT / "envs" / env / ".terraform.lock.hcl").read_text()
    for provider in ("hashicorp/google", "hashicorp/google-beta", "hashicorp/random"):
        assert f'provider "registry.terraform.io/{provider}"' in lock
    assert re.search(r'google"\s*\{\s*version\s*=\s*"7\.', lock), "google provider 7.x is locked"
    assert lock.count('"zh:') > 10, "the lock carries registry hashes for every platform"
    example = (TF_ROOT / "envs" / env / "backend.hcl.example").read_text()
    assert f'prefix = "mapi/{env}"' in example


@pytest.mark.parametrize("env", ENVS)
def test_env_wires_the_stack(env: str) -> None:
    main = [b for b in parse_blocks(TF_ROOT / "envs" / env / "main.tf") if b.kind == "module"]
    assert [b.type for b in main] == ["stack"]
    body = main[0].body
    assert "env = local.env" in re.sub(r"\s+", " ", body) or "env             = local.env" in body
    assert re.search(rf'env\s*=\s*"{env}"', (TF_ROOT / "envs" / env / "main.tf").read_text())


def test_outside_dev_the_perimeter_is_mandatory_and_enforced() -> None:
    for env in ("staging", "prod"):
        variables = {
            b.type: b
            for b in parse_blocks(TF_ROOT / "envs" / env / "variables.tf")
            if b.kind == "variable"
        }
        assert "default" not in variables["access_policy_id"].body, (
            f"{env}: access_policy_id is required"
        )
        main = (TF_ROOT / "envs" / env / "main.tf").read_text()
        assert re.search(r"dry_run\s*=\s*false", main), f"{env}: the perimeter is enforced"
    prod_vars = {b.type: b for b in parse_blocks(TF_ROOT / "envs" / "prod" / "variables.tf")}
    assert "default" not in prod_vars["domains"].body, "prod is served on real domains"
    prod = (TF_ROOT / "envs" / "prod" / "main.tf").read_text()
    assert re.search(r"lock_bundle_retention\s*=\s*true", prod)
    assert re.search(r"deletion_protection\s*=\s*true", prod)
    assert re.search(r'availability_type\s*=\s*"REGIONAL"', prod)
    assert re.search(r'protection_level\s*=\s*"HSM"', prod)


def test_examples_hold_no_secrets() -> None:
    for env in ENVS:
        text = (TF_ROOT / "envs" / env / "terraform.tfvars.example").read_text()
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        assert not re.search(r"(?i)(secret|password|pepper|private_key)\s*=", code), env


# ---------------------------------------------------------------------------- no public buckets


def test_buckets_are_never_public(all_blocks: list[Block]) -> None:
    buckets = resources(all_blocks, "google_storage_bucket")
    assert buckets, "the data module defines the buckets"
    for b in buckets:
        assert re.search(r'public_access_prevention\s*=\s*"enforced"', b.body), b.name
        assert re.search(r"uniform_bucket_level_access\s*=\s*true", b.body), b.name
        assert "predefined_acl" not in b.body
    for rtype in (
        "google_storage_bucket_acl",
        "google_storage_default_object_acl",
        "google_storage_object_acl",
        "google_storage_bucket_access_control",
        "google_storage_default_object_access_control",
    ):
        assert not resources(all_blocks, rtype), f"{rtype} is never used (uniform access only)"


def test_no_public_principals_anywhere() -> None:
    for path in tf_files():
        code = re.sub(r"#.*", "", path.read_text())
        assert "allAuthenticatedUsers" not in code, path
        # allUsers may never be an IAM member; public Cloud Run services use invoker_iam_disabled.
        assert not re.search(r'member\s*=\s*"allUsers"', code), path


def test_bucket_names_match_the_storage_contract() -> None:
    buckets = (TF_ROOT / "modules" / "data" / "buckets.tf").read_text()
    local = re.search(r"buckets = \{(.*?)\n  \}\n", buckets, re.S)
    assert local
    declared = set(re.findall(r"^    (\w+) = \{", local.group(1), re.M))
    assert declared == {p.value for p in BucketPurpose}
    assert "name          = local.bucket_names[each.key]" in buckets
    assert '"mapi-${var.env}-${purpose}"' in buckets
    assert bucket_name("prod", BucketPurpose.BUNDLES) == "mapi-prod-bundles"
    env_rule = re.search(
        r'regex\("(\^\[a-z\]\[a-z0-9-\]\{0,15\}\$)"',
        (TF_ROOT / "modules/data/variables.tf").read_text(),
    )
    assert env_rule and env_rule.group(1) == "^[a-z][a-z0-9-]{0,15}$"


def test_bootstrap_bucket_is_read_once() -> None:
    blocks = _module_blocks("data")
    role = _one(blocks, "google_project_iam_custom_role", "bootstrap_consumer")
    perms = set(re.findall(r'"(storage\.[\w.]+)"', role.body))
    assert perms == {"storage.objects.get", "storage.objects.delete"}, "get + delete, no list"
    buckets = (TF_ROOT / "modules" / "data" / "buckets.tf").read_text()
    bootstrap = re.search(r"bootstrap = \{(.*?)\}", buckets, re.S)
    assert bootstrap and re.search(r"soft_delete_seconds\s*=\s*0", bootstrap.group(1))
    iam = (TF_ROOT / "modules" / "data" / "iam.tf").read_text()
    exec_grants = re.findall(r'\["(\w+)", "(\w+)", "exec-\$\{pool\}"\]', iam)
    assert exec_grants == [("bootstrap", "bootstrap_consumer")], "executions touch bootstrap only"


def test_bundles_cannot_be_deleted_by_the_platform() -> None:
    iam = (TF_ROOT / "modules" / "data" / "iam.tf").read_text()
    grants = re.findall(r'\["bundles", "(\w+)", "(\w+)"\]', iam)
    assert grants, "bundle grants exist"
    assert all(role in {"objectCreator", "objectViewer", "reader"} for role, _ in grants), grants
    buckets = (TF_ROOT / "modules" / "data" / "buckets.tf").read_text()
    assert 'force_destroy = var.force_destroy && each.key != "bundles"' in buckets
    assert "is_locked        = var.lock_bundle_retention" in buckets


# --------------------------------------------------------------------------------- exec / egress


def test_deny_all_egress() -> None:
    blocks = _module_blocks("exec")
    deny = _one(blocks, "google_compute_firewall", "deny_all_egress")
    assert re.search(r'direction\s*=\s*"EGRESS"', deny.body)
    assert re.search(r'destination_ranges\s*=\s*\["0\.0\.0\.0/0"\]', deny.body)
    assert re.search(r'deny\s*\{\s*protocol\s*=\s*"all"\s*\}', deny.body)
    assert re.search(r"priority\s*=\s*65534", deny.body)
    v6 = _one(blocks, "google_compute_firewall", "deny_all_egress_ipv6")
    assert '"::/0"' in v6.body

    allows = [
        b
        for b in resources(blocks, "google_compute_firewall")
        if "allow {" in b.body or "allow{" in b.body
    ]
    targets = {b.name: b.body for b in allows}
    assert set(targets) == {"allow_restricted_vip", "allow_cloud_dns"}, "only two egress exceptions"
    assert "local.restricted_vip" in targets["allow_restricted_vip"]
    assert re.findall(r"ports\s*=\s*\[([^\]]*)\]", targets["allow_restricted_vip"]) == ['"443"']
    assert "local.cloud_dns_range" in targets["allow_cloud_dns"]
    assert set(re.findall(r"ports\s*=\s*\[([^\]]*)\]", targets["allow_cloud_dns"])) == {'"53"'}

    network = (TF_ROOT / "modules" / "exec" / "network.tf").read_text()
    assert 'restricted_vip  = "199.36.153.4/30"' in network
    assert 'cloud_dns_range = "35.199.192.0/19"' in network
    net = _one(blocks, "google_compute_network", "exec")
    assert re.search(r"delete_default_routes_on_create\s*=\s*true", net.body)
    routes = resources(blocks, "google_compute_route")
    assert [r.name for r in routes] == ["restricted_vip"], (
        "the restricted VIP is the only route out"
    )


def test_dns_response_policy() -> None:
    blocks = _module_blocks("exec")
    sinkhole = _one(blocks, "google_dns_response_policy_rule", "sinkhole_all")
    assert re.search(r'dns_name\s*=\s*"\*\."', sinkhole.body)
    zone = _one(blocks, "google_dns_managed_zone", "googleapis")
    assert re.search(r'dns_name\s*=\s*"googleapis\.com\."', zone.body)
    assert re.search(r'visibility\s*=\s*"private"', zone.body)
    vip = _one(blocks, "google_dns_record_set", "restricted")
    assert set(re.findall(r'"(199\.36\.153\.\d)"', vip.body)) == {
        f"199.36.153.{i}" for i in range(4, 8)
    }
    dns = (TF_ROOT / "modules" / "exec" / "dns.tf").read_text()
    bypass = re.search(r"dns_bypass = \{(.*?)\}", dns, re.S)
    assert bypass
    assert set(re.findall(r'"([^"]+)"', bypass.group(1))) == {
        "googleapis.com.",
        "*.googleapis.com.",
        "metadata.google.internal.",
    }


def test_every_job_has_max_retries_zero(all_blocks: list[Block]) -> None:
    jobs = resources(all_blocks, "google_cloud_run_v2_job")
    assert {j.name for j in jobs} == {"runner", "migrate"}
    for job in jobs:
        assert re.search(r"max_retries\s*=\s*0\b", job.body), f"{job.name}: max-retries must be 0"
        assert 'execution_environment = "EXECUTION_ENVIRONMENT_GEN2"' in job.body, job.name


def test_runner_jobs_are_sandboxed_and_env_only() -> None:
    runner = _one(_module_blocks("exec"), "google_cloud_run_v2_job", "runner")
    assert re.search(r'egress\s*=\s*"ALL_TRAFFIC"', runner.body), (
        "all traffic into the deny-all VPC"
    )
    assert re.findall(r'name\s*=\s*"(MAPI_\w+)"', runner.body) == [ENV_BOOTSTRAP_URL]
    # MAPI_RUN_ID / MAPI_NONCE are per-execution overrides; never baked into the Job.
    assert ENV_RUN_ID not in runner.body and ENV_NONCE not in runner.body
    assert "service_account       = google_service_account.exec.email" in runner.body
    jobs = (TF_ROOT / "modules" / "exec" / "jobs.tf").read_text()
    assert "setproduct(sort(keys(var.images)), var.classes)" in jobs


def test_class_sizes_match_the_resource_contract() -> None:
    variables = (TF_ROOT / "modules" / "exec" / "variables.tf").read_text()
    specs = dict(
        (cls, (cpu, mem))
        for cls, cpu, mem in re.findall(r'(\w) = \{ cpu = "(\d+)", memory = "(\d+)Gi"', variables)
    )
    assert set(specs) == {"S", "M", "L"}
    for cls, (cpu, mem) in specs.items():
        contract = RESOURCE_CLASSES[ResourceClass(cls)]
        assert int(cpu) == contract.vcpu, cls
        assert int(mem) * GIB == contract.ram_bytes, cls


def test_exec_identity_holds_no_project_roles() -> None:
    blocks = _module_blocks("exec")
    members = resources(blocks, "google_project_iam_member")
    assert [m.name for m in members] == ["dispatcher"]
    assert "var.orchestrator_sa_email" in members[0].body
    role = _one(blocks, "google_project_iam_custom_role", "dispatcher")
    perms = set(re.findall(r'"(run\.[\w.]+)"', role.body))
    assert "run.jobs.run" in perms and "run.executions.cancel" in perms
    assert not perms & {
        "run.jobs.create",
        "run.jobs.update",
        "run.jobs.delete",
        "run.jobs.setIamPolicy",
    }


# ----------------------------------------------------------------------------- VPC-SC perimeter


def test_vpc_sc_perimeter_present() -> None:
    blocks = _module_blocks("perimeter")
    perimeter = _one(blocks, "google_access_context_manager_service_perimeter", "exec_data")
    assert "use_explicit_dry_run_spec = var.dry_run" in perimeter.body
    assert perimeter.body.count("vpc_accessible_services") == 2, "status and dry-run spec alike"
    variables = (TF_ROOT / "modules" / "perimeter" / "variables.tf").read_text()
    restricted = re.search(r'variable "restricted_services".*?default = \[(.*?)\]', variables, re.S)
    assert restricted
    services = set(re.findall(r'"([\w.]+)"', restricted.group(1)))
    assert {
        "storage.googleapis.com",
        "pubsub.googleapis.com",
        "bigquery.googleapis.com",
    } <= services
    assert re.search(
        r'variable "vpc_allowed_services".*?default\s*=\s*\["storage\.googleapis\.com"\]',
        variables,
        re.S,
    )
    stack = (TF_ROOT / "modules" / "stack" / "main.tf").read_text()
    assert 'source = "../perimeter"' in stack
    assert 'check "perimeter_enabled"' in stack


# ----------------------------------------------------------------------------- control plane


def test_events_path() -> None:
    blocks = _module_blocks("control")
    sub = _one(blocks, "google_pubsub_subscription", "run_events_push")
    assert "oidc_token" in sub.body and "dead_letter_policy" in sub.body
    locals_tf = (TF_ROOT / "modules" / "control" / "locals.tf").read_text()
    assert '"jsonPayload.mapi_event:*"' in locals_tf
    push = re.search(r'push_endpoint = "\$\{local\.run_url\.events\}(/[\w/]+)"', locals_tf)
    assert push
    internal = [e for e in ENDPOINTS if e.method == "POST" and e.path == push.group(1)]
    assert internal, f"the push endpoint {push.group(1)} is a declared API route"
    events = _one(blocks, "google_cloud_run_v2_service", "events")
    assert 'ingress = "INGRESS_TRAFFIC_INTERNAL_ONLY"' in events.body
    assert "invoker_iam_disabled" not in events.body
    sink = _one(blocks, "google_logging_project_sink", "run_events")
    assert "unique_writer_identity = true" in sink.body


def test_database_is_private_postgres16() -> None:
    sql = _one(_module_blocks("control"), "google_sql_database_instance", "main")
    assert 'database_version    = "POSTGRES_16"' in sql.body
    assert "ipv4_enabled                                  = false" in sql.body
    assert 'ssl_mode                                      = "ENCRYPTED_ONLY"' in sql.body
    assert "connection_pooling_enabled = true" in sql.body


def test_secrets_never_reach_state() -> None:
    blocks = _module_blocks("control")
    for b in resources(blocks, "google_secret_manager_secret_version"):
        assert "secret_data_wo" in b.body, b.name
        assert not re.search(r"^\s*secret_data\s*=", b.body, re.M), b.name
    for b in resources(blocks, "google_sql_user"):
        assert "password_wo" in b.body
        assert not re.search(r"^\s*password\s*=", b.body, re.M)
    assert not resources(blocks, "random_password"), "generated secrets are ephemeral resources"
    variables = (TF_ROOT / "modules" / "control" / "variables.tf").read_text()
    secret_var = re.search(r'variable "oidc_client_secret" \{(.*?)\n\}', variables, re.S)
    assert secret_var and "ephemeral   = true" in secret_var.group(1)
    run_tf = (TF_ROOT / "modules" / "control" / "run.tf").read_text()
    assert 'version = "latest"' in run_tf and "secret_key_ref" in run_tf


def test_images_are_deployed_by_digest() -> None:
    for module in ("stack", "control", "exec"):
        text = (TF_ROOT / "modules" / module / "variables.tf").read_text()
        assert "sha256:[0-9a-f]{64}" in text, f"{module} validates digests"
    for path in tf_files():
        assert ":latest" not in path.read_text(), path


def test_github_federation_is_scoped() -> None:
    wif = (TF_ROOT / "modules" / "build" / "wif.tf").read_text()
    assert 'attribute_condition = "assertion.repository == \\"${var.github.repository}\\""' in wif
    assert "/attribute.environment/${each.value.environment}" in wif
    assert "google_service_account_key" not in "".join(p.read_text() for p in tf_files())


# ------------------------------------------------------------------ APIs in sync with bootstrap


def _tf_apis() -> dict[str, set[str]]:
    text = (TF_ROOT / "modules" / "stack" / "apis.tf").read_text()
    out = {}
    for role in ("control", "exec", "data", "build"):
        m = re.search(rf"    {role} = \[(.*?)\]", text, re.S)
        assert m, role
        out[role] = set(re.findall(r'"([\w.]+)"', m.group(1)))
    return out


def _bootstrap_apis() -> dict[str, set[str]]:
    text = (DEPLOY / "bootstrap.sh").read_text()
    out = {}
    for role in ("control", "exec", "data", "build"):
        m = re.search(rf"APIS_{role.upper()}=\((.*?)\)", text, re.S)
        assert m, role
        out[role] = set(m.group(1).split())
    return out


def test_bootstrap_enables_exactly_the_terraform_apis() -> None:
    assert _bootstrap_apis() == _tf_apis()


def _env_names(text: str, block: str) -> set[str]:
    """``MAPI_*`` keys assigned inside the ``block = {`` map of ``locals.tf``."""
    start = text.index(f"  {block} = {{")
    depth, i = 0, text.index("{", start)
    for j in range(i, len(text)):
        depth += {"{": 1, "}": -1}.get(text[j], 0)
        if depth == 0:
            return set(re.findall(r"\b(MAPI_[A-Z0-9_]+)\s*=", text[i:j]))
    raise AssertionError(f"unbalanced {block}")


def test_server_workloads_get_only_real_settings() -> None:
    """Every variable Terraform hands mapi-server (plain or from Secret Manager) is a
    Settings field, so a renamed setting cannot silently fall back to its default in the
    cloud. The web image's variables are nginx template inputs, not server settings."""
    from mapi_server.settings import Settings

    known = {
        (f.validation_alias if isinstance(f.validation_alias, str) else f"MAPI_{n}").upper()
        for n, f in Settings.model_fields.items()
    } | {f"MAPI_{n.upper()}" for n in Settings.model_fields}
    locals_tf = strip_comments((TF_ROOT / "modules" / "control" / "locals.tf").read_text())
    names = (
        _env_names(locals_tf, "env_common")
        | _env_names(locals_tf, "secret_env_api")
        | set(re.findall(r"\b(MAPI_[A-Z0-9_]+)\s*=", locals_tf.split("  env = {", 1)[1]))
    )
    web_only = {"MAPI_API_UPSTREAM", "MAPI_WEB_SERVER_NAMES", "MAPI_GCS_ENDPOINT"}
    unknown = sorted(names - known - web_only)
    assert not unknown, f"not mapi_server settings: {unknown}"
    # the essentials of a multi-tenant cloud deployment are all there
    for required in (
        "MAPI_PROFILE", "MAPI_OBJECT_STORE", "MAPI_SIGNER", "MAPI_KMS_KEY_VERSION",
        "MAPI_URL_SIGNER", "MAPI_SERVER_SECRET", "MAPI_METRICS_TOKEN",
        "MAPI_PUBSUB_PUSH_AUDIENCE", "MAPI_PUBSUB_PUSH_SERVICE_ACCOUNT",
        "MAPI_DATABASE_APP_URL", "MAPI_DATABASE_SYSTEM_URL", "MAPI_DB_SYSTEM_ROLE",
        "MAPI_DATABASE_GATEWAY_URL", "MAPI_DB_GATEWAY_ROLE",
        "MAPI_CLOUDRUN_PROJECT", "MAPI_EXECUTOR",
    ):  # fmt: skip
        assert required in names, required
