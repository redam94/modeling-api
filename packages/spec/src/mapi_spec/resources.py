"""Resource classes (S, M, L, XL) and their budgets."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["RESOURCE_CLASSES", "ResourceClass", "ResourceClassSpec"]


class ResourceClass(StrEnum):
    """Executor resource class. Ordered: ``S < M < L < XL``."""

    S = "S"
    M = "M"
    L = "L"
    XL = "XL"

    @property
    def rank(self) -> int:
        return list(ResourceClass).index(self)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, ResourceClass):
            return NotImplemented
        return self.rank < other.rank

    def __le__(self, other: object) -> bool:
        if not isinstance(other, ResourceClass):
            return NotImplemented
        return self.rank <= other.rank

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, ResourceClass):
            return NotImplemented
        return self.rank > other.rank

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, ResourceClass):
            return NotImplemented
        return self.rank >= other.rank

    def next_up(self) -> ResourceClass | None:
        """The class one size up (offered after ``RESOURCE_EXHAUSTED``), or ``None``."""
        members = list(ResourceClass)
        return members[self.rank + 1] if self.rank + 1 < len(members) else None


GIB = 1024**3
GB = 1000**3


@dataclass(frozen=True, slots=True)
class ResourceClassSpec:
    """CPU/RAM and the derived scratch budget and data caps (bytes)."""

    vcpu: int | None
    ram_bytes: int | None
    scratch_bytes: int | None
    max_dataset_bytes: int
    max_bundle_bytes: int


RESOURCE_CLASSES: dict[ResourceClass, ResourceClassSpec] = {
    ResourceClass.S: ResourceClassSpec(2, 8 * GIB, 3 * GIB, GB // 2, 1 * GB),
    ResourceClass.M: ResourceClassSpec(4, 16 * GIB, 6 * GIB, 2 * GB, 4 * GB),
    ResourceClass.L: ResourceClassSpec(8, 32 * GIB, 12 * GIB, 5 * GB, 8 * GB),
    ResourceClass.XL: ResourceClassSpec(None, None, None, 50 * GB, 50 * GB),
}
