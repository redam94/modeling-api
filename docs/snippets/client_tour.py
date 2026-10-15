"""The Python client guide, end to end against a running stack.

Included by ``docs/guides/python-client.md``. It needs ``make dev`` (any ports: the client
reads ``MAPI_URL``, then ``MAPI_API_URL``) and the demo files in ``demo/``
(``uv run python scripts/seed.py --export demo/``). ``make e2e`` runs it
(``conformance/tests/test_docs_platform.py``).
"""

from __future__ import annotations

# --8<-- [start:connect]
from mapi_client import Client

api = Client()  # MAPI_URL / MAPI_API_URL, a profile, or http://127.0.0.1:8000
health = api.ready()
print(api.config.base_url, health.status, health.version)
# --8<-- [end:connect]

# --8<-- [start:upload]
versions = {}
for name in ("eu_media_daily", "fx_daily", "holidays_eu", "eu_media_future"):
    versions[name] = api.datasets.upload(f"demo/{name}.zarr.zip", dataset=name)
    print(versions[name].ref, versions[name].status.value, versions[name].sha256[:19])
# --8<-- [end:upload]

# --8<-- [start:schema]
schema = versions["eu_media_daily"].data_schema  # the DataSchema the ingest run checked
print({dim: spec.policy.value for dim, spec in schema.dims.items()})
print({var: spec.role.value for var, spec in schema.vars.items()})
# --8<-- [end:schema]

# --8<-- [start:validate]
with open("demo/eu-geo-mmm.yaml", encoding="utf-8") as fh:
    spec_yaml = fh.read()

check = api.specs.validate(spec_yaml)
print("valid" if check.valid else [(i.code, i.pointer) for i in check.issues])
for stage in check.stages:  # the schema after every pipeline step, and where the split is
    if stage.data_schema is not None:
        print(stage.name, sorted(stage.data_schema.dims), "<- split" if stage.split else "")
# --8<-- [end:validate]

# --8<-- [start:fit]
from mapi_client import RunEnd, RunEventFrame, RunLiveFrame

revision = api.specs.upsert(spec_yaml)  # creates the spec, or adds a revision if it changed
run = api.runs.fit(revision.spec_id, revision.revision)

for item in api.runs.stream_events(run.id):  # SSE; resumes with Last-Event-ID
    match item:
        case RunLiveFrame(name="progress", event=event):  # live only, never replayed
            print("  ", event.payload.get("message"))
        case RunEventFrame(name=name, event=event):  # persisted, with an event id
            p = event.payload  # state, warning, diagnostic, metric, complete, error
            print(name, p.get("code") or p.get("name") or "", p.get("message") or p.get("value"))
        case RunEnd(run=final):  # the stream closes after this frame
            print("end:", final.state.value)

run = api.runs.wait(run.id, raise_on_failure=True)
print(run.state.value, run.output_model_version_id)
# --8<-- [end:fit]

# --8<-- [start:inspect]
model = next(m for m in api.models.list() if m.name == "eu-geo-mmm")
mv = api.models.version(model.id, model.latest_version)
print(mv.ref, mv.scorecard.overall if mv.scorecard else None)  # pass | warn | fail
print(mv.metrics)
for item in mv.scorecard.items if mv.scorecard else []:
    print(f"{item.status:4} {item.code}: {item.message_plain}")
# --8<-- [end:inspect]

# --8<-- [start:download]
metrics = api.models.download(model.id, mv.version, "metrics.json")  # bytes, CRC32C-checked
api.models.download(model.id, mv.version, "bundle.zarr.zip", dest="eu-geo-mmm.zarr.zip")
# --8<-- [end:download]

# --8<-- [start:score]
result = api.models.score(
    model.id,
    mv.version,
    {
        "model": mv.ref,
        "mode": "forecast",
        "horizon": {"dim": "time", "periods": 8},
        "data": "dataset://default/eu_media_future@latest",
        "side_tables": {"fx": "dataset://default/fx_daily@latest"},  # score_policy: supplied
        "output": {"kind": "expected", "summary": ["mean", "hdi_90"]},
    },
)
print(result.run.state.value, [link.name for link in result.outputs])
summary = result.tree["summary"]  # an xr.DataTree (needs the `xarray` extra)
print(result.tree.attrs["output_space"])  # {"y_hat": "weekly", ...}: weekly EUR, inverted
print(summary["y_hat"].sel(stat="mean", market="NL").round(0).values)
# --8<-- [end:score]

# --8<-- [start:errors]
from mapi_spec.errors import MapiError, ModeNotSupported

request = {
    "model": mv.ref,
    "mode": "forecast",
    "horizon": {"dim": "time", "periods": 8},
    "data": "dataset://default/eu_media_future@latest",
}
try:  # the fx side table has `score_policy: supplied`, and this request does not supply it
    api.models.score(model.id, mv.version, request)
except MapiError as err:
    print(err.code, err)  # SIDE_TABLE_COVERAGE, with a JSON pointer: /side_tables/fx

try:  # this model cannot filter (that is for state-space models)
    api.models.score(model.id, mv.version, {**request, "mode": "filter"})
except ModeNotSupported as err:
    print(err.code)  # MODE_NOT_SUPPORTED, raised before any run is created
# --8<-- [end:errors]

api.close()

# --8<-- [start:async]
import asyncio

from mapi_client import AsyncClient


async def recent_fits(n: int = 3) -> list[str]:
    async with AsyncClient() as aapi:  # same resources and arguments, awaited
        found: list[str] = []
        async for run in aapi.runs.list(kind="fit", limit=n):  # follows next_cursor lazily
            found.append(f"{run.id} {run.state.value}")
            if len(found) == n:
                break
        return found


print(asyncio.run(recent_fits()))
# --8<-- [end:async]
