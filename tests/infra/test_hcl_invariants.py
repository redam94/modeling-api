"""Static checks of infra/terraform: the security invariants hold in the HCL itself, and the
names the platform relies on agree with the mapi_spec contracts.

These run everywhere (no terraform binary, no providers). The mocked-apply suites under
``infra/terraform/modules/*/tests`` check the same properties on evaluated values; see
``test_terraform.py``.
"""

from __future__ import annotations

import re

import pytest
from mapi_spec.api import ALL_ENDPOINTS, ENDPOINTS
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
        "MAPI_CLOUDRUN_PROJECT", "MAPI_EXECUTOR", "MAPI_TRUSTED_PROXIES",
    ):  # fmt: skip
        assert required in names, required


def test_create_only_uploads_have_their_own_identity() -> None:
    """Every run's output policy is create_only, which GcsObjectStore signs as
    MAPI_URL_UPLOADER: Terraform must create that account (signBlob for api and orchestrator
    only), hand it to every server workload, and grant it create (never delete) on the two
    buckets written through upload policies."""
    iam = strip_comments((TF_ROOT / "modules" / "control" / "iam.tf").read_text())
    assert re.search(r'url-uploader\s*=\s*\{\s*id\s*=\s*"\$\{local\.prefix\}-uploader"', iam)
    signblob = re.search(
        r'resource "google_service_account_iam_member" "url_signer" \{(.*?)\n\}', iam, re.S
    )
    assert signblob, "signBlob grants exist"
    assert '"url-signer"' in signblob.group(1) and '"url-uploader"' in signblob.group(1)
    assert '["api", "orchestrator"]' in signblob.group(1)

    locals_tf = strip_comments((TF_ROOT / "modules" / "control" / "locals.tf").read_text())
    assert {"MAPI_URL_SIGNER", "MAPI_URL_UPLOADER"} <= _env_names(locals_tf, "env_common")
    assert 'google_service_account.sa["url-uploader"].email' in locals_tf

    data_iam = strip_comments((TF_ROOT / "modules" / "data" / "iam.tf").read_text())
    grants = re.findall(r'\["(\w+)", "(\w+)", "url_uploader"\]', data_iam)
    assert sorted(grants) == [("quarantine", "writer"), ("runs", "writer")], grants
    writer = _one(_module_blocks("data"), "google_project_iam_custom_role", "object_writer")
    assert "storage.objects.delete" not in writer.body, "the uploader can never overwrite"

    stack = strip_comments((TF_ROOT / "modules" / "stack" / "main.tf").read_text())
    assert re.search(
        r'url_uploader\s*=\s*module\.control\.service_accounts\["url-uploader"\]', stack
    )
    assert re.search(
        r'url-uploader\s*=\s*module\.control\.service_accounts\["url-uploader"\]', stack
    )


def _cel(expression: str, name: str) -> bool:
    """Evaluate the small IAM-condition subset the checkpoint grant uses on ``resource.name``:
    ``&&`` of ``resource.name.startsWith("...")`` and ``resource.name.extract("a{x}b") != ""``
    (extract: the text between the first ``a`` and the next ``b`` after it; empty if absent)."""
    for term in (t.strip() for t in expression.split("&&")):
        if m := re.fullmatch(r'resource\.name\.startsWith\("([^"]*)"\)', term):
            ok = name.startswith(m.group(1))
        elif m := re.fullmatch(r'resource\.name\.extract\("([^"{]*)\{\w+\}([^"]*)"\) != ""', term):
            before, after = m.group(1), m.group(2)
            i = name.find(before)
            rest = name[i + len(before) :] if i >= 0 else ""
            j = rest.find(after) if after else len(rest)
            ok = i >= 0 and j > 0
        else:
            raise AssertionError(f"unsupported condition term: {term!r}")
        if not ok:
            return False
    return True


def test_checkpoints_can_be_overwritten_and_nothing_else() -> None:
    """Segmented sampling rewrites the same checkpoint names every segment through a POST
    policy signed as url_signer. GCS needs storage.objects.delete to overwrite, so url_signer
    holds delete on the runs bucket, but only under ``.../checkpoints/``: manifests and
    outputs stay write-once."""
    from mapi_runner.worker.checkpoints import checkpoint_files
    from mapi_spec.run import MANIFEST_OBJECT_NAME
    from mapi_spec.storage import run_checkpoint_prefix, run_prefix

    blocks = _module_blocks("data")
    role = _one(blocks, "google_project_iam_custom_role", "checkpoint_overwriter")
    assert set(re.findall(r'"(storage\.[\w.]+)"', role.body)) == {"storage.objects.delete"}
    grant = _one(blocks, "google_storage_bucket_iam_member", "checkpoint_overwrite")
    assert 'google_storage_bucket.this["runs"].name' in grant.body
    assert "google_project_iam_custom_role.checkpoint_overwriter.id" in grant.body
    assert "var.access.url_signer" in grant.body
    raw = grant.attr("expression")
    assert raw and raw.startswith('"') and raw.endswith('"'), "the delete grant is conditional"
    expression = raw[1:-1].replace('\\"', '"')
    bucket = "mapi-prod-runs"
    expression = expression.replace('${google_storage_bucket.this["runs"].name}', bucket)

    def obj(name: str) -> str:
        return f"projects/_/buckets/{bucket}/objects/{name}"

    for prefix in (
        run_checkpoint_prefix("t1", "r1", 1),
        run_checkpoint_prefix("t1", "r1", 3, task_index=7),
    ):
        for f in checkpoint_files("fit-0"):
            assert _cel(expression, obj(prefix + f)), prefix + f
    for name in (
        run_prefix("t1", "r1", 1) + MANIFEST_OBJECT_NAME,
        run_prefix("t1", "r1", 1) + "bundle.zarr.zip",
        run_prefix("t1", "r1", 1, task_index=2) + "out/checkpoints.json",
        "checkpoints/t/x",
    ):
        assert not _cel(expression, obj(name)), name
    assert not _cel(expression, "projects/_/buckets/mapi-prod-bundles/objects/t/a/checkpoints/x")
    # the plain grants never include delete for the signer
    data_iam = strip_comments((TF_ROOT / "modules" / "data" / "iam.tf").read_text())
    signer = re.findall(r'\["(\w+)", "(\w+)", "url_signer"\]', data_iam)
    assert all(r in {"reader", "writer"} for _, r in signer), signer


def test_only_the_orchestrator_holds_the_platform_database_login() -> None:
    """Plan §9 (IDOR row): the role that passes row-level security is for the dispatcher,
    reconciler and finalizer. The internet-facing api and events services run their platform
    sessions as the gateway role (migration 0005) and never get the orchestrator's URL."""
    secrets_tf = strip_comments((TF_ROOT / "modules" / "control" / "secrets.tf").read_text())
    access = dict(re.findall(r'"(db-url-[a-z-]+)"\s*=\s*(\[[^\]]*\])', secrets_tf))
    assert access["db-url-orchestrator"] == '["orchestrator"]'
    assert access["db-url-gateway"] == '["api", "events"]'
    sql = strip_comments((TF_ROOT / "modules" / "control" / "sql.tf").read_text())
    assert 'db_roles = toset(["app", "gateway", "orchestrator", "migrate"])' in sql

    locals_tf = strip_comments((TF_ROOT / "modules" / "control" / "locals.tf").read_text())
    start = locals_tf.index("  secret_env_api = {")
    api_secrets = locals_tf[start : locals_tf.index("}", start)]
    assert "db-url-orchestrator" not in api_secrets
    assert re.search(r'MAPI_DATABASE_GATEWAY_URL\s*=\s*"db-url-gateway"', api_secrets)
    assert re.search(r'MAPI_DB_GATEWAY_ROLE\s*=\s*"mapi_gateway"', locals_tf)


def test_the_api_identity_only_reads_objects() -> None:
    """The API never writes or deletes objects (signed PUTs act as url_signer; copies and
    deletes are the orchestrator's), so the internet-facing identity holds read roles only:
    it cannot overwrite or delete a tenant's pending uploads."""
    iam = strip_comments((TF_ROOT / "modules" / "data" / "iam.tf").read_text())
    grants = re.findall(r'\["(\w+)", "(\w+)", "api"\]', iam)
    assert grants, "the api has bucket grants"
    assert all(role in {"reader", "objectViewer"} for _, role in grants), grants
    assert ("quarantine", "reader") in grants, "stat of an upload by name, no list"


def test_user_output_is_excluded_at_the_exec_projects_default_sink() -> None:
    """Plan §8.7: user stdout above the platform's needs is excluded at the sink. The
    supervisor turns user output into ``log`` events (and coalesced ``progress`` events);
    those still reach the events service through the routing sink, but the exec project's
    ``_Default`` bucket (hostile tenants' volume, billed per GiB) does not ingest them."""
    blocks = _module_blocks("exec")
    exclusion = _one(blocks, "google_logging_project_exclusion", "user_output")
    assert "var.project_id" in exclusion.body
    flt = exclusion.body
    assert 'resource.type=\\"cloud_run_job\\"' in flt
    assert "^mapi-${var.env}-" in flt
    for event_type in ("log", "progress"):
        assert f'jsonPayload.mapi_event.type=\\"{event_type}\\"' in flt, event_type
    for kept in ("state", "error", "complete", "warning"):
        assert f'\\"{kept}\\"' not in flt, f"{kept} events stay in the project's logs"


def test_every_server_workload_ships_its_metrics_to_cloud_monitoring() -> None:
    """The orchestrator records the dispatch, reconcile and finalize series in its own
    process: it must serve them, and each server workload needs a collector that scrapes
    /metrics (with the bearer) and writes to Managed Service for Prometheus. Nothing else
    reaches /metrics in the cloud (the load balancer routes only /v1, /auth and health)."""
    locals_tf = strip_comments((TF_ROOT / "modules" / "control" / "locals.tf").read_text())
    orchestrator = locals_tf.split("    orchestrator = merge(", 1)[1].split("}, lookup(", 1)[0]
    assert re.search(r'MAPI_WORKER_METRICS_PORT\s*=\s*"?\d+', orchestrator)
    assert re.search(r'MAPI_WORKER_METRICS_HOST\s*=\s*"127\.0\.0\.1"', orchestrator)
    assert "googlemanagedprometheus" in locals_tf
    assert "${env:MAPI_METRICS_TOKEN}" in locals_tf  # the collector presents the bearer
    run_tf = strip_comments((TF_ROOT / "modules" / "control" / "run.tf").read_text())
    for workload in ("api", "events", "orchestrator"):
        assert f"local.collector_config.{workload}" in run_tf, workload
    variables = (TF_ROOT / "modules" / "control" / "variables.tf").read_text()
    block = re.search(r'variable "metrics_collector" \{(.*?)\n\}', variables, re.S)
    assert block and "sha256:[0-9a-f]{64}" in block.group(1), "the collector is digest-pinned"
    for env in ENVS:
        main = (TF_ROOT / "envs" / env / "main.tf").read_text()
        assert "var.metrics_collector_image" in main, env


# ------------------------------------------------------------------------------ Cloud Armor


def test_cloud_armor_guards_every_load_balancer_backend() -> None:
    """Plan §14 M3 (Cloud Armor): the policy is attached to every backend of the external
    load balancer, carries the OWASP CRS rules (sqli, xss, lfi, rce, scanner detection) in a
    preview-then-enforce mode, and Adaptive Protection is on by default."""
    blocks = _module_blocks("control")
    backend = _one(blocks, "google_compute_backend_service", "run")
    assert re.search(r"security_policy\s*=\s*local\.security_policy", backend.body)
    policy = _one(blocks, "google_compute_security_policy", "edge")
    assert 'type        = "CLOUD_ARMOR"' in policy.body
    assert "count       = local.armor" in policy.body
    armor = strip_comments((TF_ROOT / "modules" / "control" / "armor.tf").read_text())
    for rule in ("sqli", "xss", "lfi", "rce", "scannerdetection"):
        assert f'"{rule}-v33-stable"' in armor, rule
    assert 'preview     = var.cloud_armor.waf_mode == "preview"' in policy.body
    assert "evaluatePreconfiguredWaf(" in policy.body and 'action      = "deny(403)"' in policy.body
    assert "layer_7_ddos_defense_config" in policy.body
    assert 'json_parsing                 = "STANDARD"' in policy.body
    variables = (TF_ROOT / "modules" / "control" / "variables.tf").read_text()
    block = re.search(r'variable "cloud_armor" \{(.*?)\n\}', variables, re.S)
    assert block is not None
    assert 'waf_mode                     = optional(string, "preview")' in block.group(1)
    assert "adaptive_protection          = optional(bool, true)" in block.group(1)
    assert "request_body_inspection_size" in block.group(1)
    assert "priority    = 2147483647" in policy.body  # an explicit default rule


def test_cloud_armor_rate_bans_cover_the_abuse_prone_routes() -> None:
    """Per-IP rate-based bans on sign-up, /auth/*, public embeds and endpoint scoring, on
    paths the API really serves (the M3 contract)."""
    armor = strip_comments((TF_ROOT / "modules" / "control" / "armor.tf").read_text())
    rules = re.findall(r"expression\s*=\s*\"(request\.path[^\"]+)\"", armor)
    joined = " ".join(rules)
    paths = {e.path for e in ALL_ENDPOINTS}
    assert "/auth/signup" in paths and "request.path.startsWith('/auth/signup')" in joined
    assert "request.path.startsWith('/auth/')" in joined
    assert any(p.startswith("/v1/embeds/") for p in paths)
    assert "request.path.startsWith('/v1/embeds/')" in joined
    assert "/v1/endpoints/{alias}/score" in paths
    score = re.search(r"request\.path\.matches\('([^']+)'\)", joined)
    assert score and re.fullmatch(score.group(1), "/v1/endpoints/geo-mmm-prod/score")
    assert not re.fullmatch(score.group(1), "/v1/endpoints/geo-mmm-prod/schema")
    assert armor.count('action      = "rate_based_ban"') == 1  # one dynamic rule, four entries
    assert 'enforce_on_key   = "IP"' in armor and 'exceed_action    = "deny(429)"' in armor
    lb = (TF_ROOT / "modules" / "control" / "lb.tf").read_text()
    for prefix in ('"/v1/*"', '"/auth/*"'):  # the rules see these paths: the LB routes them
        assert prefix in lb


def test_prod_rolls_cloud_armor_out_in_preview_first() -> None:
    prod = (TF_ROOT / "envs" / "prod" / "main.tf").read_text()
    assert "cloud_armor        = { waf_mode = var.cloud_armor_waf_mode }" in prod
    variables = (TF_ROOT / "envs" / "prod" / "variables.tf").read_text()
    block = re.search(r'variable "cloud_armor_waf_mode" \{(.*?)\n\}', variables, re.S)
    assert block and 'default     = "preview"' in block.group(1)
    stack = (TF_ROOT / "modules" / "stack" / "main.tf").read_text()
    assert 'cloud_armor              = lookup(var.control, "cloud_armor", {})' in stack


def test_log_retention_is_explicit_because_deletion_does_not_purge_logs() -> None:
    """Tenant deletion purges Postgres and object storage, not Cloud Logging: the control
    project's _Default bucket has an explicit, bounded retention instead."""
    bucket = _one(_module_blocks("control"), "google_logging_project_bucket_config", "default")
    assert 'bucket_id      = "_Default"' in bucket.body
    assert "retention_days = var.log_retention_days" in bucket.body
    variables = (TF_ROOT / "modules" / "control" / "variables.tf").read_text()
    block = re.search(r'variable "log_retention_days" \{(.*?)\n\}', variables, re.S)
    assert block and "default     = 30" in block.group(1) and "3650" in block.group(1)
    assert "does not purge" in strip_comments(block.group(1)) or "not purge" in block.group(1)
