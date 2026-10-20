"""Resource classes (S, M, L, XL, GPU) and their budgets."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = ["CPU_CLASSES", "RESOURCE_CLASSES", "ResourceClass", "ResourceClassSpec"]


class ResourceClass(StrEnum):
    """Executor resource class. Ordered: ``S < M < L < XL < GPU``.

    ``GPU`` is the last (highest-rank) member on purpose: a quota whose
    ``max_resource_class`` is below it refuses it, and :meth:`next_up` never offers it.
    """

    S = "S"
    M = "M"
    L = "L"
    XL = "XL"
    GPU = "GPU"

    @property
    def rank(self) -> int:
        return list(ResourceClass).index(self)

    @property
    def is_gpu(self) -> bool:
        """True for the accelerator class."""
        return RESOURCE_CLASSES[self].accelerators > 0

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
        """The CPU class one size up (offered after ``RESOURCE_EXHAUSTED``), or ``None``.

        Walks :data:`CPU_CLASSES` only: ``XL`` and ``GPU`` have no next class (a GPU is
        never offered as a retry for memory exhaustion).
        """
        if self not in CPU_CLASSES:
            return None
        i = CPU_CLASSES.index(self)
        return CPU_CLASSES[i + 1] if i + 1 < len(CPU_CLASSES) else None


CPU_CLASSES: Final[tuple[ResourceClass, ...]] = (
    ResourceClass.S,
    ResourceClass.M,
    ResourceClass.L,
    ResourceClass.XL,
)
"""The classes without an accelerator, smallest first."""

GIB = 1024**3
GB = 1000**3


@dataclass(frozen=True, slots=True)
class ResourceClassSpec:
    """CPU/RAM, the derived scratch budget and data caps (bytes), and accelerators."""

    vcpu: int | None
    ram_bytes: int | None
    scratch_bytes: int | None
    max_dataset_bytes: int
    max_bundle_bytes: int
    accelerator: str | None = None
    accelerators: int = 0


RESOURCE_CLASSES: dict[ResourceClass, ResourceClassSpec] = {
    ResourceClass.S: ResourceClassSpec(2, 8 * GIB, 3 * GIB, GB // 2, 1 * GB),
    ResourceClass.M: ResourceClassSpec(4, 16 * GIB, 6 * GIB, 2 * GB, 4 * GB),
    ResourceClass.L: ResourceClassSpec(8, 32 * GIB, 12 * GIB, 5 * GB, 8 * GB),
    ResourceClass.XL: ResourceClassSpec(None, None, None, 50 * GB, 50 * GB),
    # One NVIDIA L4 (Cloud Run Jobs GPU shape: 8 vCPU / 32 GiB is the documented minimum).
    ResourceClass.GPU: ResourceClassSpec(
        8, 32 * GIB, 12 * GIB, 50 * GB, 8 * GB, accelerator="nvidia-l4", accelerators=1
    ),
}
