"""test.acme_zero_share: a workflow node scaffolded by ``mapi plugin init --kind node``.

A plugin node is a JSON contract (what the workflow editor, the validator and the control
plane read; it never imports this code) plus the function the runner sandbox calls. The
contract's id is ``<kind>.<namespace>_<name>`` and ``plugin`` names your namespace.
``zero_share`` is a statistical test: the share of zeros of one variable, with a yes/no
verdict that branch conditions can read. Replace it with your node, update the About card
and keep ``mapi plugin test`` (the contract kit) green.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from mapi_spec.node_authoring import NAME_PATTERN, about, node, num, string
from mapi_workflows.runtime.nodes import NodeExport
from mapi_workflows.runtime.values import NodeContext, NodeOutcome

__all__ = ["NODES", "ZERO_SHARE", "ZERO_SHARE_CONTRACT", "zero_share"]

ZERO_SHARE_CONTRACT = node(
    "test.acme_zero_share",
    "test",
    about(
        "Share of zeros",
        "Counts how many values of a variable are zero. It warns you before a model that "
        "cannot handle many zeros.",
        use=["Before you fit a model to counts or sales"],
        avoid=["The data has no zeros by design"],
        needs=["A numeric variable"],
        assumes=["Zeros are real values, not missing values"],
        outputs=["The share of zeros and a pass or fail verdict"],
        difficulty="beginner",
        runtime="instant",
        example="In weekly store sales, 12% of the weeks have zero sales: above 10%, it fails.",
    ),
    plugin="acme",
    version="0.1.0",
    params={
        "var": string("The variable to test", "sales", max_length=128, pattern=NAME_PATTERN),
        "threshold": num("The largest share of zeros that passes", 0.2, 0, 1),
    },
    outputs={"result": "test_result"},
    fields={
        "share": ("number", "The share of zeros"),
        "reject": ("boolean", "True if there are too many zeros"),
        "verdict_plain": ("string", "The verdict in plain words"),
    },
)
"""The contract: ports, parameters (a closed JSON Schema), result fields and About card."""


def zero_share(ctx: NodeContext) -> NodeOutcome:
    """The share of zeros of ``var``: a ``test_result`` payload plus the result fields."""
    dv = ctx.dataset("data")
    assert dv is not None  # the port is required: the runner never calls without it
    var = str(ctx.params.get("var") or "sales")
    threshold = float(ctx.params.get("threshold", 0.2))  # type: ignore[arg-type]
    values = np.asarray(dv.data[var].values, dtype=float)
    share = float((values == 0).mean()) if values.size else 0.0
    reject = share > threshold
    verdict = "Too many values are zero." if reject else "Few values are zero."
    payload: dict[str, Any] = {
        "test": "test.acme_zero_share",
        "var": var,
        "statistic": share,
        "alpha": 0.05,
        "reject": reject,
        "decision": "too_many_zeros" if reject else "ok",
        "verdict_plain": verdict,
        "null_plain": "The share of zeros is at most the threshold.",
        "n_obs": int(values.size),
    }
    return NodeOutcome(
        result={"share": share, "reject": reject, "verdict_plain": verdict},
        outputs={"result": payload},
    )


ZERO_SHARE = NodeExport(contract=ZERO_SHARE_CONTRACT, fn=zero_share)

NODES = [ZERO_SHARE]
"""The ``mapi.workflow_nodes`` entry point: every node listed here is offered."""
