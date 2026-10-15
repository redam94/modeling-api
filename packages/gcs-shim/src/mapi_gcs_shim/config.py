"""Shim configuration: buckets, principals (service accounts) and their key files.

Layout of a shim root (``--root``, default ``.local/gcs``)::

    {root}/_shim/config.json        buckets + principals (tokens and HMAC secrets), mode 0600
    {root}/_shim/keys/{id}.json     one key file per principal (what clients load), mode 0600
    {root}/_shim/shim.lock          exclusive lock held by the running server
    {root}/tmp/                     staging area for atomic writes
    {root}/buckets/{bucket}/...     object data and metadata sidecars

On first use :func:`init_root` writes a default configuration for an environment
(``mapi-{env}-{quarantine,bootstrap,runs,bundles,data}`` and the ``control``/``exec``
principals) with freshly generated secrets.
"""

from __future__ import annotations

import base64
import contextlib
import hmac
import json
import os
import re
import secrets
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, Literal

from mapi_spec.localdev import DEFAULT_ENV
from mapi_spec.storage import BUCKET_RE, Buckets
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from mapi_gcs_shim.iam import ANY_BUCKET, Binding, Permission

__all__ = [
    "CONFIG_DIR",
    "DEFAULT_MAX_OBJECT_BYTES",
    "KEY_FILE_TYPE",
    "PRINCIPAL_CONTROL",
    "PRINCIPAL_EXEC",
    "WRITE_ONCE_RETENTION_SECONDS",
    "BucketConfig",
    "PrincipalConfig",
    "ShimConfig",
    "ShimCredentials",
    "atomic_write",
    "check_bucket_name",
    "config_path",
    "default_config",
    "default_root",
    "init_root",
    "key_file_path",
    "load_config",
    "save_config",
    "write_key_files",
]

CONFIG_DIR: Final = "_shim"
CONFIG_FILE: Final = "config.json"
KEYS_DIR: Final = "keys"
KEY_FILE_TYPE: Final = "mapi_gcs_shim_key"

PRINCIPAL_CONTROL: Final = "control"
"""Control plane / finalizer: ``roles/storage.admin`` on every bucket."""
PRINCIPAL_EXEC: Final = "exec"
"""Execution identity: get + delete on the bootstrap bucket only (no list, no bundles)."""

WRITE_ONCE_RETENTION_SECONDS: Final = 100 * 365 * 86400
"""A locked retention period this long makes a bucket effectively write-once."""

DEFAULT_MAX_OBJECT_BYTES: Final = 5 * 1024**3

_RESERVED_BUCKETS: Final = frozenset({"storage", "upload", "download"})
_PRINCIPAL_ID_RE: Final = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_EMAIL_RE: Final = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,190}$")


def check_bucket_name(name: str) -> str:
    """Validate a bucket name (GCS rules, plus names reserved by the shim's own routes).

    Bucket names become one filesystem path component, so this is also the traversal guard.
    """
    if not re.match(BUCKET_RE, name):
        raise ValueError(f"invalid bucket name {name!r}: must match {BUCKET_RE}")
    if ".." in name or name.startswith("goog") or "google" in name:
        raise ValueError(f"invalid bucket name {name!r}: '..', 'goog*' and 'google' are reserved")
    if name in _RESERVED_BUCKETS:
        raise ValueError(f"bucket name {name!r} is reserved by the shim's routes")
    return name


class BucketConfig(BaseModel):
    """Policy for one bucket: retention (write-once) and lifecycle (age-based delete)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    retention_period_seconds: int | None = Field(None, gt=0)
    retention_locked: bool = False
    lifecycle_age_seconds: int | None = Field(None, gt=0)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return check_bucket_name(v)

    @model_validator(mode="after")
    def _lock(self) -> BucketConfig:
        if self.retention_locked and self.retention_period_seconds is None:
            raise ValueError("a locked retention policy needs retention_period_seconds")
        return self

    @property
    def write_once(self) -> bool:
        """Objects can never be deleted or overwritten (locked, effectively infinite period)."""
        return (
            self.retention_locked
            and self.retention_period_seconds is not None
            and self.retention_period_seconds >= WRITE_ONCE_RETENTION_SECONDS
        )


class PrincipalConfig(BaseModel):
    """A service account: a bearer token for the JSON API and an HMAC secret for signing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    email: str
    token: SecretStr
    secret: SecretStr
    bindings: tuple[Binding, ...] = ()

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _PRINCIPAL_ID_RE.match(v):
            raise ValueError(f"principal id {v!r} must match {_PRINCIPAL_ID_RE.pattern}")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        if not _EMAIL_RE.match(v):
            raise ValueError(f"principal email {v!r} is not a valid service-account email")
        return v

    @field_validator("token", "secret")
    @classmethod
    def _secret(cls, v: SecretStr) -> SecretStr:
        if len(v.get_secret_value()) < 16:
            raise ValueError("tokens and secrets must be at least 16 characters")
        return v

    def permissions(self, bucket: str | None) -> frozenset[Permission]:
        """Permissions on ``bucket`` (``None`` = project-level, i.e. ``"*"`` bindings only)."""
        out: frozenset[Permission] = frozenset()
        for b in self.bindings:
            if b.applies_to(bucket) and (bucket is not None or b.bucket == ANY_BUCKET):
                out |= b.granted()
        return out

    def can(self, permission: Permission, bucket: str | None) -> bool:
        return permission in self.permissions(bucket)


class ShimConfig(BaseModel):
    """The whole shim configuration (persisted as ``_shim/config.json``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    env: str = DEFAULT_ENV
    buckets: tuple[BucketConfig, ...] = ()
    principals: tuple[PrincipalConfig, ...] = ()
    clock_skew_seconds: int = Field(300, ge=0, le=3600)
    max_object_bytes: int = Field(DEFAULT_MAX_OBJECT_BYTES, ge=1)

    @field_validator("env")
    @classmethod
    def _env(cls, v: str) -> str:
        Buckets(v)  # validates the environment name
        return v

    @model_validator(mode="after")
    def _unique(self) -> ShimConfig:
        for what, values in (
            ("bucket names", [b.name for b in self.buckets]),
            ("principal ids", [p.id for p in self.principals]),
            ("principal emails", [p.email for p in self.principals]),
            ("principal tokens", [p.token.get_secret_value() for p in self.principals]),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{what} must be unique")
        return self

    def principal(self, principal_id: str) -> PrincipalConfig:
        for p in self.principals:
            if p.id == principal_id:
                return p
        raise KeyError(f"unknown principal {principal_id!r}")

    def by_email(self, email: str) -> PrincipalConfig | None:
        for p in self.principals:
            if p.email == email:
                return p
        return None

    def by_token(self, token: str) -> PrincipalConfig | None:
        """Constant-time lookup of the principal owning a bearer token."""
        found: PrincipalConfig | None = None
        given = token.encode()
        for p in self.principals:
            if hmac.compare_digest(p.token.get_secret_value().encode(), given):
                found = p
        return found

    def to_json_dict(self) -> dict[str, Any]:
        """JSON-able form **including secrets** (only ever written to a 0600 file)."""
        data = self.model_dump(mode="json")
        for out, p in zip(data["principals"], self.principals, strict=True):
            out["token"] = p.token.get_secret_value()
            out["secret"] = p.secret.get_secret_value()
        return data


def _new_token() -> str:
    return "shim-" + secrets.token_urlsafe(32)


def _new_secret() -> str:
    # Like a GCS HMAC secret: 40 base64 characters (30 random bytes).
    return base64.b64encode(secrets.token_bytes(30)).decode()


def default_config(env: str = DEFAULT_ENV) -> ShimConfig:
    """The plan §8.6 buckets and the two platform principals, with fresh secrets."""
    b = Buckets(env)
    day = 86400
    buckets = (
        BucketConfig(name=b.quarantine, lifecycle_age_seconds=7 * day),
        BucketConfig(name=b.bootstrap, lifecycle_age_seconds=1 * day),
        BucketConfig(name=b.runs),
        BucketConfig(
            name=b.bundles,
            retention_period_seconds=WRITE_ONCE_RETENTION_SECONDS,
            retention_locked=True,
        ),
        BucketConfig(name=b.data),
    )
    domain = f"mapi-{env}.iam.gserviceaccount.com"
    principals = (
        PrincipalConfig(
            id=PRINCIPAL_CONTROL,
            email=f"{PRINCIPAL_CONTROL}@{domain}",
            token=SecretStr(_new_token()),
            secret=SecretStr(_new_secret()),
            bindings=(Binding(bucket=ANY_BUCKET, role="roles/storage.admin"),),
        ),
        PrincipalConfig(
            id=PRINCIPAL_EXEC,
            email=f"{PRINCIPAL_EXEC}@{domain}",
            token=SecretStr(_new_token()),
            secret=SecretStr(_new_secret()),
            bindings=(
                Binding(
                    bucket=b.bootstrap,
                    permissions=(Permission.OBJECTS_GET, Permission.OBJECTS_DELETE),
                ),
            ),
        ),
    )
    return ShimConfig(env=env, buckets=buckets, principals=principals)


# ----------------------------------------------------------------------------- persistence


def atomic_write(path: Path, data: bytes, *, mode: int = 0o644) -> None:
    """Write ``data`` to ``path`` atomically (temp file in the same dir, fsync, rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def config_path(root: Path) -> Path:
    return Path(root) / CONFIG_DIR / CONFIG_FILE


def key_file_path(root: Path, principal_id: str) -> Path:
    if not _PRINCIPAL_ID_RE.match(principal_id):
        raise ValueError(f"invalid principal id {principal_id!r}")
    return Path(root) / CONFIG_DIR / KEYS_DIR / f"{principal_id}.json"


def default_root(data_dir: str | os.PathLike[str]) -> Path:
    """``{MAPI_DATA_DIR}/gcs`` — where the local stack keeps the shim's state."""
    return Path(data_dir) / "gcs"


def load_config(path: Path) -> ShimConfig:
    return ShimConfig.model_validate_json(Path(path).read_bytes())


def save_config(path: Path, cfg: ShimConfig) -> None:
    atomic_write(Path(path), json.dumps(cfg.to_json_dict(), indent=2).encode(), mode=0o600)


def init_root(root: Path, *, env: str = DEFAULT_ENV) -> ShimConfig:
    """Create the root layout and a default config if none exists; return the config.

    An existing config is loaded as is (its ``env`` wins over ``env``), so secrets are
    stable across restarts.
    """
    root = Path(root)
    (root / CONFIG_DIR / KEYS_DIR).mkdir(parents=True, exist_ok=True)
    os.chmod(root / CONFIG_DIR, 0o700)
    (root / "tmp").mkdir(exist_ok=True)
    (root / "buckets").mkdir(exist_ok=True)
    path = config_path(root)
    if path.exists():
        return load_config(path)
    cfg = default_config(env)
    save_config(path, cfg)
    return cfg


def write_key_files(root: Path, cfg: ShimConfig, *, endpoint: str | None) -> dict[str, Path]:
    """(Re)write one key file per principal; returns ``{principal_id: path}``."""
    out: dict[str, Path] = {}
    for p in cfg.principals:
        path = key_file_path(root, p.id)
        creds = ShimCredentials.from_principal(p, endpoint=endpoint)
        atomic_write(path, creds.dump_json().encode(), mode=0o600)
        out[p.id] = path
    return out


class ShimCredentials(BaseModel):
    """A principal's key file: what a client needs to call the shim and to sign URLs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["mapi_gcs_shim_key"] = KEY_FILE_TYPE
    principal: str
    client_email: str
    token: SecretStr
    secret: SecretStr
    endpoint: str | None = None

    @classmethod
    def from_principal(cls, p: PrincipalConfig, *, endpoint: str | None = None) -> ShimCredentials:
        return cls(
            principal=p.id,
            client_email=p.email,
            token=p.token,
            secret=p.secret,
            endpoint=endpoint,
        )

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> ShimCredentials:
        return cls.model_validate_json(Path(path).read_bytes())

    @classmethod
    def from_root(
        cls, root: str | os.PathLike[str], principal: str = PRINCIPAL_CONTROL
    ) -> ShimCredentials:
        """Load ``{root}/_shim/keys/{principal}.json`` (written when the shim starts)."""
        return cls.load(key_file_path(Path(root), principal))

    def dump_json(self) -> str:
        """JSON **including secrets**."""
        data: Mapping[str, Any] = {
            **self.model_dump(mode="json"),
            "token": self.token.get_secret_value(),
            "secret": self.secret.get_secret_value(),
        }
        return json.dumps(data, indent=2)
