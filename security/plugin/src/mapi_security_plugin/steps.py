"""A malicious preprocessing step that attempts egress/filesystem escapes at transform time.

Preprocessing runs in the same untrusted worker as the model, so a hostile step is another
way tenant code could try to reach credentials or the network. This one simply runs the
isolation probes from inside ``transform`` and records the outcomes in its state; on an
enforcing executor they are all blocked.
"""

from __future__ import annotations

from typing import Any

import xarray as xr


class EvilStep:
    """A stateful step (fit → state, transform(state)) whose fit tries to escape the sandbox."""

    id = "security.evil_step"
    version = "0.0.0"

    def fit(self, ds: xr.Dataset, **_: Any) -> dict[str, Any]:
        from mapi_security.outcomes import Category
        from mapi_security.probes import ALL_PROBES

        outcomes = {
            p.probe_id: p.run().outcome.value
            for p in ALL_PROBES
            if p.category is Category.ISOLATION
        }
        return {"probe_outcomes": outcomes}

    def transform(self, ds: xr.Dataset, state: dict[str, Any]) -> xr.Dataset:
        return ds
