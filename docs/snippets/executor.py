"""Getting started: the same fit and score, submitted as runs to the in-process executor.

In a notebook you can ``await main()`` directly; as a script, ``asyncio.run(main())``.
"""

# --8<-- [start:setup]
import asyncio

from mapi_plugin_tabular.datasets import (
    STORE_DEMAND_SCHEMA,
    STORE_DEMAND_SPEC_YAML,
    store_demand_dataset,
)
from mapi_sdk import InprocExecutor
from mapi_spec.executor import ExecState, RunEventType, RunKind, RunSubmit

history = store_demand_dataset(stores=4, skus=2, weeks=60, seed=1)
recent = history.isel(week=slice(-8, None))  # a window of fitted weeks to re-score

executor = InprocExecutor(
    # dataset refs in specs and requests resolve to in-memory datasets
    resolver={
        "dataset://retail/store_sku_weekly@latest": history,
        "dataset://retail/last_8_weeks@v1": recent,
    },
)
print(executor.caps().isolation.name)  # NONE: your own code, in your own process
# --8<-- [end:setup]


# --8<-- [start:runs]
async def main() -> None:
    fit = await executor.submit(
        RunSubmit(
            run_id="fit-001",
            kind=RunKind.FIT,
            payload={
                "spec": STORE_DEMAND_SPEC_YAML,
                "raw_schema": STORE_DEMAND_SCHEMA.model_dump(mode="json"),
            },
        ),
        nonce="nonce-fit-0001",  # resubmitting the same nonce returns the same run
    )
    status = await executor.wait(fit, timeout=300)
    print(status.state)  # succeeded
    for event in executor.events(fit):  # the same event stream a cloud run will produce
        if event.type is RunEventType.PROGRESS:
            print(event.seq, event.payload.get("message"))  # 2 validated the spec and the data

    executor.put_bundle("store-demand@1", executor.result(fit))
    score = await executor.submit(
        RunSubmit(
            run_id="score-001",
            kind=RunKind.SCORE,
            payload={
                "bundle": "store-demand@1",
                "request": {
                    "apiVersion": "mapi/v1",
                    "kind": "ScoreRequest",
                    "model": "endpoint://retail/store-demand-xgb",
                    "mode": "predict",
                    "data": "dataset://retail/last_8_weeks@v1",
                },
            },
        ),
        nonce="nonce-score-0001",
    )
    assert (await executor.wait(score, timeout=300)).state is ExecState.SUCCEEDED
    tree = executor.result(score)  # an xr.DataTree, exactly what score_bundle returns
    print(dict(tree["summary"].to_dataset()["y_hat"].sizes))
    # {'stat': 3, 'store_id': 4, 'sku': 2, 'week': 8}


asyncio.run(main())  # in Jupyter: `await main()`
# --8<-- [end:runs]
