"""v0.14 from the client: run media (list, keys, log an image or a table, checked downloads),
the run comparison, tracked keys, experiments and saved run views - sync and async."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
import respx
from mapi_client import AsyncClient, Client, ClientConfig, RunTrackingFrame, media_file_name
from mapi_sdk.media import encode_png
from mapi_spec.api import (
    Experiment,
    ExperimentRunsResult,
    MediaKeySummary,
    Run,
    RunCompare,
    RunKeys,
    RunMedia,
    RunView,
    TrackingFrame,
)
from mapi_spec.errors import IntegrityFailure, MapiError
from mapi_spec.media import MediaTable
from mapi_spec.storage import crc32c, crc32c_to_base64

from .conftest import STORAGE
from .conftest import _example as ex

BASE = "http://api.test"
V1 = f"{BASE}/v1"
EXP = "exp_01jaf3f1g2h3j4k5m6n7p8q9r0"
PNG = encode_png(np.arange(12, dtype=np.uint8).reshape(3, 4))[0]


def body_of(request: httpx.Request) -> Any:
    return json.loads(request.content)


def _page(*items: dict[str, Any], next_cursor: str | None = None) -> dict[str, Any]:
    return {"items": list(items), "next_cursor": next_cursor}


def _media(data: bytes = PNG, **changes: Any) -> dict[str, Any]:
    fields = {
        "id": "rm_1",
        "size": len(data),
        "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
        "crc32c": crc32c_to_base64(crc32c(data)),
        "url": f"{STORAGE}/media/rm_1",
        "width": 4,
        "height": 3,
    }
    return ex(RunMedia, 0, **{**fields, **changes})


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as r:
        yield r


@pytest.fixture
def client(router: respx.MockRouter) -> Iterator[Client]:
    with Client(config=ClientConfig(base_url=BASE, max_retries=0), sleep=lambda _s: None) as c:
        yield c


@pytest.fixture
async def aclient(router: respx.MockRouter) -> AsyncIterator[AsyncClient]:
    async def nosleep(_s: float) -> None:
        return None

    async with AsyncClient(config=ClientConfig(base_url=BASE), sleep=nosleep) as c:
        yield c


# ===================================================================================== media


def test_media_list_keys_and_checked_download(client: Client, router: respx.MockRouter) -> None:
    listing = router.get(f"{V1}/runs/run_1/media").mock(
        return_value=httpx.Response(200, json=_page(_media()))
    )
    keys = router.get(f"{V1}/runs/run_1/media-keys").mock(
        return_value=httpx.Response(200, json=_page(ex(MediaKeySummary)))
    )
    router.get(f"{STORAGE}/media/rm_1").mock(return_value=httpx.Response(200, content=PNG))
    items = list(client.runs.media("run_1", key="samples", kind="image"))
    assert listing.calls[0].request.url.params.multi_items() == [
        ("key", "samples"),
        ("kind", "image"),
        ("links", "true"),
    ]
    assert [k.key for k in client.runs.media_keys("run_1")] == ["samples"]
    assert keys.called
    assert client.runs.download_media("run_1", items[0]) == PNG
    assert media_file_name(items[0]) == "samples-val-3.png"


def test_download_refuses_bytes_that_do_not_match(client: Client, router: respx.MockRouter) -> None:
    other = PNG[:-1] + b"\x00"
    router.get(f"{STORAGE}/media/rm_1").mock(return_value=httpx.Response(200, content=other))
    with pytest.raises(IntegrityFailure):  # CRC32C (the size is the same)
        client.runs.download_media("run_1", RunMedia.model_validate(_media()))
    # a record whose crc matches the bytes but whose sha256 does not
    lying = _media(other, sha256="sha256:" + "0" * 64)
    with pytest.raises(IntegrityFailure, match="sha256"):
        client.runs.download_media("run_1", RunMedia.model_validate(lying))


def test_download_by_id_fetches_a_fresh_link(client: Client, router: respx.MockRouter) -> None:
    get = router.get(f"{V1}/runs/run_1/media/rm_1").mock(
        return_value=httpx.Response(200, json=_media())
    )
    router.get(f"{STORAGE}/media/rm_1").mock(return_value=httpx.Response(200, content=PNG))
    assert client.runs.download_media("run_1", "rm_1") == PNG
    no_link = RunMedia.model_validate({k: v for k, v in _media().items() if k != "url"})
    assert client.runs.download_media("run_1", no_link) == PNG
    assert get.call_count == 2


def test_log_image_from_bytes_path_and_array(
    client: Client, router: respx.MockRouter, tmp_path: Path
) -> None:
    route = router.post(f"{V1}/runs/run_1/media").mock(
        return_value=httpx.Response(201, json=_media())
    )
    item = client.runs.log_image("run_1", "samples", PNG, step=3, labels={"split": "val"})
    assert item.id == "rm_1"
    sent = body_of(route.calls[0].request)
    assert sent["content_type"] == "image/png" and sent["kind"] == "image"
    assert base64.b64decode(sent["data"]) == PNG
    assert (sent["step"], sent["labels"]) == (3, {"split": "val"})
    path = tmp_path / "a.png"
    path.write_bytes(PNG)
    client.runs.log_image("run_1", "samples", path, caption="from a file")
    assert body_of(route.calls[1].request)["caption"] == "from a file"
    client.runs.log_image("run_1", "samples", np.zeros((2, 2), np.uint8))
    assert base64.b64decode(body_of(route.calls[2].request)["data"]).startswith(b"\x89PNG")
    with pytest.raises(ValueError, match="not a PNG or a JPEG"):
        client.runs.log_image("run_1", "samples", b"GIF89a")
    with pytest.raises(ValueError, match="media key"):
        client.runs.log_image("run_1", "bad/key", PNG)
    with pytest.raises(ValueError, match="4,194,304"):
        client.runs.log_image("run_1", "samples", PNG + b"\0" * (4 * 1024 * 1024))
    assert route.call_count == 3


def test_log_table_and_the_tracker(client: Client, router: respx.MockRouter) -> None:
    route = router.post(f"{V1}/runs/run_1/media").mock(
        return_value=httpx.Response(201, json=_media(kind="table"))
    )
    client.runs.log_table("run_1", "scores", {"class": ["a", "b"], "n": [1, 2]})
    sent = body_of(route.calls[0].request)
    assert sent["content_type"] == "application/json" and sent["kind"] == "table"
    table = MediaTable.model_validate_json(base64.b64decode(sent["data"]))
    assert table.columns == ["class", "n"] and table.rows == [["a", 1], ["b", 2]]
    raw = b'{"columns":["x"],"rows":[[1]]}'
    client.runs.log_table("run_1", "scores", raw)
    assert base64.b64decode(body_of(route.calls[1].request)["data"]) == raw
    with client.runs.track("run_1") as t:
        t.log_image("samples", PNG, step=0)
        t.log_table("scores", [{"a": 1}])
        assert route.call_count == 4  # sent at once, not at the flush


def test_tracking_frame_media_flag() -> None:
    frame = RunTrackingFrame(TrackingFrame(run_id="run_1", media=True))
    assert frame.media


# ================================================================================ comparison


def test_compare_and_keys(client: Client, router: respx.MockRouter) -> None:
    compare = router.get(f"{V1}/run-compare").mock(
        return_value=httpx.Response(200, json=ex(RunCompare))
    )
    keys = router.get(f"{V1}/run-keys").mock(return_value=httpx.Response(200, json=ex(RunKeys)))
    res = client.runs.compare(["run_1", "run_2"], keys=["loss"], media_keys=["samples"])
    assert res.params[0].differs
    assert compare.calls[0].request.url.params.multi_items() == [
        ("run", "run_1"),
        ("run", "run_2"),
        ("key", "loss"),
        ("media_key", "samples"),
        ("max_points", "300"),
        ("params", "true"),
    ]
    assert client.runs.keys(experiment_id=EXP).metrics[0].key == "rmse"
    assert keys.calls[0].request.url.params["experiment_id"] == EXP
    with pytest.raises(ValueError):
        client.runs.compare(["run_1"])
    assert compare.call_count == 1


def test_runs_list_create_and_update_with_an_experiment(
    client: Client, router: respx.MockRouter
) -> None:
    listing = router.get(f"{V1}/runs").mock(return_value=httpx.Response(200, json=_page()))
    create = router.post(f"{V1}/runs").mock(
        return_value=httpx.Response(201, json=ex(Run, 0, experiment_id=EXP))
    )
    patch = router.patch(f"{V1}/runs/run_1").mock(return_value=httpx.Response(200, json=ex(Run)))
    list(client.runs.list(experiment_id=EXP))
    assert listing.calls[0].request.url.params["experiment_id"] == EXP
    client.runs.create({"kind": "fit", "spec_id": "spc_1", "revision": 1}, experiment_id=EXP)
    assert body_of(create.calls[0].request)["experiment_id"] == EXP
    client.runs.fit("spc_1", 1, experiment_id=EXP)
    assert body_of(create.calls[1].request)["experiment_id"] == EXP
    client.runs.update("run_1", experiment_id=None)
    assert body_of(patch.calls[0].request) == {"experiment_id": None}


# =============================================================================== experiments


def test_experiments_crud_and_runs(client: Client, router: respx.MockRouter) -> None:
    exp = ex(Experiment)
    listing = router.get(f"{V1}/experiments").mock(
        return_value=httpx.Response(200, json=_page(exp))
    )
    router.post(f"{V1}/experiments").mock(return_value=httpx.Response(201, json=exp))
    router.get(f"{V1}/experiments/{EXP}").mock(return_value=httpx.Response(200, json=exp))
    patch = router.patch(f"{V1}/experiments/{EXP}").mock(
        return_value=httpx.Response(200, json={**exp, "archived": True})
    )
    delete = router.delete(f"{V1}/experiments/{EXP}").mock(return_value=httpx.Response(204))
    runs = router.post(f"{V1}/experiments/{EXP}/runs").mock(
        return_value=httpx.Response(200, json=ex(ExperimentRunsResult))
    )
    created = client.experiments.create("adstock length study", description="d")
    assert created.id == EXP
    assert [e.id for e in client.experiments.list(archived=False)] == [EXP]
    assert listing.calls[0].request.url.params["archived"] == "false"
    found = client.experiments.find("ADSTOCK length study")  # by name, case ignored
    assert found is not None and found.id == EXP
    assert client.experiments.find(EXP) is not None  # by id
    assert client.experiments.find("other") is None
    assert client.experiments.update(EXP, archived=True).archived
    assert body_of(patch.calls[0].request) == {"archived": True}
    client.experiments.update(EXP, description=None)
    assert body_of(patch.calls[1].request) == {"description": None}
    assert client.experiments.add_runs(EXP, ["run_1", "run_2"]).added == 2
    client.experiments.remove_runs(EXP, ["run_1"])
    assert body_of(runs.calls[0].request) == {"add": ["run_1", "run_2"], "remove": []}
    assert body_of(runs.calls[1].request) == {"add": [], "remove": ["run_1"]}
    client.experiments.delete(EXP)
    assert delete.called
    with pytest.raises(ValueError):
        client.experiments.add_runs(EXP, "run_1")


def test_experiment_find_by_an_unknown_id_falls_back_to_names(
    client: Client, router: respx.MockRouter
) -> None:
    router.get(f"{V1}/experiments/exp_zzz").mock(
        return_value=httpx.Response(
            404,
            json={"type": "about:blank", "title": "Not found", "status": 404, "code": "NOT_FOUND"},
            headers={"content-type": "application/problem+json"},
        )
    )
    router.get(f"{V1}/experiments").mock(return_value=httpx.Response(200, json=_page()))
    assert client.experiments.find("exp_zzz") is None


# ============================================================================== saved views


def test_run_views(client: Client, router: respx.MockRouter) -> None:
    view = ex(RunView)
    router.get(f"{V1}/run-views").mock(return_value=httpx.Response(200, json=_page(view)))
    create = router.post(f"{V1}/run-views").mock(return_value=httpx.Response(201, json=view))
    patch = router.patch(f"{V1}/run-views/{view['id']}").mock(
        return_value=httpx.Response(200, json=view)
    )
    router.delete(f"{V1}/run-views/{view['id']}").mock(return_value=httpx.Response(204))
    client.run_views.create(
        "best", {"filters": ["metric.rmse < 15"], "order_by": "metric.rmse"}, experiment_id=EXP
    )
    sent = body_of(create.calls[0].request)
    assert sent["experiment_id"] == EXP and sent["query"]["filters"] == ["metric.rmse < 15"]
    found = client.run_views.find("Best Adstock Fits")
    assert found is not None and found.id == view["id"]
    client.run_views.update(view["id"], name="renamed")
    assert body_of(patch.calls[0].request) == {"name": "renamed"}
    client.run_views.delete(view["id"])
    with pytest.raises(ValueError):
        client.run_views.create("bad", {"filters": ["metric.rmse"]})
    assert create.call_count == 1


# ===================================================================================== async


async def test_async_parity(aclient: AsyncClient, router: respx.MockRouter) -> None:
    router.get(f"{V1}/runs/run_1/media").mock(
        return_value=httpx.Response(200, json=_page(_media()))
    )
    router.get(f"{STORAGE}/media/rm_1").mock(return_value=httpx.Response(200, content=PNG))
    post = router.post(f"{V1}/runs/run_1/media").mock(
        return_value=httpx.Response(201, json=_media())
    )
    router.get(f"{V1}/run-compare").mock(return_value=httpx.Response(200, json=ex(RunCompare)))
    router.get(f"{V1}/experiments").mock(
        return_value=httpx.Response(200, json=_page(ex(Experiment)))
    )
    router.get(f"{V1}/run-views").mock(return_value=httpx.Response(200, json=_page(ex(RunView))))
    items = [m async for m in aclient.runs.media("run_1")]
    assert await aclient.runs.download_media("run_1", items[0]) == PNG
    await aclient.runs.log_image("run_1", "samples", PNG)
    async with aclient.runs.track("run_1") as t:
        await t.log_table("scores", {"a": [1]})
    assert post.call_count == 2
    res = await aclient.runs.compare(["run_1", "run_2"])
    assert len(res.runs) == 2
    found = await aclient.experiments.find("adstock length study")
    assert found is not None and found.id == EXP
    assert await aclient.run_views.find("nope") is None


def _problem(code: str, status: int) -> Callable[[httpx.Request], httpx.Response]:
    def respond(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json={"type": "about:blank", "title": code, "status": status, "code": code},
            headers={"content-type": "application/problem+json"},
        )

    return respond


def test_log_image_errors_surface(client: Client, router: respx.MockRouter) -> None:
    router.post(f"{V1}/runs/run_1/media").mock(side_effect=_problem("QUOTA_EXCEEDED", 429))
    with pytest.raises(MapiError) as exc:
        client.runs.log_image("run_1", "samples", PNG)
    assert exc.value.code.value == "QUOTA_EXCEEDED"
