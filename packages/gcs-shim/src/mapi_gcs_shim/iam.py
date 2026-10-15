"""IAM-lite: permissions, predefined roles and per-bucket role bindings.

Mirrors the subset of Cloud Storage IAM the platform relies on (plan §8.1, §9):

* the **control** principal (control plane / finalizer) is ``roles/storage.admin``;
* the **exec** principal (execution service account) has only ``storage.objects.get`` and
  ``storage.objects.delete`` on the bootstrap bucket — no ``list``, and no access at all to
  the bundles bucket.

Bindings are evaluated on every bearer-token JSON-API call. Signed URLs and POST policies are
evaluated against the **signer's** bindings at the time of use, exactly like GCS (a URL signed
by a principal that lost access stops working).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["ANY_BUCKET", "ROLES", "Binding", "Permission"]

ANY_BUCKET: Final = "*"
"""Binding target meaning "every bucket" (a project-level binding)."""


class Permission(StrEnum):
    """The Cloud Storage permissions the shim enforces (plus one shim-only admin permission)."""

    OBJECTS_GET = "storage.objects.get"
    OBJECTS_LIST = "storage.objects.list"
    OBJECTS_CREATE = "storage.objects.create"
    OBJECTS_DELETE = "storage.objects.delete"
    OBJECTS_UPDATE = "storage.objects.update"
    BUCKETS_GET = "storage.buckets.get"
    BUCKETS_LIST = "storage.buckets.list"
    BUCKETS_CREATE = "storage.buckets.create"
    BUCKETS_DELETE = "storage.buckets.delete"
    SHIM_ADMIN = "shim.admin"
    """Shim maintenance endpoints (``/_shim/v1/...``); never exists in real GCS."""


_OBJECT_ALL = frozenset(
    {
        Permission.OBJECTS_GET,
        Permission.OBJECTS_LIST,
        Permission.OBJECTS_CREATE,
        Permission.OBJECTS_DELETE,
        Permission.OBJECTS_UPDATE,
    }
)

ROLES: Final[dict[str, frozenset[Permission]]] = {
    "roles/storage.admin": frozenset(Permission),
    "roles/storage.objectAdmin": _OBJECT_ALL,
    "roles/storage.objectUser": _OBJECT_ALL,
    "roles/storage.objectViewer": frozenset({Permission.OBJECTS_GET, Permission.OBJECTS_LIST}),
    "roles/storage.objectCreator": frozenset({Permission.OBJECTS_CREATE}),
    "roles/storage.legacyBucketReader": frozenset(
        {Permission.BUCKETS_GET, Permission.OBJECTS_LIST}
    ),
}
"""Predefined roles (the GCS role names, restricted to the permissions above)."""


class Binding(BaseModel):
    """Grant a role and/or explicit permissions on one bucket (or ``"*"`` = every bucket)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bucket: str = Field(ANY_BUCKET, min_length=1, max_length=222)
    role: str | None = None
    permissions: tuple[Permission, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> Binding:
        if self.role is None and not self.permissions:
            raise ValueError("a binding needs a role or at least one permission")
        if self.role is not None and self.role not in ROLES:
            raise ValueError(f"unknown role {self.role!r}; known roles: {sorted(ROLES)}")
        return self

    def granted(self) -> frozenset[Permission]:
        """Every permission this binding grants."""
        role = ROLES[self.role] if self.role is not None else frozenset()
        return role | frozenset(self.permissions)

    def applies_to(self, bucket: str | None) -> bool:
        """Whether this binding covers ``bucket`` (``None`` = a project-level check)."""
        if self.bucket == ANY_BUCKET:
            return True
        return bucket is not None and self.bucket == bucket
