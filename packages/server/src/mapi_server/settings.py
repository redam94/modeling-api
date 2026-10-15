"""Server configuration (``pydantic-settings``; every field reads ``MAPI_<NAME>``).

The defaults describe a local, single-tenant install that matches
:mod:`mapi_spec.localdev`: API on ``127.0.0.1:8000``, the GCS shim on ``127.0.0.1:4443``,
an embedded Postgres (``pgserver``) under ``MAPI_DATA_DIR`` unless ``DATABASE_URL`` is set.
"""

from __future__ import annotations

import hashlib
import shlex
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

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

__all__ = ["ExecutorName", "ObjectStoreKind", "Profile", "Settings", "local_image_digest"]


class Profile(StrEnum):
    """Deployment profile. The ``local-process`` executor refuses ``multi-tenant``."""

    SINGLE_TENANT = "single-tenant"
    MULTI_TENANT = "multi-tenant"


class ExecutorName(StrEnum):
    LOCAL_PROCESS = "local-process"
    DOCKER = "docker"
    DOCKER_GVISOR = "docker-gvisor"


class ObjectStoreKind(StrEnum):
    SHIM = "shim"  # mapi_gcs_shim.ShimObjectStore against MAPI_GCS_ENDPOINT
    MEMORY = "memory"  # in-process store (tests; signed URLs only work through its ASGI app)


def local_image_digest(lock_path: Path) -> str:
    """``sha256:<hex>`` of ``uv.lock``: the image digest of the local environment."""
    return "sha256:" + hashlib.sha256(lock_path.read_bytes()).hexdigest()


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
    exec_token: str | None = Field(
        None,
        repr=False,
        description="Bearer token of the shim's exec principal (local executors only)",
    )
    control_token: str | None = Field(
        None, repr=False, description="Bearer token of the shim's control-plane principal"
    )

    # ----------------------------------------------------------------------------- executors
    executor: ExecutorName = ExecutorName.LOCAL_PROCESS
    runner_command: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["mapi-runner"],
        description="argv of the supervisor (shell-quoted string in the environment)",
    )
    docker_image: str = "mapi/base-bayes:local"
    docker_network: str = Field(
        "none",
        description="'none' (no network) or an internal network that only contains the shim",
    )
    docker_runtime_gvisor: str = "runsc"
    executor_workdir: Path | None = None
    cancel_grace_s: float = Field(10.0, ge=0)

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
    signing_key_path: Path | None = None

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

    @field_validator("env")
    @classmethod
    def _env(cls, v: str) -> str:
        Buckets(v)
        return v

    @model_validator(mode="after")
    def _origins(self) -> Settings:
        if self.web_origin.rstrip("/") == self.plots_origin.rstrip("/"):
            raise ValueError("the plot sandbox must be served from a different origin")
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
