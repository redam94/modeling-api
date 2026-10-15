"""A deliberately malicious model plugin — the vehicle that runs the probes *inside* a run.

This package is **never installed into the platform** and is **not registered through the
``mapi.models`` entry point**. It exists only so the security suite can push hostile tenant
code through the real runner (supervisor + worker) on an enforcing executor and confirm that
every attack is blocked. Loading it is always explicit (the suite sets it on the overlay);
nothing in the control plane or the default install references it.

The model's ``fit`` runs the isolation probes (metadata/egress/DNS, /proc, fork/mem/disk,
.pth injection, writing outside scratch) and records their outcomes; a malicious step
(:mod:`mapi_security_plugin.steps`) does the same at transform time. On a correctly isolated
executor every probe comes out BLOCKED and the model simply reports that — it cannot exfiltrate
anything, which is the point.
"""

from __future__ import annotations

from mapi_security_plugin.model import EvilModel
from mapi_security_plugin.steps import EvilStep

__version__ = "0.0.0-malicious"

# Mirrors the real plugins' ``PLUGINS`` tuple so the suite can load it the same way — but it
# is NOT wired to the ``mapi.models`` entry point in pyproject, so a normal install ignores it.
PLUGINS = (EvilModel,)

__all__ = ["PLUGINS", "EvilModel", "EvilStep", "__version__"]
