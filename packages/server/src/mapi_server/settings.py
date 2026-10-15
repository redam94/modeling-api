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
from typing import Annotated, Literal
from urllib.parse import urlsplit

from mapi_spec.localdev import (
    API_PORT,
    DEFAULT_ENV,
    GCS_SHIM_PORT,
    LOCALHOST,
    PLOT_SANDBOX_PORT,
    WEB_PORT,
)
from mapi_spec.storage import Buckets
from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from mapi_server import __version__

__all__ = [
    "DEFAULT_CLOUDRUN_BASES",
    "DEFAULT_DOCKER_IMAGES",
    "ExecutorName",
    "ObjectStoreKind",
    "Profile",
    "Settings",
    "SignerKind",
    "local_image_digest",
]

DEFAULT_CLOUDRUN_BASES: dict[str, str] = {
    "mmm": "base-bayes",
    "tabular": "base-bayes",
    "sklearn": "base-ml",
    "ts": "base-forecast",
}
"""Plugin namespace → the base whose Cloud Run Jobs ship it (mirrors the docker images)."""

DEFAULT_DOCKER_IMAGES: dict[str, str] = {
    "mmm": f"mapi/base-bayes:{__version__}",
    "tabular": f"mapi/base-bayes:{__version__}",
    "sklearn": f"mapi/base-ml:{__version__}",
    "ts": f"mapi/base-forecast:{__version__}",
}
"""Plugin namespace → the runner image ``make docker-build`` tags that ships it."""


class Profile(StrEnum):
    """Deployment profile. The ``local-process`` executor refuses ``multi-tenant``."""

    SINGLE_TENANT = "single-tenant"
    MULTI_TENANT = "multi-tenant"


class ExecutorName(StrEnum):
    LOCAL_PROCESS = "local-process"
    DOCKER = "docker"
    DOCKER_GVISOR = "docker-gvisor"
    CLOUDRUN_JOBS = "cloudrun-jobs"


class SignerKind(StrEnum):
    """Who signs bundle manifests: a local P-256 key file, or a Cloud KMS key version."""

    LOCAL = "local"
    KMS = "kms"


class ObjectStoreKind(StrEnum):
    SHIM = "shim"  # mapi_gcs_shim.ShimObjectStore against MAPI_GCS_ENDPOINT
    MEMORY = "memory"  # in-process store (tests; signed URLs only work through its ASGI app)


def local_image_digest(lock_path: Path) -> str:
    """``sha256:<hex>`` of ``uv.lock``: the image digest of the local environment."""
    return "sha256:" + hashlib.sha256(lock_path.read_bytes()).hexdigest()


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
    db_echo: bool = False

    # ------------------------------------------------------------------------------- storage
    object_store: ObjectStoreKind = ObjectStoreKind.SHIM
    gcs_endpoint: str = f"http://{LOCALHOST}:{GCS_SHIM_PORT}"
    gcs_public_endpoint: str | None = Field(
        None,
        description="Base URL put into signed URLs when executions/browsers reach the shim "
        "under another host name (default: gcs_endpoint)",
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
    docker_runtime_gvisor: str = "runsc"
    executor_workdir: Path | None = None
    cancel_grace_s: float = Field(10.0, ge=0)

    # ------------------------------------------------------------------------ Cloud Run Jobs
    cloudrun_project: str | None = Field(
        None, description="Exec project that holds the Cloud Run Job inventory (cloudrun-jobs)"
    )
    cloudrun_region: str = Field("europe-west1", description="Region of the Job inventory")
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
        description="The DNS response policy is verified to apply to Jobs [verify M0]; public "
        "tenants are only placed when both egress and DNS are enforced",
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
    reconcile_interval_s: float = Field(10.0, gt=0)
    stall_intervals: int = Field(15, ge=2, description="K: silence > K x progress interval")
    complete_event_grace_s: float = Field(60.0, ge=0)
    lost_execution_grace_s: float = Field(60.0, ge=0)
    max_infra_retries: int = Field(2, ge=0)
    auto_retry_window_s: float = Field(1800.0, ge=0, description="No auto-retry beyond 30 min")
    finalize_max_attempts: int = Field(5, ge=1)
    deadline_grace_s: float = Field(60.0, ge=0)

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
    sse_heartbeat_s: float = Field(15.0, gt=0)
    sse_poll_s: float = Field(5.0, gt=0, description="Fallback poll when no NOTIFY arrives")
    max_sidecar_bytes: int = Field(8 * 1024 * 1024, ge=1024)
    log_level: Literal["debug", "info", "warning", "error"] = "info"

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
        "allowed_hosts", "pubsub_push_subscriptions", "kms_trusted_key_versions", mode="before"
    )
    @classmethod
    def _hosts(cls, v: object) -> object:
        if isinstance(v, str):
            return [h.strip() for h in v.split(",") if h.strip()]
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
        if (self.pubsub_push_audience is None) != (self.pubsub_push_service_account is None):
            raise ValueError(
                "set MAPI_PUBSUB_PUSH_AUDIENCE and MAPI_PUBSUB_PUSH_SERVICE_ACCOUNT together"
            )
        if self.signer is SignerKind.KMS and not self.kms_key_version:
            raise ValueError("MAPI_SIGNER=kms needs MAPI_KMS_KEY_VERSION")
        return self

    # ------------------------------------------------------------------------- derived
    @property
    def buckets(self) -> Buckets:
        return Buckets(self.env)

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
        """The image digest the catalog is cached under (``sha256`` of ``uv.lock`` locally)."""
        if self.image_digest:
            return self.image_digest
        lock = self.uv_lock_path or _find_uv_lock(Path.cwd())
        if lock is None or not lock.is_file():
            raise RuntimeError(
                "cannot determine the local image digest: set MAPI_IMAGE_DIGEST or "
                "MAPI_UV_LOCK_PATH (no uv.lock found above the working directory)"
            )
        return local_image_digest(lock)
