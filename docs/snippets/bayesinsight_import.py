"""Import a BayesInsight project, check the draft and fit it on a laptop before uploading.

Included by ``docs/tutorials/bayesinsight-import.md``; runs offline
(conformance/tests/test_docs_snippets.py, marked slow: two tiny MCMC fits). The project is
the synthetic one in ``mapi_plugin_mmm.bayesinsight_examples``: a weekly geography x product
panel simulated exactly as ``mmm.bayesinsight`` models it, with known parameters, so the
page can check that the imported model recovers them. The API call goes to a stand-in that
runs the server's own import code (``httpx.MockTransport``).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from mapi_client import Client, ClientConfig
from mapi_plugin_mmm.bayesinsight_examples import (
    BI_METADATA,
    BI_MODEL_DEF,
    bayesinsight_dataset,
    bayesinsight_mff,
)
from mapi_spec.tokens import generate_token

# Your BayesInsight export: model_def.json, metadata.json and the MFF table.
data, truth = bayesinsight_dataset(weeks=60, seed=0)
bayesinsight_mff(data).to_csv("sales_mff.csv", index=False)
Path("model_def.json").write_text(json.dumps(BI_MODEL_DEF, indent=2), "utf-8")
Path("metadata.json").write_text(json.dumps(BI_METADATA, indent=2), "utf-8")

# --8<-- [start:import]
from mapi_spec.bayesinsight import import_bayesinsight
from mapi_spec.guided import BayesInsightImport

request = BayesInsightImport(
    name="bi-sales",
    dataset="bi_mff",  # the dataset the MFF upload becomes
    target="mmm.bayesinsight",  # or "mmm.hierarchical_geo" (the default)
    model_def=json.loads(Path("model_def.json").read_text("utf-8")),
    metadata=json.loads(Path("metadata.json").read_text("utf-8")),
)
draft = import_bayesinsight(request, project="retail")  # pure JSON: no server, no numpy
for issue in draft.issues:
    print(f"{issue.severity.value:7} {issue.pointer}: {issue.message[:70]}…")
print(sorted(draft.mapping.dims), sorted(draft.mapping.vars))
# --8<-- [end:import]
assert not [i for i in draft.issues if i.severity.value == "error"]
assert set(draft.mapping.dims) == {"date", "geo", "product"}

# --8<-- [start:compare]
geo = import_bayesinsight(request.model_copy(update={"target": "mmm.hierarchical_geo"}),
                          project="retail")  # fmt: skip
for issue in geo.issues:
    if issue.severity.value == "warning":
        print(f"warning {issue.pointer}: {issue.message[:70]}…")
# --8<-- [end:compare]
assert any(i.severity.value == "warning" for i in geo.issues)

# --8<-- [start:validate]
from mapi_sdk.registry import default_registry
from mapi_spec.guided import draft_schema
from mapi_spec.spec import ModelSpec
from mapi_spec.validate import validate_spec

spec = ModelSpec.model_validate(draft.spec)
check = validate_spec(spec, default_registry().catalog(), draft_schema(draft.mapping))
print("valid" if check.ok else [(e.pointer, e.message) for e in check.errors])
# --8<-- [end:validate]
assert check.ok

# --8<-- [start:fit]
from mapi_runner.jobs import IngestJob
from mapi_runner.worker.ingest import build_dataset
from mapi_sdk.harness import fit_spec

# the sandboxed ingest's own code: the MFF table through the draft's mapping
ds, schema, _ = build_dataset(Path("sales_mff.csv"), IngestJob(mapping=draft.mapping))
quick = spec.model_dump(mode="json", by_alias=True, exclude_defaults=True)
quick["model"]["fit"] = {"mode": "evaluate_then_refit", "engine": "pymc",
                         "tune": 100, "draws": 100, "chains": 2, "seed": 1}  # fmt: skip
bundle = fit_spec(ModelSpec.model_validate(quick), ds, registry=default_registry(),
                  raw_schema=schema)  # fmt: skip
post = bundle.model.posterior.mean(("chain", "draw"))
print({v: round(float(post[f"{v}__coef"]), 1) for v in ("tv", "search", "price")})
print("holdout MAPE %:", round(bundle.metrics.values["mape"], 1))
# --8<-- [end:fit]
assert abs(float(post["tv__coef"]) - truth.coef["TV"]) < 8
assert abs(float(post["search__coef"]) - truth.coef["Search"]) < 6
assert abs(float(post["price__coef"]) - truth.price) < 3
assert bundle.metrics.values["mape"] < 5


def _api(request: httpx.Request) -> httpx.Response:
    """A stand-in for ``POST /v1/guided/import/bayesinsight``: the server's import code."""
    assert request.url.path == "/v1/guided/import/bayesinsight"
    body = BayesInsightImport.model_validate_json(request.content)
    answer = import_bayesinsight(body, project="retail")
    return httpx.Response(200, json=answer.model_dump(mode="json"))


api = Client(
    config=ClientConfig(base_url="https://api.mapi.example", token=str(generate_token()),
                        project="acme/retail"),  # fmt: skip
    transport=httpx.MockTransport(_api),
)
# --8<-- [start:api]
remote = api.guided.import_bayesinsight(request)  # the same draft, from the platform
print(remote.dataset_ref, len(remote.issues), "issues")
# --8<-- [end:api]
assert remote.yaml == draft.yaml
api.close()
