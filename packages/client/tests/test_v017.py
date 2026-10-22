"""v0.17 from the client: dashboards for any model type (presets, the source catalog, the
layout 2 upgrade, drafts of either layout, scenario rows, keeping a tried case, batch
scoring, the predictions download and pictures) and the same inputs through a share link -
sync and async."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from mapi_client import AsyncClient, Client, ClientConfig
from mapi_client._v016_common import batch_body, dashboard_spec, rows_body
from mapi_spec.api import (
    BatchSubmit,
    BatchTemplate,
    Dashboard,
    DashboardPresets,
    DashboardUpgrade,
    Readout,
    ReadoutHeader,
    ShareSession,
    SourceCatalog,
)
from mapi_spec.dashboard_v2 import DashboardSpecV2
from mapi_spec.dashboards import DashboardSpec
from pydantic import ValidationError

from .conftest import _example as ex

BASE = "http://api.test"
V1 = f"{BASE}/v1"
PROJECT = "acme/growth"
TOKEN = "mapi_test_token"
DSH = "dsh_01jaf3r1s2t3v4w5x6y7z8a9b0"
DSA = "dsa_01jaf3r1s2t3v4w5x6y7z8a9b0"
SHARE = "mpd_7k2m9q4x0123456789abcdefghjkmnpqrstvwxyz"
SESSION = ex(ShareSession)["session"]
INPUTS = ex(BatchSubmit)["inputs"]
CSV = b"case_id,tenure,plan,p_churn\nc1,12,basic,0.31\nc2,40,plus,'=1+1\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def body_of(request: httpx.Request) -> Any:
    return json.loads(request.content)


def _readout(seq: int, status: str = "ready", **changes: Any) -> dict[str, Any]:
    data = ex(Readout, 0, seq=seq, status=status, **changes)
    if status != "ready":
        data["snapshot"] = None
    return data


def _session() -> dict[str, Any]:
    expires = (datetime.now(UTC) + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    return ex(ShareSession, 0, expires_at=expires)


def _no_credentials(request: httpx.Request) -> bool:
    return "authorization" not in request.headers and "x-mapi-project" not in request.headers


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as r:
        yield r


def _config() -> ClientConfig:
    return ClientConfig(
        base_url=BASE, token=TOKEN, project=PROJECT, max_retries=0, allow_insecure_token=True
    )


@pytest.fixture
def client(router: respx.MockRouter) -> Iterator[Client]:
    with Client(config=_config(), sleep=lambda _s: None) as c:
        yield c


@pytest.fixture
async def aclient(router: respx.MockRouter) -> AsyncIterator[AsyncClient]:
    async def nosleep(_s: float) -> None:
        return None

    async with AsyncClient(config=_config(), sleep=nosleep) as c:
        yield c


# ================================================================================== bodies


def test_specs_of_either_layout() -> None:
    v2 = DashboardSpecV2.model_json_schema()["examples"][0]
    assert isinstance(dashboard_spec(v2), DashboardSpecV2)
    v1 = DashboardSpec.model_json_schema()["examples"][0]
    assert isinstance(dashboard_spec(v1), DashboardSpec)
    parsed = dashboard_spec(v2)
    assert dashboard_spec(parsed) is parsed


def test_rows_and_batch_bodies() -> None:
    one = rows_body({"price": 18.0}, label="One price")
    assert one.rows == [{"price": 18.0}] and one.label == "One price"
    assert len(rows_body([{"price": 18.0}, {"price": 22.5}], label=None).rows) == 2
    with pytest.raises(ValidationError):
        rows_body([{"price": float(i)} for i in range(21)], label=None)  # at most 20 rows
    with pytest.raises(ValidationError):
        rows_body({"price": float("nan")}, label=None)
    with pytest.raises(TypeError):
        rows_body(one, label=None)  # type: ignore[arg-type]
    assert batch_body(INPUTS, label="October").label == "October"
    with pytest.raises(TypeError):
        batch_body(BatchSubmit.model_validate(ex(BatchSubmit)), label=None)


# ============================================================================== dashboards


def test_presets_sources_and_upgrade(client: Client, router: respx.MockRouter) -> None:
    presets = router.get(f"{V1}/dashboards:presets").mock(
        return_value=httpx.Response(200, json=ex(DashboardPresets))
    )
    router.get(f"{V1}/dashboards/{DSH}/sources").mock(
        return_value=httpx.Response(200, json=ex(SourceCatalog))
    )
    upgrade = router.post(f"{V1}/dashboards/{DSH}:upgrade").mock(
        return_value=httpx.Response(200, json=ex(DashboardUpgrade))
    )
    created = router.post(f"{V1}/dashboards").mock(
        return_value=httpx.Response(201, json=ex(Dashboard))
    )

    found = client.dashboards.presets(model_id="churn", version=2)
    assert found.suggestions[0].preset == "forecast"
    assert isinstance(found.spec, DashboardSpecV2)
    sent = presets.calls.last.request
    assert dict(sent.url.params) == {"model_id": "churn", "version": "2"}
    assert sent.headers["x-mapi-project"] == PROJECT
    with pytest.raises(ValueError, match="model_id"):
        client.dashboards.presets()

    assert isinstance(client.dashboards.sources(DSH), SourceCatalog)
    result = client.dashboards.upgrade(DSH, etag='"3"')
    assert isinstance(result.dashboard.draft_spec, DashboardSpecV2) and result.notes
    assert upgrade.calls.last.request.headers["if-match"] == '"3"'

    client.dashboards.create("churn-weekly", found.spec)
    assert body_of(created.calls.last.request)["spec"]["layout_version"] == 2


def test_try_a_case_keep_it_and_score_a_file(client: Client, router: respx.MockRouter) -> None:
    predict = router.post(f"{V1}/dashboards/{DSH}/predictions").mock(
        return_value=httpx.Response(202, json=_readout(20, "queued", kind="prediction"))
    )
    router.get(f"{V1}/dashboards/{DSH}/readouts/20").mock(
        side_effect=[
            httpx.Response(200, json=_readout(20, "running", kind="prediction")),
            httpx.Response(200, json=_readout(20, "ready", kind="prediction")),
        ]
    )
    keep = router.post(f"{V1}/dashboards/{DSH}/readouts/20:keep").mock(
        return_value=httpx.Response(200, json=ex(ReadoutHeader, 0, seq=20, kind="prediction"))
    )
    router.get(f"{V1}/dashboards/{DSH}/batch/template").mock(
        return_value=httpx.Response(200, json=ex(BatchTemplate))
    )
    batch = router.post(f"{V1}/dashboards/{DSH}/batch").mock(
        return_value=httpx.Response(202, json=_readout(21, "queued", kind="batch"))
    )
    download = router.get(f"{V1}/dashboards/{DSH}/readouts/21/predictions.csv").mock(
        return_value=httpx.Response(200, content=CSV, headers={"content-type": "text/csv"})
    )
    router.get(f"{V1}/dashboards/{DSH}/assets/{DSA}").mock(
        return_value=httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
    )

    dashboards = client.dashboards
    queued = dashboards.predict(DSH, {"price": 18.0}, label="One price")
    assert queued.status == "queued"
    sent = predict.calls.last.request
    assert body_of(sent) == {"rows": [{"price": 18.0}], "label": "One price"}
    assert "idempotency-key" in sent.headers
    assert dashboards.wait_readout(DSH, queued.seq).status == "ready"
    assert dashboards.keep_readout(DSH, 20).seq == 20
    assert keep.called

    assert dashboards.batch_template(DSH).id_column == "case_id"
    readout = dashboards.score_batch(DSH, INPUTS, label="October", idempotency_key="k-1")
    assert readout.seq == 21 and batch.calls.last.request.headers["idempotency-key"] == "k-1"
    assert body_of(batch.calls.last.request)["label"] == "October"
    assert dashboards.download_predictions(DSH, 21) == CSV
    assert download.calls.last.request.headers["x-mapi-project"] == PROJECT
    assert dashboards.asset(DSH, DSA) == PNG
    with pytest.raises(ValueError, match="picture"):
        dashboards.asset(DSH, "dsa_../../x")


def test_share_link_inputs_send_no_credentials(client: Client, router: respx.MockRouter) -> None:
    router.post(f"{V1}/share-sessions").mock(return_value=httpx.Response(200, json=_session()))
    predict = router.post(f"{V1}/shared/predictions").mock(
        return_value=httpx.Response(202, json=_readout(30, "queued", kind="prediction"))
    )
    template = router.get(f"{V1}/shared/batch/template").mock(
        return_value=httpx.Response(200, json=ex(BatchTemplate))
    )
    batch = router.post(f"{V1}/shared/batch").mock(
        return_value=httpx.Response(202, json=_readout(31, "queued", kind="batch"))
    )
    download = router.get(f"{V1}/shared/readouts/31/predictions.csv").mock(
        return_value=httpx.Response(200, content=CSV)
    )
    asset = router.get(f"{V1}/shared/assets/{DSA}").mock(
        return_value=httpx.Response(200, content=PNG)
    )
    view = client.dashboards.open_share(SHARE)
    assert view.predict([{"price": 18.0}, {"price": 22.5}]).seq == 30
    assert view.batch_template().max_rows == 2000
    assert view.score_batch(INPUTS).seq == 31
    assert view.download_predictions(31) == CSV
    assert view.asset(DSA) == PNG
    for route in (predict, template, batch, download, asset):
        sent = route.calls.last.request
        assert sent.headers["x-mapi-share"] == SESSION and _no_credentials(sent)


# =================================================================================== async


async def test_async_dashboards(aclient: AsyncClient, router: respx.MockRouter) -> None:
    router.get(f"{V1}/dashboards:presets").mock(
        return_value=httpx.Response(200, json=ex(DashboardPresets))
    )
    router.get(f"{V1}/dashboards/{DSH}/sources").mock(
        return_value=httpx.Response(200, json=ex(SourceCatalog))
    )
    router.post(f"{V1}/dashboards/{DSH}:upgrade").mock(
        return_value=httpx.Response(200, json=ex(DashboardUpgrade))
    )
    router.post(f"{V1}/dashboards/{DSH}/predictions").mock(
        return_value=httpx.Response(200, json=_readout(20, "ready", kind="prediction"))
    )
    router.post(f"{V1}/dashboards/{DSH}/readouts/20:keep").mock(
        return_value=httpx.Response(200, json=ex(ReadoutHeader, 0, seq=20, kind="prediction"))
    )
    router.get(f"{V1}/dashboards/{DSH}/batch/template").mock(
        return_value=httpx.Response(200, json=ex(BatchTemplate))
    )
    router.post(f"{V1}/dashboards/{DSH}/batch").mock(
        return_value=httpx.Response(202, json=_readout(21, "queued", kind="batch"))
    )
    router.get(f"{V1}/dashboards/{DSH}/readouts/21/predictions.csv").mock(
        return_value=httpx.Response(200, content=CSV)
    )
    router.get(f"{V1}/dashboards/{DSH}/assets/{DSA}").mock(
        return_value=httpx.Response(200, content=PNG)
    )
    dashboards = aclient.dashboards
    assert (await dashboards.presets(alias="churn-live")).suggestions
    assert isinstance(await dashboards.sources(DSH), SourceCatalog)
    assert (await dashboards.upgrade(DSH, etag='"3"')).notes
    assert (await dashboards.predict(DSH, {"price": 18.0})).status == "ready"
    assert (await dashboards.keep_readout(DSH, 20)).seq == 20
    assert (await dashboards.batch_template(DSH)).max_rows == 2000
    assert (await dashboards.score_batch(DSH, INPUTS)).seq == 21
    assert await dashboards.download_predictions(DSH, 21) == CSV
    assert await dashboards.asset(DSH, DSA) == PNG


async def test_async_share_link_inputs(aclient: AsyncClient, router: respx.MockRouter) -> None:
    router.post(f"{V1}/share-sessions").mock(return_value=httpx.Response(200, json=_session()))
    predict = router.post(f"{V1}/shared/predictions").mock(
        return_value=httpx.Response(202, json=_readout(30, "queued", kind="prediction"))
    )
    router.get(f"{V1}/shared/batch/template").mock(
        return_value=httpx.Response(200, json=ex(BatchTemplate))
    )
    router.post(f"{V1}/shared/batch").mock(
        return_value=httpx.Response(202, json=_readout(31, "queued", kind="batch"))
    )
    router.get(f"{V1}/shared/readouts/31/predictions.csv").mock(
        return_value=httpx.Response(200, content=CSV)
    )
    router.get(f"{V1}/shared/assets/{DSA}").mock(return_value=httpx.Response(200, content=PNG))
    view = await aclient.dashboards.open_share(SHARE)
    assert (await view.predict({"price": 18.0})).seq == 30
    assert (await view.batch_template()).id_column == "case_id"
    assert (await view.score_batch(INPUTS)).seq == 31
    assert await view.download_predictions(31) == CSV
    assert await view.asset(DSA) == PNG
    sent = predict.calls.last.request
    assert sent.headers["x-mapi-share"] == SESSION and _no_credentials(sent)
