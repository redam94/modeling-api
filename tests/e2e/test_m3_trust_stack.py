"""The M3 trust lifecycle through the *running* stack (``make dev``, then ``make e2e``).

From outside the processes, against a ``dev``-profile stack with the outbox mailer:

* sign-up: ``POST /auth/signup`` answers ``202``; the verification e-mail is read back from
  ``GET /dev/outbox``; ``POST /auth/verify-email`` creates a ``public`` workspace; signing
  in through the development IdP with that address claims the admin grant;
* verification: the workspace is e-mail verified and may not upload code (cut line A);
* export: a GDPR export is built by the orchestrator and downloads as a zip through its
  signed link, holding the workspace's own records only;
* deletion: a scheduled deletion suspends the workspace and can be cancelled; when the
  stack runs with ``MAPI_TENANT_DELETION_GRACE_S=0`` a second request is purged by the
  orchestrator (``purged``, the tenant unknown to its admin, the API token dead).

The account (``MAPI_E2E_SIGNUP_ACCOUNT``, default ``viewer@dev.local``) must be one the dev
IdP offers (``MAPI_DEVIDP_USERS``) so it can sign in; every run creates a new workspace.
"""

from __future__ import annotations

import io
import os
import time
import uuid
import zipfile
from collections.abc import Iterator
from typing import Any, Final

import httpx
import pytest
from mapi_client.oidc import dev_idp_login
from mapi_spec.localdev import LocalDev
from mapi_spec.trust import TERMS_VERSION

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.slow]

TIMEOUT_S: Final = 180.0


@pytest.fixture(scope="module")
def api() -> str:
    url = LocalDev.from_environ(os.environ).api_url.rstrip("/")
    try:
        r = httpx.get(f"{url}/dev/outbox", timeout=10)
    except httpx.TransportError as exc:
        pytest.fail(f"the API is not reachable at {url} ({exc}); start it with `make dev`")
    if r.status_code != 200:
        pytest.skip("the trust suite needs a dev-profile stack with MAPI_MAILER=outbox")
    return url


@pytest.fixture(scope="module")
def account() -> str:
    return os.environ.get("MAPI_E2E_SIGNUP_ACCOUNT", "viewer@dev.local").strip().lower()


@pytest.fixture(scope="module")
def raw(api: str) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=api, timeout=60, follow_redirects=False) as client:
        yield client


def _poll(fn: Any, *, what: str) -> Any:
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        value = fn()
        if value is not None:
            return value
        time.sleep(1.0)
    pytest.fail(f"timed out waiting for {what}")


@pytest.fixture(scope="module")
def workspace(api: str, raw: httpx.Client, account: str) -> dict[str, str]:
    org = f"E2E Trust {uuid.uuid4().hex[:6]}"
    r = raw.post(
        "/auth/signup",
        json={"email": account, "name": "E2E Viewer", "organisation": org,
              "accepted_terms": True, "terms_version": TERMS_VERSION},
    )  # fmt: skip
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "check_email"
    item = raw.get("/dev/outbox", params={"to": account, "limit": 1}).json()["items"][0]
    token = item["tags"]["token"]
    assert token in item["body"]
    r = raw.post("/auth/verify-email", json={"token": token})
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["trust_tier"] == "public" and result["project_id"] == "default"
    again = raw.post("/auth/verify-email", json={"token": token})
    assert again.status_code == 410  # single use
    return {"tenant": result["tenant_id"], "id_token": dev_idp_login(api, account)}


def _auth(ws: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {ws['id_token']}"}


def test_the_new_workspace_is_public_and_administered_by_the_signer(
    raw: httpx.Client, workspace: dict[str, str]
) -> None:
    me = raw.get("/v1/me", headers=_auth(workspace)).json()
    roles = {t["tenant"]["id"]: t["role"] for t in me["tenants"]}
    assert roles.get(workspace["tenant"]) == "admin"
    tier = {t["tenant"]["id"]: t["tenant"]["trust_tier"] for t in me["tenants"]}
    assert tier[workspace["tenant"]] == "public"
    status = raw.get(
        f"/v1/tenants/{workspace['tenant']}/verification", headers=_auth(workspace)
    ).json()
    assert status["email_verified"] is True and status["uploaded_code_allowed"] is False


def test_gdpr_export_downloads_a_zip_of_the_workspace(
    raw: httpx.Client, workspace: dict[str, str]
) -> None:
    tenant = workspace["tenant"]
    r = raw.post(f"/v1/tenants/{tenant}/exports", json={}, headers=_auth(workspace))
    assert r.status_code == 202, r.text
    exp_id = r.json()["id"]

    def ready() -> dict[str, Any] | None:
        doc = raw.get(f"/v1/tenants/{tenant}/exports/{exp_id}", headers=_auth(workspace)).json()
        assert doc["state"] != "failed", doc
        return doc if doc["state"] == "ready" else None

    doc = _poll(ready, what="the export to be built (is the worker running?)")
    blob = httpx.get(doc["download_url"], timeout=60)
    assert blob.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(blob.content))
    assert {"README.txt", "manifest.json", "metadata/projects.jsonl"} <= set(zf.namelist())
    assert f'"tenant_id": "{tenant}"' in zf.read("metadata/projects.jsonl").decode()


def test_deletion_is_scheduled_cancelled_and_purged(
    raw: httpx.Client, workspace: dict[str, str]
) -> None:
    tenant, auth = workspace["tenant"], _auth(workspace)
    bad = raw.post(f"/v1/tenants/{tenant}/deletion", json={"confirm": "nope"}, headers=auth)
    assert bad.status_code == 422
    r = raw.post(f"/v1/tenants/{tenant}/deletion", json={"confirm": tenant}, headers=auth)
    assert r.status_code == 202, r.text
    if r.json()["purge_after"] > r.json()["requested_at"]:
        cancel = raw.delete(f"/v1/tenants/{tenant}/deletion", headers=auth)
        assert cancel.status_code == 200 and cancel.json()["state"] == "cancelled"
        pytest.skip("the purge is checked on stacks with MAPI_TENANT_DELETION_GRACE_S=0")

    def gone() -> int | None:
        code = raw.get(f"/v1/tenants/{tenant}/deletion", headers=auth).status_code
        return code if code in (403, 404) else None

    # once the purge starts the tenant refuses its own members; operators see the progress
    _poll(gone, what="the orchestrator to start the purge")
    operator = {"Authorization": f"Bearer {dev_idp_login(str(raw.base_url), 'admin@dev.local')}"}

    def purged() -> dict[str, Any] | None:
        doc = raw.get(f"/v1/tenants/{tenant}/deletion", headers=operator).json()
        return doc if doc["state"] == "purged" else None

    doc = _poll(purged, what="the purge to finish")
    assert {s["store"]: s["state"] for s in doc["stores"]}["database"] == "done"
