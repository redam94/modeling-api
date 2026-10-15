"""Credentials for the end-to-end suites against a running stack.

``make dev`` runs the servers in the ``dev`` profile: the suites sign in through its
development IdP and act in ``default/default`` (see :class:`mapi_conformance.stack.StackAuth`).
A single-tenant stack needs no credentials; ``MAPI_TOKEN`` wins over the development IdP.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from mapi_conformance.stack import StackAuth, stack_auth
from mapi_spec.localdev import LocalDev

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def stack_credentials() -> StackAuth:
    local = LocalDev.from_environ(os.environ)
    data_dir = Path(local.data_dir)
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    return stack_auth(local.api_url, data_dir=data_dir)
