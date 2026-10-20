"""Backend and device helpers.

Importing this module imports jax but never initialises a backend: :func:`backend` and
:func:`device_kind` do that on first call (inside a fit), so the catalog (``describe``) and
the control-plane-safe parts of the package stay cheap. 64-bit mode stays off: networks train
in float32 and the Laplace algebra runs in numpy float64.
"""

from __future__ import annotations

import jax

__all__ = ["backend", "device_kind", "uses_gpu"]


def backend() -> str:
    """``jax.default_backend()`` (``cpu``, ``gpu`` or ``tpu``)."""
    return str(jax.default_backend())


def device_kind() -> str:
    """The kind of the first device (for example ``cpu`` or ``NVIDIA L4``)."""
    devices = jax.devices()
    return str(getattr(devices[0], "device_kind", devices[0].platform)) if devices else "none"


def uses_gpu() -> bool:
    return backend() == "gpu"
