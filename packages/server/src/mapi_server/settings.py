"""Server configuration (``pydantic-settings``; every field reads ``MAPI_<NAME>``).

The defaults describe a local, single-tenant install that matches
:mod:`mapi_spec.localdev`: API on ``127.0.0.1:8000``, the GCS shim on ``127.0.0.1:4443``,
an embedded Postgres (``pgserver``) under ``MAPI_DATA_DIR`` unless ``DATABASE_URL`` is set.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from mapi_spec.executor import RunKind
from mapi_spec.localdev import (
    API_PORT,
    DEFAULT_ENV,
    GCS_SHIM_PORT,
    LOCALHOST,
    PLOT_SANDBOX_PORT,
    WEB_PORT,
)
from mapi_spec.storage import Buckets
from mapi_spec.tenancy import DEFAULT_RESIDENCY, TrustTier, residency_of_region
from mapi_spec.trust import SHARED_EMAIL_DOMAINS
from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from mapi_server import __version__
from mapi_server.clientaddr import normalise_trusted_proxies

__all__ = [
    "DEFAULT_CLOUDRUN_BASES",
    "DEFAULT_DEV_USERS",
    "DEFAULT_DOCKER_IMAGES",
    "DEFAULT_ESTIMATE_WALL_S",
    "ExecutorName",
    "MailerKind",
    "ObjectStoreKind",
    "Profile",
    "Settings",
    "SignerKind",
    "checkout_image_digest",
    "local_image_digest",
]

DEFAULT_CLOUDRUN_BASES: dict[str, str] = {
    "mmm": "base-bayes",
    "sci": "base-bayes",
    "tabular": "base-bayes",
    "sklearn": "base-ml",
    "ts": "base-forecast",
    "causal": "base-forecast",
}
"""Plugin namespace → the base whose Cloud Run Jobs ship it (mirrors the docker images)."""

DEFAULT_DOCKER_IMAGES: dict[str, str] = {
    "mmm": f"mapi/base-bayes:{__version__}",
    "sci": f"mapi/base-bayes:{__version__}",
    "tabular": f"mapi/base-bayes:{__version__}",
    "sklearn": f"mapi/base-ml:{__version__}",
    "ts": f"mapi/base-forecast:{__version__}",
    "causal": f"mapi/base-forecast:{__version__}",
}
"""Plugin namespace → the runner image ``make docker-build`` tags that ships it."""

DEFAULT_DEV_USERS: tuple[str, ...] = ("admin@dev.local", "analyst@dev.local", "viewer@dev.local")
"""Accounts of the development identity provider: the first is a platform operator and the
admin of the ``default`` tenant, the second a ``runner`` and the third a ``viewer`` of the
``default`` project."""

DEFAULT_ESTIMATE_WALL_S: dict[str, float] = {
    RunKind.FIT.value: 300.0,
    RunKind.SCORE.value: 30.0,
    RunKind.INGEST.value: 60.0,
    RunKind.DESCRIBE.value: 60.0,
    RunKind.VALIDATE.value: 30.0,
    RunKind.EVALUATE.value: 120.0,
    RunKind.DERIVE.value: 60.0,
    RunKind.RENDER.value: 30.0,
    RunKind.OPTIMIZE.value: 300.0,
    RunKind.TRANSLATE_PRIORS.value: 60.0,
    RunKind.EXPLORE.value: 60.0,
    RunKind.GOVERNANCE.value: 900.0,
    RunKind.INSPECT.value: 15.0,
}
"""Wall-time estimate per run kind (seconds, capped at the run's timeout): what a run is
charged against vCPU-second quotas until its attempts are metered into the cost ledger."""


class Profile(StrEnum):
    """Deployment profile.

    * ``single-tenant`` (default): one ``default`` tenant and project; requests without
      credentials act as the local administrator (the API listens on loopback only);
    * ``multi-tenant``: every non-public route needs a session or an API token, and the
      ``local-process`` executor is refused;
    * ``dev``: multi-tenant semantics with the built-in development identity provider
      (:mod:`mapi_server.devidp`), so the whole stack, sign-in included, works offline.
    """

    SINGLE_TENANT = "single-tenant"
    MULTI_TENANT = "multi-tenant"
    DEV = "dev"


class ExecutorName(StrEnum):
    LOCAL_PROCESS = "local-process"
    DOCKER = "docker"
    DOCKER_GVISOR = "docker-gvisor"
    CLOUDRUN_JOBS = "cloudrun-jobs"


class SignerKind(StrEnum):
    """Who signs bundle manifests: a local P-256 key file, or a Cloud KMS key version."""

    LOCAL = "local"
    KMS = "kms"


class MailerKind(StrEnum):
    """How the server sends e-mail (sign-up verification links)."""

    CONSOLE = "console"  # log the message (single-tenant installs; never multi-tenant)
    OUTBOX = "outbox"  # write JSON files under MAPI_OUTBOX_DIR (dev; read at /dev/outbox)
    SMTP = "smtp"  # smtplib with STARTTLS or implicit TLS (production)
    NONE = "none"  # no e-mail: self-service sign-up is off


class ObjectStoreKind(StrEnum):
    SHIM = "shim"  # mapi_gcs_shim.ShimObjectStore against MAPI_GCS_ENDPOINT
    MEMORY = "memory"  # in-process store (tests; signed URLs only work through its ASGI app)
    GCS = "gcs"  # real Cloud Storage (mapi_gcs_shim.GcsObjectStore; the `gcp` extra)


def local_image_digest(lock_path: Path) -> str:
    """``sha256:<hex>`` of ``uv.lock``: the image digest of an environment built from it."""
    return "sha256:" + hashlib.sha256(lock_path.read_bytes()).hexdigest()


CHECKOUT_SOURCES = ("packages/*/src", "plugins/*/src")
"""The workspace sources a source checkout's catalog is made from (steps, models, plots and
the runner that describes them), relative to the directory of ``uv.lock``."""


def checkout_image_digest(lock_path: Path) -> str:
    """The image digest of a source checkout (``make dev``): ``uv.lock`` plus every file
    under :data:`CHECKOUT_SOURCES` (path and content).

    A code change that leaves ``uv.lock`` alone (a new recipe, an About card, a fixed step)
    changes the catalog, so it must change the digest too, or the catalog cached under the
    old digest is served forever. Without workspace sources next to the lock this is
    :func:`local_image_digest`."""
    root = lock_path.parent
    files = sorted(
        p
        for pattern in CHECKOUT_SOURCES
        for src in root.glob(pattern)
        for p in src.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix not in {".pyc", ".pyo"}
    )
    if not files:
        return local_image_digest(lock_path)
    h = hashlib.sha256(lock_path.read_bytes())
    for p in files:
        h.update(p.relative_to(root).as_posix().encode())
        h.update(b"\0")
        h.update(hashlib.sha256(p.read_bytes()).digest())
    return "sha256:" + h.hexdigest()


def _default_runner_command() -> list[str]:
    """``mapi-runner`` next to this interpreter (works without the venv on ``PATH``)."""
    sibling = Path(sys.executable).parent / "mapi-runner"
    return [str(sibling)] if sibling.is_file() else ["mapi-runner"]


def _find_uv_lock(start: Path) -> Path | None:
    for parent in (start, *start.parents):
        candidate = parent / "uv.lock"
        if candidate.is_file():
            return candidate
    return None


class Settings(BaseSettings):
    """All server settings. Construct with keyword arguments in tests."""

    model_config = SettingsConfigDict(
        env_prefix="MAPI_", extra="ignore", populate_by_name=True, validate_default=True
    )

    # ------------------------------------------------------------------ identity / topology
    env: str = Field(DEFAULT_ENV, description="Selects bucket names mapi-{env}-*")
    profile: Profile = Profile.SINGLE_TENANT
    tenant_id: str = "default"
    project_id: str = "default"
    debug: bool = Field(False, description="Include tracebacks in problem.untrusted_text")

    api_host: str = LOCALHOST
    api_port: int = API_PORT
    api_url: str = f"http://{LOCALHOST}:{API_PORT}"
    web_origin: str = f"http://{LOCALHOST}:{WEB_PORT}"
    plots_origin: str = f"http://{LOCALHOST}:{PLOT_SANDBOX_PORT}"
    data_dir: Path = Path(".local")
    allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [LOCALHOST, "localhost", "api"],
        description="Host header values the API answers (comma-separated in the environment; "
        "'*' turns the check off). The host of api_url is always allowed. The API is "
        "unauthenticated in M1, so this is what stops DNS-rebinding pages from using it.",
    )

    # ------------------------------------------------------------------------------ database
    database_url: str | None = Field(
        None,
        validation_alias=AliasChoices("MAPI_DATABASE_URL", "DATABASE_URL", "database_url"),
        description="postgresql:// URL; unset = embedded pgserver under data_dir/pg",
    )
    db_pool_size: int = Field(10, ge=1, le=200)
    db_system_pool_size: int = Field(5, ge=1, le=200)
    db_gateway_pool_size: int = Field(5, ge=1, le=200)
    db_echo: bool = False
    database_app_url: str | None = Field(
        None,
        repr=False,
        description="Login of the request-path role (row-level security applies). Unset: "
        "connect with DATABASE_URL and SET ROLE db_app_role on every connection",
    )
    database_system_url: str | None = Field(
        None,
        repr=False,
        description="Login of the platform role: the orchestrator's dispatcher, reconciler, "
        "finalizer and housekeeper (bypasses row-level security; the API never needs it). "
        "Unset: DATABASE_URL with SET ROLE db_system_role",
    )
    database_gateway_url: str | None = Field(
        None,
        repr=False,
        description="Login of the gateway role: what the API and events service look up "
        "before a tenant is known (sign-in, memberships, operator metadata, pushed events; "
        "row-level security applies). Unset: DATABASE_URL with SET ROLE db_gateway_role",
    )
    db_app_role: str = Field("mapi_app", pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    db_system_role: str = Field("mapi_system", pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    db_gateway_role: str = Field("mapi_gateway", pattern=r"^[a-z_][a-z0-9_]{0,62}$")

    # ------------------------------------------------------------------------------- storage
    object_store: ObjectStoreKind = ObjectStoreKind.SHIM
    gcs_endpoint: str = f"http://{LOCALHOST}:{GCS_SHIM_PORT}"
    gcs_public_endpoint: str | None = Field(
        None,
        description="Base URL put into signed URLs when executions/browsers reach the shim "
        "under another host name (default: gcs_endpoint)",
    )
    gcp_project: str | None = Field(
        None, description="Google Cloud project of the Cloud Storage client (MAPI_OBJECT_STORE=gcs)"
    )
    url_signer: str | None = Field(
        None,
        description="Service account that signs Cloud Storage URLs through IAM signBlob "
        "(keyless; required with MAPI_OBJECT_STORE=gcs)",
    )
    url_uploader: str | None = Field(
        None,
        description="Service account that signs create-only upload policies (objects.create, "
        "never delete; required with MAPI_OBJECT_STORE=gcs and distinct from url_signer)",
    )
    gcs_root: Path | None = Field(
        None, description="The GCS shim's state directory (default: data_dir/gcs)"
    )
    gcs_key_file: Path | None = Field(
        None,
        description="Key file of the shim's control principal "
        "(default: gcs_root/_shim/keys/control.json)",
    )
    exec_token: str | None = Field(
        None,
        repr=False,
        description="Bearer token of the shim's exec principal (local executors only; default: "
        "read from gcs_root/_shim/keys/exec.json)",
    )

    # ----------------------------------------------------------------------------- executors
    executor: ExecutorName = ExecutorName.LOCAL_PROCESS
    runner_command: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: _default_runner_command(),
        description="argv of the supervisor (shell-quoted string in the environment)",
    )
    docker_image: str = Field(
        f"mapi/base-bayes:{__version__}",
        description="Runner image for runs without a mapped plugin (ingest, describe, ...)",
    )
    docker_images: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_DOCKER_IMAGES),
        description="Plugin namespace → runner image (JSON in the environment)",
    )
    docker_network: str = Field(
        "none",
        description="Network of run containers: 'container:<gcs-shim container>' (share the "
        "shim's namespace, so 127.0.0.1:4443 works) or an internal network holding only the "
        "shim. 'none' is refused while the bootstrap URL is HTTP (nothing would be reachable).",
    )
    docker_network_isolated: bool = Field(
        False,
        description="Declare MAPI_DOCKER_NETWORK an internal network that holds only the GCS "
        "shim (no route out, no DNS beyond it), so docker-gvisor reports enforced egress and "
        "DNS. Shared tiers still need a passing security-suite run recorded for these caps",
    )
    docker_runtime_gvisor: str = "runsc"
    executor_workdir: Path | None = None
    cancel_grace_s: float = Field(10.0, ge=0)

    # ------------------------------------------------------------------------ Cloud Run Jobs
    cloudrun_project: str | None = Field(
        None, description="Exec project that holds the Cloud Run Job inventory (cloudrun-jobs)"
    )
    cloudrun_region: str = Field("europe-west1", description="Region of the Job inventory")
    default_residency: str | None = Field(
        None,
        pattern=r"^(?:[a-z]{2,6}|[a-z]+-[a-z]+[0-9]{1,2})$",
        description="Data residency of the platform tenant, of tenants adopted by the "
        "migrations and of tenants created without one (default: the multi-region of "
        "MAPI_CLOUDRUN_REGION for cloudrun-jobs, e.g. eu, else us)",
    )
    cloudrun_job_template: str = Field(
        "mapi-{env}-{base}-{class}",
        description="Job id convention; {env}, {base} (base-bayes|base-ml|base-forecast) and "
        "{class} (s|m|l) are substituted. Must match the Jobs Terraform creates",
    )
    cloudrun_plugin_bases: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_CLOUDRUN_BASES),
        description="Plugin namespace → base image family (JSON in the environment)",
    )
    cloudrun_default_base: str = Field(
        "base-bayes", description="Base for runs without a mapped plugin (ingest, describe)"
    )
    cloudrun_submits_per_minute: float = Field(
        150.0,
        gt=0,
        le=180,
        description="Token-bucket rate of jobs.run calls per orchestrator; the regional quota "
        "is 180/min [verify M0], so split it between orchestrator replicas",
    )
    cloudrun_submit_burst: int = Field(10, ge=1)
    cloudrun_max_429_retries: int = Field(6, ge=0, description="429 answers before giving up")
    cloudrun_backoff_base_s: float = Field(1.0, gt=0)
    cloudrun_backoff_cap_s: float = Field(64.0, gt=0)
    cloudrun_timeout_margin_s: int = Field(
        120, ge=0, description="Task timeout = run timeout + margin (the supervisor flushes)"
    )
    cloudrun_egress_enforced: bool = Field(
        True, description="The exec network denies all egress except the restricted VIP"
    )
    cloudrun_dns_enforced: bool = Field(
        False,
        description="The DNS response policy is verified to apply to Jobs [verify M0]; "
        "verified and public tenants are only placed when both egress and DNS are enforced "
        "(and suite evidence is recorded)",
    )
    cloudrun_adopt_lookback_s: float = Field(
        26 * 3600, gt=0, description="How far back orphan adoption lists executions"
    )

    # ------------------------------------------------------------------ Pub/Sub push events
    pubsub_push_audience: str | None = Field(
        None,
        description="Expected audience of the Pub/Sub push OIDC token (usually the push "
        "endpoint URL). When set, POST /internal/events requires a Google-signed token",
    )
    pubsub_push_service_account: str | None = Field(
        None, description="Service account the push subscription signs its tokens as"
    )
    pubsub_push_subscriptions: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="Accepted subscription names (comma-separated; empty = any)",
    )
    google_jwks_url: str = Field(
        "https://www.googleapis.com/oauth2/v3/certs",
        description="JWKS of Google's OIDC signing keys (push token verification)",
    )

    # ------------------------------------------------------------- dispatcher / reconciler
    dispatch_rate_per_minute: float = Field(180.0, gt=0)
    dispatch_burst: int = Field(10, ge=1)
    dispatch_batch: int = Field(10, ge=1, le=1000)
    dispatch_poll_s: float = Field(1.0, gt=0)
    dispatch_lease_s: float = Field(120.0, gt=0)
    dispatch_tiers: Annotated[list[TrustTier], NoDecode] = Field(
        default_factory=list,
        description="Trust tiers whose runs this orchestrator leases (comma-separated); "
        "empty: every tier. Give each orchestrator a disjoint list when several serve "
        "different exec pools, so tenant tiers never share a pool",
    )
    reconcile_interval_s: float = Field(10.0, gt=0)
    stall_intervals: int = Field(15, ge=2, description="K: silence > K x progress interval")
    complete_event_grace_s: float = Field(60.0, ge=0)
    lost_execution_grace_s: float = Field(60.0, ge=0)
    max_infra_retries: int = Field(2, ge=0)
    auto_retry_window_s: float = Field(1800.0, ge=0, description="No auto-retry beyond 30 min")
    auto_retry_cost_factor: float = Field(
        2.0,
        ge=0,
        description="No auto-retry once a run's metered cost reaches this multiple of its "
        "estimate (plan §8.3: retries capped by cost)",
    )
    finalize_max_attempts: int = Field(5, ge=1)
    deadline_grace_s: float = Field(60.0, ge=0)
    housekeeping_interval_s: float = Field(
        300.0, gt=0, description="Sweep of expired upload intents and other expiring rows"
    )
    tracking_retention_days: int | None = Field(
        None,
        ge=1,
        le=36500,
        description="Days after a run finishes that its metric points and user log lines are "
        "kept (unset: kept with the run). Summaries, params, tags and notes are always kept",
    )

    # ---------------------------------------------------------------------- catalog / signing
    catalog_path: Path | None = Field(
        None, description="Catalog JSON exported at image build (runner describe)"
    )
    image_digest: str | None = Field(None, pattern=r"^sha256:[0-9a-f]{64}$")
    uv_lock_path: Path | None = None
    signing_key_path: Path | None = Field(
        None,
        description="PEM of the bundle-signing key (default: data_dir/keys/"
        "bundle-signing-p256.pem); every process that signs or verifies bundles must share it",
    )
    signing_trusted_key_paths: Annotated[list[Path], NoDecode] = Field(
        default_factory=list,
        description="PEM files (public or private) of retired bundle-signing keys whose "
        "signatures still verify (comma-separated): key rotation without invalidating the "
        "bundles the old key signed",
    )
    signing_key_create: bool = Field(
        True,
        description="Create the signing key when it is missing; off where `mapi-server "
        "migrate` creates it on shared storage (compose api/worker)",
    )

    signer: SignerKind = Field(
        SignerKind.LOCAL, description="local = PEM key file; kms = Cloud KMS asymmetric key"
    )
    kms_key_version: str | None = Field(
        None,
        description="projects/…/cryptoKeys/…/cryptoKeyVersions/N that signs bundle manifests",
    )
    kms_trusted_key_versions: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="Older key versions whose signatures still verify (comma-separated)",
    )

    # ------------------------------------------------------------------------------ API misc
    internal_token: str | None = Field(
        None, repr=False, description="If set, POST /internal/events requires this bearer"
    )

    # -------------------------------------------------------------- authentication (M2)
    auth_required: bool | None = Field(
        None,
        description="Require a session or API token on every non-public route (default: "
        "required in every profile except single-tenant)",
    )
    server_secret: str | None = Field(
        None,
        repr=False,
        description="Root secret of the token pepper and audit fingerprints (default: read "
        "or create server-secret next to the bundle-signing key). Changing it invalidates "
        "every API token.",
    )
    server_secret_path: Path | None = None
    cookie_secure: bool | None = Field(
        None, description="Secure, __Host- prefixed cookies (default: on when api_url is https)"
    )
    oidc_issuer: str | None = Field(
        None, description="OpenID Connect issuer (e.g. https://accounts.google.com)"
    )
    oidc_discovery_url: str | None = Field(
        None, description="Default: <issuer>/.well-known/openid-configuration"
    )
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = Field(None, repr=False)
    oidc_redirect_uri: str | None = Field(
        None, description="Registered redirect URI (default: <api_url>/auth/callback)"
    )
    oidc_algorithms: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["RS256", "ES256"]
    )
    oidc_cli_client_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["mapi-cli"],
        description="Client ids of public OIDC clients (the mapi CLI) whose ID tokens of the "
        "same issuer are accepted as bearers (comma-separated; empty: API tokens only)",
    )
    oidc_extra_params: dict[str, str] = Field(
        default_factory=dict,
        description='Extra authorization parameters, e.g. {"hd": "acme.com"} (JSON)',
    )
    operator_emails: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="Verified e-mails that sign in as platform operators (comma-separated)",
    )
    default_tenant_admins: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="E-mails invited as admins of the default tenant at start-up "
        "(comma-separated; the invitation is claimed at first sign-in)",
    )
    devidp_users: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_DEV_USERS),
        description="Accounts of the development identity provider (MAPI_PROFILE=dev only)",
    )

    # ---------------------------------------------------------------- quotas and cost (M2)
    price_table_path: Path | None = Field(
        None, description="JSON PriceTable (default: mapi_spec.cost.DEFAULT_PRICE_TABLE)"
    )
    estimate_wall_s: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_ESTIMATE_WALL_S))
    rate_limits_enabled: bool = True
    lease_candidates: int = Field(
        500, ge=1, le=10_000, description="Queued runs the fair-share planner considers"
    )
    lease_candidates_per_tenant: int = Field(
        50,
        ge=1,
        le=10_000,
        description="At most this many of them per tenant (a tenant whose runs are held "
        "cannot fill the window)",
    )
    executor_report_interval_s: float = Field(10.0, gt=0)
    executor_evidence_max_age_s: float = Field(
        3 * 86400.0,
        gt=0,
        description="How long a recorded conformance or security-suite report lets an "
        "executor take verified and public tenants (the nightly suites renew it)",
    )
    sse_heartbeat_s: float = Field(15.0, gt=0)
    sse_poll_s: float = Field(5.0, gt=0, description="Fallback poll when no NOTIFY arrives")
    sse_reauth_s: float = Field(
        60.0,
        gt=0,
        description="How often an open event stream re-checks its caller's credential "
        "(a revoked token, an expired session or a lost membership closes the stream)",
    )
    max_sidecar_bytes: int = Field(8 * 1024 * 1024, ge=1024)
    log_level: Literal["debug", "info", "warning", "error"] = "info"

    # ------------------------------------------------------- plot rendering and embeds (M3)
    render_timeout_s: int = Field(
        300,
        ge=30,
        le=3600,
        description="Wall-clock limit of a POST /v1/plots:render run (the sandbox kills it)",
    )
    inspect_timeout_s: int = Field(
        300,
        ge=30,
        le=3600,
        description="Wall-clock limit of an upload inspect run (M4; it reads only metadata)",
    )
    render_plotly_enabled: bool = Field(
        False,
        description="Accept Plotly plots in POST /v1/plots:render. Plotly static export needs "
        "kaleido and a Chrome/Chromium in the runner image, which the base images do not "
        "ship; while off, Plotly renders are refused with MODE_NOT_SUPPORTED",
    )
    embed_rate_per_minute: int = Field(
        300, ge=1, description="Public embed views per minute and embed link (429 beyond)"
    )
    embed_ip_rate_per_minute: int = Field(
        120,
        ge=1,
        description="Public embed requests per minute and client address, valid or not",
    )
    embed_cache_s: float = Field(
        60.0,
        ge=0,
        description="How long an API instance reuses an embed's resolved plot (revocation and "
        "expiry are still checked on every request; responses are never cached by browsers "
        "or shared caches, so a revoked link stops rendering on the next load)",
    )
    embed_image_url_ttl_s: int = Field(
        900, ge=60, le=86400, description="Lifetime of the PNG fallback's signed URL"
    )

    # ------------------------------------------------------------------- observability
    log_json: bool = Field(
        True, description="Emit JSON logs (structlog); off gives a human console renderer"
    )
    trusted_proxies: Annotated[list[str] | None, NoDecode] = Field(
        None,
        description="Addresses or CIDR ranges of the proxies in front of the API (load "
        "balancer, Cloud Run front end, the web container's /v1/ proxy), comma-separated. A "
        "request from one of them takes the right-most untrusted X-Forwarded-For hop as the "
        "client address (per-address limits, Sybil checks, audit). 'none': clients reach the "
        "API directly and X-Forwarded-For is ignored. Required in the multi-tenant profile",
    )
    metrics_enabled: bool = Field(True, description="Mount GET /metrics (Prometheus)")
    worker_metrics_port: int | None = Field(
        None,
        ge=1,
        le=65535,
        description="Serve GET /metrics of the worker (dispatcher, reconciler, finalizer, "
        "events) on this port; unset: the worker exposes no metrics",
    )
    worker_metrics_host: str = Field(
        "127.0.0.1", description="Bind address of the worker's metrics endpoint"
    )
    metrics_token: str | None = Field(
        None,
        repr=False,
        description="Bearer a scraper must present on GET /metrics; required in the "
        "multi-tenant profile (the series carry tenant ids and spend)",
    )
    tracing_enabled: bool = Field(True, description="Install the OpenTelemetry tracer provider")
    otlp_endpoint: str | None = Field(
        None,
        description="OTLP/HTTP collector base URL for span export (also read from "
        "OTEL_EXPORTER_OTLP_ENDPOINT); unset means spans are created but not exported",
    )

    # ------------------------------------------------------------ webhooks (M3, plan §9)
    webhook_allow_http: bool = Field(
        False,
        description="Development only: allow http:// webhook URLs, for the hosts in "
        "MAPI_WEBHOOK_DEV_ALLOWLIST only (refused in the multi-tenant profile)",
    )
    webhook_dev_allowlist: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="Development only: webhook host names (comma-separated) exempt from the "
        "SSRF address and port checks, e.g. a receiver on localhost (refused in the "
        "multi-tenant profile)",
    )
    webhook_ca_file: Path | None = Field(
        None,
        description="Extra PEM CA bundle trusted for webhook receivers (a private CA); the "
        "system trust store is always used",
    )
    webhook_poll_interval_s: float = Field(
        2.0, gt=0, description="How often the worker looks for due webhook deliveries"
    )
    webhook_max_attempts: int = Field(
        8, ge=1, le=8, description="Tries per delivery (the retry schedule allows up to 8)"
    )
    webhook_auto_disable_after: int = Field(
        20,
        ge=1,
        le=1000,
        description="Disable a webhook after this many consecutive events failed",
    )
    webhook_max_per_project: int = Field(25, ge=1, le=1000)
    webhook_retention_days: int = Field(
        30, ge=1, le=3650, description="Days webhook events and their delivery logs are kept"
    )

    # ------------------------------------------------------------------- workflows (M3.5)
    workflow_tick_s: float = Field(
        1.0, gt=0, description="How often the orchestrator advances active workflow runs"
    )
    workflow_max_active_runs: int = Field(
        20,
        ge=1,
        le=10_000,
        description="Workflow runs a project may have going at once, sub-workflow runs included",
    )
    workflow_max_schedules: int = Field(
        20, ge=0, le=1000, description="Active cron schedules per project"
    )
    workflow_max_triggers: int = Field(
        20, ge=0, le=1000, description="Active dataset triggers per project"
    )
    workflow_node_timeout_s: int = Field(
        1800, ge=60, le=24 * 3600, description="Timeout of a node without its own timeout"
    )
    workflow_max_json_output_bytes: int = Field(
        1024 * 1024, ge=1024, le=16 * 1024 * 1024, description="Largest JSON output of a node"
    )

    # ------------------------------------------------------------- trust lifecycle (M3)
    signup_enabled: bool | None = Field(
        None,
        description="Self-service sign-up at POST /auth/signup (default: on in the dev and "
        "multi-tenant profiles when a mailer is configured, off single-tenant)",
    )
    mailer: MailerKind | None = Field(
        None,
        description="How e-mail is sent: console, outbox, smtp or none (default: outbox in "
        "dev, console single-tenant, smtp multi-tenant)",
    )
    mail_from: str = Field(
        "modeling-api <no-reply@mapi.localhost>",
        max_length=320,
        description="From: header of the e-mails the server sends",
    )
    outbox_dir: Path | None = Field(
        None, description="Where the outbox mailer writes messages (default: data_dir/outbox)"
    )
    smtp_host: str | None = Field(None, description="SMTP server (MAPI_MAILER=smtp)")
    smtp_port: int = Field(587, ge=1, le=65535)
    smtp_tls: Literal["starttls", "ssl"] = Field(
        "starttls", description="starttls (port 587) or ssl (implicit TLS, port 465)"
    )
    smtp_username: str | None = None
    smtp_password: str | None = Field(None, repr=False)
    smtp_timeout_s: float = Field(10.0, gt=0)
    signup_verify_url: str | None = Field(
        None,
        description="Verification link template with {token} (default: "
        "<web_origin>/verify-email?token={token})",
    )
    signup_blocked_domains: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="E-mail domains refused at sign-up besides the built-in disposable list "
        "(comma-separated; subdomains included)",
    )
    signup_blocklist_path: Path | None = Field(
        None,
        description="A file of disposable e-mail domains (one per line, # comments), e.g. a "
        "maintained public list, added to the built-in one",
    )
    signup_per_ip_per_hour: int = Field(
        5, ge=1, description="Sign-ups accepted per client address and hour"
    )
    signup_per_domain_per_day: int = Field(
        25,
        ge=1,
        description="Verified sign-ups per e-mail domain and day (shared free-mail domains "
        "are exempt)",
    )
    signup_global_per_hour: int = Field(
        500, ge=1, description="Verified sign-ups per hour on the whole platform"
    )
    signup_shared_email_domains: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(SHARED_EMAIL_DOMAINS),
        description="Free-mail domains shared by unrelated people (comma-separated; replaces "
        "the built-in list): no per-domain sign-up cap and no domain-burst Sybil flag",
    )
    verify_per_ip_per_minute: int = Field(
        20, ge=1, description="E-mail verification attempts per client address and minute"
    )
    signup_sybil_window_days: int = Field(
        30, ge=1, description="Look-back of the Sybil heuristics (same address, browser, e-mail)"
    )
    signup_sybil_threshold: int = Field(
        3,
        ge=1,
        description="Workspaces created from one address, browser or e-mail within the window "
        "before a new one is flagged for review",
    )
    signup_flagged_suspend: bool = Field(
        False,
        description="Suspend a flagged sign-up's tenant until an operator approves it "
        "(default: it works and waits in the review queue)",
    )
    signup_retention_days: int = Field(
        30, ge=1, description="Unverified sign-ups (with their e-mail) are deleted after this"
    )
    public_code_upload_enabled: bool = Field(
        False,
        description="Cut line B: let identity-verified public tenants run uploaded code "
        "(off through M4: public tenants run built-ins and declarative specs only)",
    )
    tenant_deletion_grace_s: float = Field(
        7 * 86400.0,
        ge=0,
        description="Grace period of a tenant deletion: the tenant is suspended and may cancel",
    )
    deletion_reauth_max_age_s: float | None = Field(
        None,
        gt=0,
        description="Also require a sign-in at most this old to request a deletion (re-auth); "
        "unset: the confirmation phrase alone",
    )
    deletion_run_stop_timeout_s: float = Field(
        3600.0,
        gt=0,
        description="How long a purge waits for the tenant's cancelled runs to stop",
    )
    deletion_max_attempts: int = Field(
        8, ge=1, description="Automatic retries of a failed purge before operators must retry"
    )
    bundle_retention_days: int = Field(
        30,
        ge=0,
        description="The bundles bucket's retention period (Terraform bundle_retention_days): "
        "when a purge may retry objects a retention lock refused",
    )
    lifecycle_interval_s: float = Field(
        15.0, gt=0, description="Cadence of the deletion and export scheduler (orchestrator)"
    )
    export_max_inline_bytes: int = Field(
        256 * 1024 * 1024,
        ge=0,
        description="Dataset bytes a GDPR export archive includes; larger exports list the "
        "remaining objects (with sizes and hashes) instead",
    )
    export_retention_s: float = Field(
        7 * 86400.0, gt=0, description="How long a GDPR export archive is kept"
    )
    export_link_ttl_s: float = Field(
        900.0, gt=0, le=7 * 86400, description="Lifetime of an export's signed download link"
    )
    exports_per_day: int = Field(3, ge=1, description="GDPR exports a tenant may request per day")
    export_max_metadata_bytes: int = Field(
        256 * 1024 * 1024,
        ge=0,
        description="Bytes of metadata rows (runs, logs, events, audit, ...) a GDPR export "
        "archive includes; the tables cut short are listed in its manifest",
    )
    export_build_timeout_s: float = Field(
        3600.0,
        gt=0,
        description="How long one GDPR export build may take; a build still marked running "
        "5 minutes after that (its process died) is built again once, then failed",
    )

    def obs_settings(self) -> Any:
        """The :class:`~mapi_server.observability.ObsSettings` for this process."""
        from mapi_server.observability import ObsSettings

        return ObsSettings(
            service_name="mapi-server",
            service_version=__version__,
            log_level=self.log_level,
            log_json=self.log_json,
            tracing_enabled=self.tracing_enabled,
            otlp_endpoint=self.otlp_endpoint,
        )

    @field_validator("runner_command", mode="before")
    @classmethod
    def _argv(cls, v: object) -> object:
        if isinstance(v, str):
            parts = shlex.split(v)
            if not parts:
                raise ValueError("runner_command must not be empty")
            return parts
        return v

    @field_validator(
        "allowed_hosts",
        "oidc_algorithms",
        "oidc_cli_client_ids",
        "operator_emails",
        "default_tenant_admins",
        "devidp_users",
        "pubsub_push_subscriptions",
        "kms_trusted_key_versions",
        "signing_trusted_key_paths",
        "dispatch_tiers",
        "webhook_dev_allowlist",
        "signup_blocked_domains",
        "signup_shared_email_domains",
        mode="before",
    )
    @classmethod
    def _hosts(cls, v: object) -> object:
        if isinstance(v, str):
            return [h.strip() for h in v.split(",") if h.strip()]
        return v

    @field_validator("trusted_proxies", mode="before")
    @classmethod
    def _proxies(cls, v: object) -> object:
        if isinstance(v, str):
            if v.strip().lower() == "none":
                return []
            v = v.split(",")
        if isinstance(v, list | tuple):
            return normalise_trusted_proxies(str(x) for x in v)
        return v

    @field_validator("env")
    @classmethod
    def _env(cls, v: str) -> str:
        Buckets(v)
        return v

    @model_validator(mode="after")
    def _origins(self) -> Settings:
        if self.web_origin.rstrip("/") == self.plots_origin.rstrip("/"):
            raise ValueError("the plot sandbox must be served from a different origin")
        if len({self.db_app_role, self.db_system_role, self.db_gateway_role}) != 3:
            raise ValueError("db_app_role, db_system_role and db_gateway_role must differ")
        if self.profile is not Profile.SINGLE_TENANT and self.auth_required is False:
            raise ValueError("authentication cannot be turned off in a multi-tenant profile")
        if (self.pubsub_push_audience is None) != (self.pubsub_push_service_account is None):
            raise ValueError(
                "set MAPI_PUBSUB_PUSH_AUDIENCE and MAPI_PUBSUB_PUSH_SERVICE_ACCOUNT together"
            )
        if self.signer is SignerKind.KMS and not self.kms_key_version:
            raise ValueError("MAPI_SIGNER=kms needs MAPI_KMS_KEY_VERSION")
        if self.object_store is ObjectStoreKind.GCS and not self.url_signer:
            raise ValueError("MAPI_OBJECT_STORE=gcs needs MAPI_URL_SIGNER (the signBlob account)")
        if self.object_store is ObjectStoreKind.GCS and (
            not self.url_uploader or self.url_uploader == self.url_signer
        ):
            # Every run's output policy is create_only; GCS POST forms carry no generation
            # precondition, so only an account without storage.objects.delete can sign one.
            raise ValueError(
                "MAPI_OBJECT_STORE=gcs needs MAPI_URL_UPLOADER: a service account other than "
                "MAPI_URL_SIGNER that may create objects but never delete them"
            )
        if self.profile is Profile.MULTI_TENANT and (
            self.webhook_allow_http or self.webhook_dev_allowlist
        ):
            raise ValueError(
                "MAPI_WEBHOOK_ALLOW_HTTP and MAPI_WEBHOOK_DEV_ALLOWLIST are for development; "
                "the multi-tenant profile always applies the full SSRF policy"
            )
        if (
            self.profile is Profile.MULTI_TENANT
            and self.signup_open
            and self.effective_mailer in (MailerKind.CONSOLE, MailerKind.OUTBOX)
        ):
            raise ValueError(
                "multi-tenant sign-up needs MAPI_MAILER=smtp: console and outbox mailers would "
                "expose verification links to whoever reads the logs or the disk"
            )
        if self.signup_enabled and not self._mailer_usable:
            raise ValueError(
                "MAPI_SIGNUP_ENABLED needs a mailer: MAPI_MAILER=smtp with MAPI_SMTP_HOST "
                "(or console/outbox outside the multi-tenant profile)"
            )
        weak = [a for a in self.oidc_algorithms if a.upper().startswith("HS") or a == "none"]
        if weak:
            raise ValueError("oidc_algorithms must be asymmetric (RS*, ES*, PS* or EdDSA)")
        if self.profile is Profile.MULTI_TENANT and self.trusted_proxies is None:
            raise ValueError(
                "the multi-tenant profile needs MAPI_TRUSTED_PROXIES: the proxies in front of "
                "the API (so per-address limits see the caller, not the load balancer), or "
                "'none' when clients connect to the API directly"
            )
        return self

    # ------------------------------------------------------------------------- derived
    @property
    def effective_mailer(self) -> MailerKind:
        """``MAPI_MAILER``, else the profile's default (dev: outbox, multi-tenant: smtp)."""
        if self.mailer is not None:
            return self.mailer
        return {
            Profile.DEV: MailerKind.OUTBOX,
            Profile.MULTI_TENANT: MailerKind.SMTP,
        }.get(self.profile, MailerKind.CONSOLE)

    @property
    def signup_open(self) -> bool:
        """Is self-service sign-up on (and can it send its e-mail)?"""
        enabled = (
            self.signup_enabled
            if self.signup_enabled is not None
            else self.profile is not Profile.SINGLE_TENANT
        )
        return enabled and self._mailer_usable

    @property
    def _mailer_usable(self) -> bool:
        kind = self.effective_mailer
        return kind is not MailerKind.NONE and (kind is not MailerKind.SMTP or bool(self.smtp_host))

    def resolved_outbox_dir(self) -> Path:
        return self.outbox_dir or (self.data_dir / "outbox")

    def verify_link(self, token: str) -> str:
        """The verification link e-mailed to a sign-up."""
        template = self.signup_verify_url or (
            self.web_origin.rstrip("/") + "/verify-email?token={token}"
        )
        return template.replace("{token}", token)

    @property
    def buckets(self) -> Buckets:
        return Buckets(self.env)

    @property
    def multi_tenant(self) -> bool:
        """Every profile except ``single-tenant`` (sign-in and tenant isolation are live)."""
        return self.profile is not Profile.SINGLE_TENANT

    @property
    def requires_auth(self) -> bool:
        """Must every non-public request carry a session or an API token?"""
        if self.auth_required is not None:
            return self.auth_required
        return self.multi_tenant

    @property
    def devidp_enabled(self) -> bool:
        """The built-in development identity provider (``MAPI_PROFILE=dev`` only)."""
        return self.profile is Profile.DEV

    @property
    def secure_cookies(self) -> bool:
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.api_url.startswith("https://")

    @property
    def secret_path(self) -> Path:
        """Next to the bundle-signing key by default: both live on durable, shared storage."""
        return self.server_secret_path or (self.key_path.parent / "server-secret")

    def effective_default_residency(self) -> str:
        """``default_residency``, else what the Cloud Run inventory's region satisfies (the
        executor must be able to place the tenants created by default), else ``us``."""
        if self.default_residency:
            return self.default_residency
        if self.executor is ExecutorName.CLOUDRUN_JOBS:
            return residency_of_region(self.cloudrun_region)
        return DEFAULT_RESIDENCY

    def effective_operator_emails(self) -> list[str]:
        """``operator_emails`` plus, in the dev profile, the first development account."""
        emails = [e.strip().lower() for e in self.operator_emails if e.strip()]
        if self.devidp_enabled and self.devidp_users:
            emails.append(self.devidp_users[0].strip().lower())
        return emails

    def redirect_uri(self) -> str:
        """The OIDC redirect URI (``<api_url>/auth/callback`` unless configured)."""
        return self.oidc_redirect_uri or f"{self.api_url.rstrip('/')}/auth/callback"

    def allowed_origins(self) -> set[str]:
        """Origins whose cookie-authenticated mutations pass the CSRF origin check."""
        out: set[str] = set()
        for url in (self.web_origin, self.api_url):
            parts = urlsplit(url)
            if parts.scheme and parts.netloc:
                out.add(f"{parts.scheme}://{parts.netloc}")
        return out

    @property
    def pg_dir(self) -> Path:
        return self.data_dir / "pg"

    @property
    def workdir(self) -> Path:
        return self.executor_workdir or (self.data_dir / "executions")

    @property
    def key_path(self) -> Path:
        return self.signing_key_path or (self.data_dir / "keys" / "bundle-signing-p256.pem")

    def trusted_hosts(self) -> list[str]:
        """``allowed_hosts`` plus the host of ``api_url`` (what TrustedHostMiddleware gets)."""
        hosts = list(dict.fromkeys(self.allowed_hosts))
        api_host = urlsplit(self.api_url).hostname
        if api_host and api_host not in hosts:
            hosts.append(api_host)
        return hosts

    def resolved_gcs_root(self) -> Path:
        return self.gcs_root or (self.data_dir / "gcs")

    def gcs_key_path(self, principal: str) -> Path:
        """Key file of a shim principal (``control`` honours ``MAPI_GCS_KEY_FILE``)."""
        if principal == "control" and self.gcs_key_file is not None:
            return self.gcs_key_file
        return self.resolved_gcs_root() / "_shim" / "keys" / f"{principal}.json"

    def resolved_exec_token(self) -> str | None:
        """``MAPI_EXEC_TOKEN``, else the token in the shim's ``exec`` key file (shim store)."""
        if self.exec_token is not None or self.object_store is not ObjectStoreKind.SHIM:
            return self.exec_token
        path = self.gcs_key_path("exec")
        if not path.is_file():
            return None
        token = json.loads(path.read_text()).get("token")
        return str(token) if token else None

    def bootstrap_url_template(self) -> str:
        """``MAPI_BOOTSTRAP_URL`` handed to local executions (the GCS shim JSON API)."""
        return f"{self.gcs_endpoint.rstrip('/')}/storage/v1/b/{self.buckets.bootstrap}/o/{{nonce}}"

    def resolve_image_digest(self) -> str:
        """The image digest the catalog is cached under: ``image_digest``, else the
        ``sha256`` of ``uv_lock_path`` (the images bake their lock), else the source
        checkout's digest (:func:`checkout_image_digest` of the ``uv.lock`` above the
        working directory: ``make dev`` describes again after a code change)."""
        if self.image_digest:
            return self.image_digest
        if self.uv_lock_path is not None:
            if not self.uv_lock_path.is_file():
                raise RuntimeError(f"MAPI_UV_LOCK_PATH {self.uv_lock_path} is not a file")
            return local_image_digest(self.uv_lock_path)
        lock = _find_uv_lock(Path.cwd())
        if lock is None:
            raise RuntimeError(
                "cannot determine the local image digest: set MAPI_IMAGE_DIGEST or "
                "MAPI_UV_LOCK_PATH (no uv.lock found above the working directory)"
            )
        return checkout_image_digest(lock)
