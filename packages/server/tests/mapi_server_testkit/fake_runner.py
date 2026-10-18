"""A stand-in supervisor that follows the run protocol (tests only).

``python fake_runner.py MODE`` reads ``MAPI_RUN_ID``/``MAPI_NONCE``/``MAPI_BOOTSTRAP_URL``/
``MAPI_EXEC_TOKEN``, GETs and DELETEs the RunSpec, prints HMAC-signed ``{"mapi_event": …}``
lines, uploads outputs through the signed POST policy, PUTs a signed ``manifest.json``, emits
``complete`` and exits 0 — or misbehaves on purpose, depending on ``MODE``:

``ok`` · ``fail_user`` (error event, exit 10) · ``oom`` (exit 137) · ``tamper`` (an object not
in the manifest) · ``bad_hmac`` (events signed with the wrong key) · ``hang`` (sleeps until
SIGTERM, then exits 143) · ``partial`` (partial manifest, exit 16).
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from mapi_spec.capabilities import Diagnostic
from mapi_spec.errors import ErrorCode
from mapi_spec.run import (
    OUTPUT_ARTIFACTS,
    OUTPUT_MANIFEST,
    CompletePayload,
    ErrorPayload,
    FailureClass,
    InputDigest,
    LogPayload,
    ManifestObject,
    MetricPayload,
    OutputManifest,
    ProgressPayload,
    RunnerEnv,
    RunnerPhase,
    RunSpec,
    StatePayload,
    WarningPayload,
    encode_event_line,
    make_event,
    new_hmac_key,
    sign_output_manifest,
)
from mapi_spec.scorecard import SCORECARD_OUTPUT, evaluate_check, rollup
from mapi_spec.storage import PostPolicy, SignedPut, crc32c, crc32c_to_base64, md5_base64

SAFE_PLOT: dict[str, Any] = {
    "id": "core.ppc_fan",
    "title": "Posterior predictive fan",
    "kind": "vega-lite",
    "schema_version": "v5",
    "spec": {
        "mark": "line",
        "data": {"name": "fan"},
        "encoding": {
            "x": {"field": "t", "type": "quantitative"},
            "y": {"field": "y", "type": "quantitative"},
        },
    },
    "datasets": {"fan": {"kind": "data_ref", "path": "ui/plotdata/fan.arrow"}},
    "caption_plain": "Where the model expects the data to fall.",
    "provenance": {"recipe": "core.ppc_fan@1.0.0", "generated_at": "2026-10-15T09:40:00Z"},
}
UNSAFE_PLOT: dict[str, Any] = {
    **SAFE_PLOT,
    "id": "evil",
    "title": "Exfiltrate",
    "spec": {
        "mark": "image",
        "data": {"name": "fan"},
        "encoding": {"url": {"value": "https://attacker.example/beacon.png"}},
    },
}


class Supervisor:
    def __init__(self, rs: RunSpec, key: str) -> None:
        self.rs = rs
        self.key = key
        self.seq = 0

    def emit(self, payload: Any) -> None:
        ev = make_event(self.rs.run_id, self.rs.attempt, self.seq, payload, key=self.key)
        self.seq += 1
        sys.stdout.write(encode_event_line(ev) + "\n")
        sys.stdout.flush()

    def post(self, client: httpx.Client, name: str, data: bytes, ctype: str) -> ManifestObject:
        policy = self.rs.outputs[OUTPUT_ARTIFACTS]
        assert isinstance(policy, PostPolicy)
        key = policy.prefix + name
        files = {"file": (name, data, ctype)}
        form = {**policy.fields, "key": key}
        r = client.post(policy.url, data=form, files=files)
        r.raise_for_status()
        return ManifestObject(
            key=key,
            size=len(data),
            crc32c=crc32c_to_base64(crc32c(data)),
            md5=md5_base64(data),
            content_type=ctype,
        )


def outputs_for(rs: RunSpec, mode: str) -> dict[str, tuple[bytes, str]]:
    def js(obj: Any) -> tuple[bytes, str]:
        return json.dumps(obj).encode(), "application/json"

    kind = rs.kind.value
    if kind == "fit":
        diag = Diagnostic(
            code="RHAT_OK",
            severity="info",
            metric="r_hat_max",
            value=1.002,
            threshold=1.01,
            message_expert="max r-hat 1.002",
            message_plain="All chains agree.",
        )
        out = {
            "bundle.zarr.zip": (os.urandom(2048), "application/zip"),
            "diagnostics.json": js([diag.model_dump(mode="json")]),
            "metrics.json": js({"values": {"rmse": 0.25, "mape": None}, "n_holdout": 4}),
            "plots.json": js([SAFE_PLOT, UNSAFE_PLOT]),
            "coords.json": js(
                {
                    "raw": {
                        "t": {
                            "kind": "time",
                            "size": 48,
                            "start": "2020-01-01",
                            "end": "2023-12-01",
                            "freq": "MS",
                            "contiguous": True,
                        }
                    },
                }
            ),
            "ui/summaries.json": js({"y": {"mean": 1.0}}),
            # an M3 runner's scorecard: converged, and evaluated on the holdout (Q13)
            SCORECARD_OUTPUT: js(
                rollup(
                    [
                        evaluate_check("convergence.rhat", 1.002),
                        evaluate_check("holdout.mape", 0.05),
                    ]
                ).model_dump(mode="json")
            ),
        }
        if mode == "partial":
            out.pop("bundle.zarr.zip")
        return out
    if kind == "ingest":
        schema = {
            "dims": {"t": {"kind": "time", "policy": "extend", "freq": "MS"}},
            "vars": {"y": {"dims": ["t"], "dtype": "float64", "role": "target"}},
        }
        return {
            "data.zarr.zip": (os.urandom(4096), "application/zip"),
            "schema.json": js(schema),
            "summary.json": js({"vars": ["y"], "coords": {"t": {"kind": "time", "size": 48}}}),
            "preview.json": js({"rows": [[1.0]]}),
            "columns.json": js(
                [
                    {"name": "date", "dtype": "date", "n_rows": 48, "n_distinct": 48,
                     "detected_freq": "MS", "monotonic": True, "origin": "column"},
                    {"name": "co2", "dtype": "number", "n_rows": 48, "min": 300.0,
                     "max": 420.0, "origin": "column"},
                ]
            ),
        }  # fmt: skip
    if kind == "describe":
        path = os.environ["FAKE_RUNNER_CATALOG"]
        out = {"catalog.json": (Path(path).read_bytes(), "application/json")}
        if examples := os.environ.get("FAKE_RUNNER_PLOT_EXAMPLES"):
            out["plot_examples.json"] = (Path(examples).read_bytes(), "application/json")
        return out
    if kind == "score":
        return {
            "result.json": js({"ok": True, "kind": kind}),
            "plots.json": js([SAFE_PLOT, UNSAFE_PLOT]),
        }
    return {"result.json": js({"ok": True, "kind": kind})}


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "ok"
    env = RunnerEnv.from_environ(os.environ)
    headers = {"Authorization": f"Bearer {env.exec_token}"} if env.exec_token else {}
    with httpx.Client(timeout=30) as client:
        r = client.get(env.resolved_bootstrap_url(), headers=headers)
        if r.status_code != 200:
            return 2
        client.delete(env.resolved_bootstrap_url(), headers=headers)
        rs = RunSpec.model_validate_json(r.content)
        if rs.run_id != env.run_id or rs.nonce != env.nonce:
            return 2
        key = new_hmac_key() if mode == "bad_hmac" else rs.hmac_key
        sup = Supervisor(rs, key)
        if mode == "silent_hang":
            # never emit an event: the run stays in PROVISIONING until the deadline or a kill.
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
            time.sleep(600)
        sup.emit(StatePayload(phase=RunnerPhase.BOOTSTRAP))
        sup.emit(ProgressPayload(phase=RunnerPhase.FIT, fraction=0.5, message="halfway"))
        sup.emit(LogPayload(stream="stderr", line="sampling chain 0"))
        if mode == "hang":
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
            time.sleep(600)
        if mode == "fail_user":
            sup.emit(
                ErrorPayload(
                    code=ErrorCode.USER_CODE_ERROR,
                    failure_class=FailureClass.USER,
                    message="the model plugin raised an exception",
                    untrusted_text="Traceback (most recent call last):\n  ValueError: boom",
                )
            )
            return 10
        if mode == "oom":
            return 137
        sup.emit(MetricPayload(name="rmse", value=0.25))
        sup.emit(WarningPayload(code="SLOW_SAMPLING", message="sampling took long"))
        objects: dict[str, ManifestObject] = {}
        for name, (data, ctype) in outputs_for(rs, mode).items():
            objects[name] = sup.post(client, name, data, ctype)
        if mode == "tamper":
            sup.post(client, "sneaky.bin", b"not in the manifest", "application/octet-stream")
        status = "partial" if mode == "partial" else "complete"
        manifest = OutputManifest(
            tenant_id=rs.tenant_id,
            project_id=rs.project_id,
            run_id=rs.run_id,
            attempt=rs.attempt,
            kind=rs.kind,
            status=status,
            prefix=rs.outputs[OUTPUT_ARTIFACTS].prefix,  # type: ignore[union-attr]
            spec_hash=rs.spec_hash,
            catalog_digest=rs.catalog_digest,
            inputs={
                n: InputDigest(generation=g.generation, size=g.size, crc32c=g.crc32c)
                for n, g in rs.inputs.items()
            },
            objects=objects,
            runner_version="fake-0",
            lib_versions={"fake": "0"},
            created_at=datetime.now(UTC),
        )
        signed = sign_output_manifest(manifest, rs.hmac_key)
        body = signed.model_dump_json().encode()
        put = rs.outputs[OUTPUT_MANIFEST]
        assert isinstance(put, SignedPut)
        r = client.put(put.url, content=body, headers=put.headers)
        r.raise_for_status()
        sup.emit(
            CompletePayload(
                status=status,
                manifest_name=put.name,
                manifest_crc32c=crc32c_to_base64(crc32c(body)),
                objects=len(objects),
                total_bytes=sum(o.size for o in objects.values()),
            )
        )
    return 16 if mode == "partial" else 0


if __name__ == "__main__":
    sys.exit(main())
