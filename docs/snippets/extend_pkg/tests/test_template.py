"""Contract-kit tests for acme_weekly_check (run them with ``mapi plugin test``).

The kit checks the id (your namespace, not a built-in template id), the About card and the
document, validated against the node contracts (the built-in nodes and this package's node).
"""

from __future__ import annotations

import pytest
from mapi_workflows.kit import check_template
from mapi_workflows.runtime.nodes import all_node_contracts

from acme.nodes import ZERO_SHARE
from acme.templates import TEMPLATES, WEEKLY_CHECK


def test_the_template_is_exported() -> None:
    assert WEEKLY_CHECK in TEMPLATES
    assert WEEKLY_CHECK.id == "acme_weekly_check"


@pytest.mark.mapi_contract
def test_contract_kit() -> None:
    issues = check_template(WEEKLY_CHECK, nodes=all_node_contracts([ZERO_SHARE.contract]))
    assert issues == [], [f"{i.pointer}: {i.message}" for i in issues]
