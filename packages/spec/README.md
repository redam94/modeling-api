# mapi-spec

Pure-Pydantic contracts for modeling-api: the declarative documents users write
(`ModelSpec`, `ScoreRequest`, `Sweep`, `Backtest`, `SBC`, `Campaign`), the data contract
(`DataSchema`, dim policies, roles), step `SchemaEffect`s, model manifests and capabilities,
the `PlotSpec` envelope, stable error codes, and the **static validation** the control plane
runs on every submit.

`mapi_spec` never imports numpy, xarray or pandas (enforced by import-linter). Everything here
is JSON in, JSON out, so the API server can validate specs without importing plugin code or
decoding user arrays.

License: Apache License 2.0.

## Install

```bash
uv add mapi-spec            # or: pip install mapi-spec
```

Dependencies: `pydantic>=2.9`, `pyyaml`, `jsonschema`.

## Quick start

```python
from mapi_spec import load_model_spec, spec_hash, canonical_json
from mapi_spec.examples import E33_YAML

spec = load_model_spec(E33_YAML)  # strict YAML 1.2 / JSON, typed result
print(spec.metadata.name, spec_hash(spec))  # eu-geo-mmm sha256:...
print([s.id for s in spec.pipeline])  # ['to_eur', 'weekly', 'scale_spend', ...]
```

Validate against a catalog and the dataset's raw schema (all static JSON):

```python
from mapi_spec import Catalog, DataSchema, validate_spec

result = validate_spec(spec, catalog, raw_schema)  # catalog: Catalog, raw_schema: DataSchema
for issue in result.issues:
    print(issue.severity, issue.code, issue.pointer, issue.message)
resolved = result.raise_for_errors()  # ResolvedSpec (store it with the run)
resolved.stages["weekly"]  # DataSchema after the 'weekly' step
resolved.split_stage  # 'weekly'
```

Check a score request against a fitted model's metadata (CoordPlan, no arrays):

```python
from mapi_spec import ScoreMetadata, load_score_request, validate_score_request

meta = ScoreMetadata(resolved=resolved, fit_coords={"scale_spend": {...}})
issues = validate_score_request(load_score_request(text), meta, inputs=data_summary)
```

From the shell:

```bash
python -m mapi_spec check my-spec.yaml          # prints "<kind> sha256:<hash>" or a problem+json
python -m mapi_spec check my-spec.yaml --canonical
python -m mapi_spec export-schemas ./schemas    # modelspec.json, scorerequest.json, ...
```

## Module map

| Module | What it holds |
|---|---|
| `schema` | `DimPolicy` (`fixed`/`extend`/`new_levels`), `DimSpec`, `Role`, `VarSpec`, `RaggedSpec`, `DataSchema`, `DataSchemaTemplate` |
| `effects` | `StepKind`, `SchemaEffect`, `ContextNeed`, `apply_effect`, `propagate` |
| `spec` | `ModelSpec`, `ScoreRequest`, `Sweep`, `Backtest`, `SBC`, `Campaign` and their blocks |
| `loader` | strict YAML 1.2/JSON loader, apiVersion upgrades, `canonical_json`, `spec_hash`, `dump_yaml` |
| `capabilities` | `Representation` + aggregation/summary rules, `Capabilities`, `ModelManifest`, `OutputSelector`, `Diagnostic` |
| `errors` | `ErrorCode`, `ERROR_INFO` (HTTP status), RFC 7807 `Problem`, `Issue`, `MapiError` hierarchy |
| `plots` | `PlotSpec` envelope, `DataRef`/`InlineArrow`, `PlotBindings`, `PlotProvenance` |
| `catalog` | `StepCatalogEntry`, `ModelCatalogEntry`, `PlotCatalogEntry`, `Catalog` (semver resolution) |
| `validate` | `validate_spec`, `validate_score_request`, `ResolvedSpec`, `ScoreMetadata` |
| `refs` | `Version`, `VersionRange`, `UseRef`, `DatasetRef`, `EndpointRef`, `ModelVersionRef`, `SpecRef` |
| `coords` | `CoordSummary`, `DataSummary`, pandas-compatible frequency arithmetic |
| `template` | the tiny placeholder language used by schema templates and effects |
| `sweep` | `expand_sweep` (grid → child specs), `set_pointer` |
| `executor` | the `Executor` protocol, `RunSubmit`, `Handle`, `ExecStatus`, `ExecutorCaps`, `Isolation`, `RunKind`, `RunEvent` |
| `run` | run protocol (M1): `RunState` + `transition()`, `RunSpec` bootstrap, event payloads and HMAC signing, stdout wire format, `OutputManifest`/`BundleManifest`, `RunnerEnv`, `RunnerExit` |
| `storage` | `ObjectMeta`, the async `ObjectStore` protocol, signed URL shapes, CRC32C/MD5 helpers, bucket and key naming |
| `api` | REST DTOs for `/v1` (`Page[T]`, `Run`, `RunCreate`, `ModelVersion`, ...), SSE/`Prefer`/idempotency helpers, `M1_ENDPOINTS` |
| `localdev` | local ports, origins and environment variable names (`LocalDev`) |
| `jsonschema_export` | `export_schemas()` (JSON Schema 2020-12 for every document, run-protocol model and REST DTO) |
| `examples` | the plan's example documents (E33 geo MMM, xgboost store demand, score request) |

## Rules worth knowing

### Strict loader

* At most **256 KB**, nesting **depth 32**, **10 000 nodes** (`Limits` to change).
* YAML **anchors, aliases, tags, `%TAG` directives and multi-document streams are rejected**,
  so "billion laughs" and `!!python/object` payloads never get past the parser.
* Plain scalars follow the **YAML 1.2 core schema**: `yes`, `on`, `0777`-is-777, dates and
  `1:20` are not magically converted. NaN/Infinity and duplicate keys are rejected; mapping
  keys must be strings.
* Every model forbids unknown fields (`extra="forbid"`); documents are discriminated on `kind`.
* Validation errors become `Issue`s with **RFC 6901 JSON pointers** into the submitted
  document (union tags are stripped from pydantic's locations).

### Canonical form and hashing

`canonical_json(doc)` omits defaults, sorts keys, uses compact separators and normalises
integral floats to integers (and `90m` to `1h30m`). Writing a default explicitly, reordering
keys or writing `1.0` instead of `1` therefore does not change `spec_hash(doc)`. Revisions are
identified by this hash.

### apiVersion

`mapi/v1` is current. `register_upgrader(old, new, fn)` adds a converter; `upgrade()` walks the
chain and `parse_document(..., accepted=(N, N-1))` accepts the previous version.

### Schema effects and propagation

A step's `SchemaEffect` describes its schema change without data. Inside one effect the order
is `renames → drops → transforms_dims → stacks → updates → derives → adds`. Exported effects
may reference settings with placeholders (all names must be listed in `template_params`):

```json
{"template_params": ["vars", "to"], "updates": {"{*vars as v}": {"units": "{to}"}}}
```

Placeholder syntax: `"{p}"` (typed value), `"x_{p}"` (interpolation), `"{a.b}"`,
`"{min:p}"`/`"{max:p}"`/`"{len:p}"`, `"{*p}"` (splice a list) and `"pre{*p as x}post"` (one
mapping entry per list item). A `dynamic: true` effect stops static propagation with a
`SCHEMA_DYNAMIC` warning (an explicit dry-run resolves it).

### Split stage and leakage

The split happens at `data.split.after_step` (a step id or `raw`). By default it is the end of
the **stateless prefix**: the leading steps that have no fitted state and no context need
(E33: after `resample`, so the weekly `time` dim exists). Any stateful step before the split
is `LEAKAGE_RISK` (an error unless `allow_leakage: true`). The split dims must exist at the
split stage (`SPLIT_INVALID` otherwise).

### Dim policies

`fixed` is tighter than both `extend` and `new_levels`. The effective policy of a model dim is
the meet of the data's and the plugin's; `model.settings.dim_policies` (platform-reserved) may
only tighten it (`DIM_POLICY_LOOSENED` otherwise).

### Recursive forecasting

A step whose catalog entry is `depends_on_target` and actually lags a target-role variable
(`target_vars_param`) with `min_lag` smaller than the holdout length or score horizon is
rejected with `RECURSIVE_UNSUPPORTED`, unless the model `owns: [recursion]`.

### Representations

| Representation | Aggregation | Summaries |
|---|---|---|
| `draws` | any | mean, sd, median, point, hdi_N, eti_N, qN |
| `quantiles` | none | median, qN, eti_N, point |
| `interval` | none | interval, point |
| `point` | linear (sum/mean), flagged | mean, point |
| `moments` | none | mean, sd, eti_N, point |

### CoordPlan checks (score time, from metadata)

`validate_score_request` reports `MODE_NOT_SUPPORTED`, `UNSEEN_LEVELS`, `COORD_NOT_ALLOWED`,
`HORIZON_EXCEEDS_FIT` (409), `MISSING_FUTURE_COVARIATE`, `NON_CONTIGUOUS_EXTENSION`,
`RECURSIVE_UNSUPPORTED`, `SIDE_TABLE_COVERAGE` (`score_policy: supplied` tables needed by steps
after `inputs_stage`), `CAPABILITY_UNSUPPORTED` (draws/summaries/aggregation not valid for the
representation) and `QUOTA_EXCEEDED` (draw caps). User-supplied values (coordinate levels,
jsonschema messages) only ever appear in `untrusted_text`, never in `message`.

## Error codes

`ErrorCode` values are stable; `ERROR_INFO[code]` gives the HTTP status, title and category,
and `MapiError.to_problem()` renders an RFC 7807 `application/problem+json` body whose
`errors` list carries every `Issue` with its pointer.

## Development

```bash
uv run pytest packages/spec -q
uv run mypy --strict packages/spec/src
uv run ruff check packages/spec
MAPI_HYPOTHESIS_EXAMPLES=2000 uv run pytest packages/spec/tests/test_roundtrip_properties.py
```
