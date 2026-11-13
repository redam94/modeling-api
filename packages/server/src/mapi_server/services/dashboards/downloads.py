"""Downloads of a dashboard: the predictions CSV of a batch readout and the revision's
pictures (v0.17, plan D12, §3.5).

:func:`predictions_csv` renders the CSV **on demand** from the batch run's validated
``summary.json`` and the stored file (pure Python, stdlib ``csv``; no stored CSV, no signed
URL, no run id leaves the server): the id, the inputs of each row, and per output its
centre and range (classifiers: the most likely class and its chance). Only the readout's
own actor (the same share link, the same client user, the same member) or an editor may
download it; anybody else gets ``404``. A readout past its private lifetime, or whose run
output or file is gone, answers ``410 DASHBOARD_DOWNLOAD_GONE``. Cells that a spreadsheet
would run as a formula (``= + - @`` and tab or carriage return first) are prefixed with
``'`` unless they are numbers. Every download is charged to the tenant's egress and audited
(``DASHBOARD_BATCH_DOWNLOAD``).

:func:`asset_bytes` serves a picture of the published revision to a share link (the member
route reads it with :func:`~mapi_server.services.dashboards.assets.read_asset` directly).
"""

from __future__ import annotations

import csv
import io
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from mapi_spec.audit import AuditAction
from mapi_spec.errors import ErrorCode, MapiError
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import NotFoundError
from mapi_server.services.dashboards.access import DashboardContext
from mapi_server.services.dashboards.assets import AssetBytes, read_asset
from mapi_server.services.quotas import charge_egress
from mapi_server.services.runs import now

__all__ = [
    "CSV_MAX_BYTES",
    "PredictionsCsv",
    "asset_bytes",
    "csv_cell",
    "predictions_csv",
    "render_predictions",
]

CSV_MAX_BYTES: Final = 8 * 1024 * 1024
_FORMULA_START: Final = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True, slots=True)
class PredictionsCsv:
    """A rendered predictions file."""

    data: bytes
    filename: str
    rows: int


def _gone(what: str = "the predictions of this readout are no longer available") -> MapiError:
    return MapiError(what, code=ErrorCode.DASHBOARD_DOWNLOAD_GONE)


def _is_number_text(text: str) -> bool:
    try:
        return math.isfinite(float(text))
    except ValueError:
        return False


def csv_cell(value: object) -> str:
    """One CSV cell: numbers as they are, text with a leading ``'`` when a spreadsheet
    would run it as a formula (formula injection)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return (
            repr(value)
            if isinstance(value, float) and math.isfinite(value)
            else (str(value) if isinstance(value, int) else "")
        )
    text = str(value)
    if text.startswith(_FORMULA_START) and not _is_number_text(text):
        return "'" + text
    return text


def _upload_rows(ds: Any, id_column: str) -> dict[str, dict[str, Any]]:
    """The stored file's values per id (the file is one row per case along the id)."""
    coord = ds.coords.get(id_column) if ds is not None else None
    ids = coord.data if coord is not None and isinstance(coord.data, list) else []
    cols = {n: v.data for n, v in ds.data_vars.items() if isinstance(v.data, list)} if ds else {}
    out: dict[str, dict[str, Any]] = {}
    for i, raw in enumerate(ids):
        out[str(raw)] = {n: (data[i] if i < len(data) else None) for n, data in cols.items()}
    return out


def render_predictions(
    summary: Any,
    outputs: Sequence[str],
    *,
    id_dim: str,
    columns: Sequence[str],
    inputs: Mapping[str, Mapping[str, Any]],
    keys: Sequence[Any] | None = None,
) -> tuple[str, int]:
    """The CSV text and its row count: one row per id (in the order the summary gives
    them), the input ``columns`` of that id, then per output ``<name>`` (the centre),
    ``<name>_low`` and ``<name>_high``, or for class probabilities ``<name>`` (the most likely
    class) and ``<name>_probability``. Raises ``DASHBOARD_BATCH_TOO_LARGE`` over
    :data:`CSV_MAX_BYTES`. ``keys`` (:data:`~.panels.BatchKey`): the file's rows when they
    give other dims too, in file order; each row then holds only its own cell."""
    from mapi_server.services.dashboards.panels import _value_of, keyed_groups, table_of

    tables = {o: t for o in outputs if (t := table_of(summary, o)) is not None}
    by_id: dict[str, dict[str, list[Any]]] = {}
    order: list[str] = []
    for name, t in tables.items():
        if keys:
            for label, cells in keyed_groups(t, keys):
                by_id.setdefault(label, {})[name] = cells
            continue
        for c in t.cells:
            key = c.coords.get(id_dim)
            if key is None:
                continue
            if key not in by_id:
                by_id[key] = {}
                order.append(key)
            by_id[key].setdefault(name, []).append(c)
    if keys:
        order = [k for k, _ in keys if k in by_id]
    head = [id_dim, *columns]
    for name, t in tables.items():
        if t.class_dim is not None:
            head += [name, f"{name}_probability"]
        else:
            head += [name, f"{name}_low", f"{name}_high"]
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([csv_cell(h) for h in head])
    for key in order:
        values = inputs.get(key, {})
        line: list[Any] = [key, *(values.get(c) for c in columns)]
        for name, t in tables.items():
            cells = sorted(
                by_id[key].get(name, []), key=lambda c: c.coords.get(t.time_dim or "", "")
            )
            if t.time_dim and t.class_dim is None:
                cells = cells[:1]
            v = _value_of(t, cells, True)
            line += (
                [v.label, v.probability] if t.class_dim is not None else [v.centre, v.low, v.high]
            )
        writer.writerow([csv_cell(x) for x in line])
        if buf.tell() > CSV_MAX_BYTES:
            raise MapiError(
                "the predictions file would be larger than 8 MiB",
                code=ErrorCode.DASHBOARD_BATCH_TOO_LARGE,
            )
    return buf.getvalue(), len(order)


async def predictions_csv(
    ctx: ServerContext,
    session: AsyncSession,
    dctx: DashboardContext,
    seq: int,
    *,
    editor: bool = False,
) -> PredictionsCsv:
    """``GET …/readouts/{seq}/predictions.csv``: the predictions of the caller's own batch
    readout (``editor``: any batch readout of the dashboard). ``404`` for a readout the
    caller may not download or that is not a ready batch; ``410`` once it expired."""
    from mapi_spec.dashboard_v2 import DashboardSpecV2

    from mapi_server.services.dashboards import readouts
    from mapi_server.services.dashboards.access import any_revision_spec
    from mapi_server.services.dashboards.inputs import _audit

    row = await readouts.load_visible(session, dctx, seq)
    own = row.actor_kind == dctx.principal_kind and row.actor_id == dctx.principal_id
    if row.kind != "batch" or not (own or editor):
        raise NotFoundError("readout", str(seq))
    if row.expires_at is not None and row.expires_at <= now():
        raise _gone()
    if row.status == "failed":
        raise _gone("this file gave no predictions: working them out failed")
    if row.status != "ready" or not row.run_ids:
        raise NotFoundError("readout", str(seq))
    ref = row.input_ref if isinstance(row.input_ref, dict) else {}
    rev = await session.get(m.DashboardRevision, row.revision_id)
    spec = any_revision_spec(rev) if rev is not None else None
    if not isinstance(spec, DashboardSpecV2) or spec.batch is None:
        raise NotFoundError("readout", str(seq))
    summary = await readouts._summary_of(ctx, session, row.run_ids[0])
    if summary is None:
        raise _gone()
    upload = await readouts._read_upload(ctx, ref) if ref.get("bucket") else None
    if upload is None:
        raise _gone()
    id_column = str(ref.get("id_column") or spec.batch.id_column)
    id_dim = str(ref.get("id_dim") or id_column)
    columns = [str(c) for c in ref.get("columns") or [] if isinstance(c, str)]
    keys = await readouts._batch_keys(ctx, ref)
    text, n = render_predictions(
        summary, list(spec.batch.outputs), id_dim=id_dim, columns=columns,
        inputs=_upload_rows(upload, id_column), keys=keys,
    )  # fmt: skip
    data = text.encode("utf-8")
    await charge_egress(ctx, dctx.tenant_id, len(data))
    await _audit(
        ctx,
        session,
        dctx,
        AuditAction.DASHBOARD_BATCH_DOWNLOAD,
        {"seq": seq, "rows": n, "bytes": len(data)},
    )
    return PredictionsCsv(data=data, filename=f"predictions-{seq}.csv", rows=n)


async def asset_bytes(
    ctx: ServerContext, session: AsyncSession, dctx: DashboardContext, asset_id: str
) -> AssetBytes:
    """A picture of the caller's published revision (``404`` for any other asset, also of
    an older revision of the same dashboard)."""
    return await read_asset(
        ctx, session, dctx.dashboard_id, asset_id, revision_ids=[dctx.revision_id]
    )
