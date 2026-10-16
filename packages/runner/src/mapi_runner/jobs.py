"""What ``RunSpec.spec`` holds for each run kind, and the input/output naming conventions.

The contract lives in :mod:`mapi_spec.jobs` (shared with the control plane, which writes the
RunSpecs and reads the outputs); this module re-exports it for the runner's own modules.
"""

from __future__ import annotations

from mapi_spec.jobs import (
    BUNDLE_MANIFEST_OUTPUT,
    DATASET_OUTPUTS,
    DESCRIBE_OUTPUT,
    FIT_BUNDLE,
    FIT_SIDECARS,
    INGEST_DATA,
    INGEST_SCHEMA,
    INPUT_BUNDLE_MANIFEST,
    INPUT_HISTORY,
    INPUT_PLOTS,
    INPUT_SCENARIO,
    INPUT_SCHEMA,
    INPUT_UPLOAD,
    SCORE_INPUTS,
    SCORE_OUTPUT,
    SIDE_TABLE_PREFIX,
    DeriveJob,
    DescribeJob,
    EvaluateJob,
    IngestJob,
    IngestSource,
    Job,
    MffOptions,
    RenderJob,
    ScoreJob,
    parse_job,
    side_table_input,
)

__all__ = [
    "BUNDLE_MANIFEST_OUTPUT",
    "DATASET_OUTPUTS",
    "DESCRIBE_OUTPUT",
    "FIT_BUNDLE",
    "FIT_SIDECARS",
    "INGEST_DATA",
    "INGEST_SCHEMA",
    "INPUT_BUNDLE_MANIFEST",
    "INPUT_HISTORY",
    "INPUT_PLOTS",
    "INPUT_SCENARIO",
    "INPUT_SCHEMA",
    "INPUT_UPLOAD",
    "SCORE_INPUTS",
    "SCORE_OUTPUT",
    "SIDE_TABLE_PREFIX",
    "DeriveJob",
    "DescribeJob",
    "EvaluateJob",
    "IngestJob",
    "IngestSource",
    "Job",
    "MffOptions",
    "RenderJob",
    "ScoreJob",
    "parse_job",
    "side_table_input",
]
