"""mapi-workflows: nest statistical tests, data processing and models into automated DAGs.

The package has two layers:

* the **core** (:mod:`mapi_workflows.engine`, :mod:`.orchestrator`, :mod:`.payloads`,
  :mod:`.reference`) is pure Python over :mod:`mapi_spec`: it decides what runs, evaluates
  branch conditions with the safe expression language, computes comparisons, selections and
  reports, and keys the result cache. The control plane imports it; it never touches data.
* the **runtime** (:mod:`mapi_workflows.runtime`, the ``runtime`` extra) holds the node
  implementations that work on data - statistical tests (statsmodels/scipy), transforms,
  split plans, order searches, in-process model fits and backtests, benchmarks - and an
  in-process runner for notebooks, the CLI and tests. It runs inside the runner sandbox.
"""

from mapi_workflows.engine import Action, CacheHit, EngineEvent, WorkflowEngine
from mapi_workflows.orchestrator import BoundValue, OrchestratorContext, run_orchestrator_node

__all__ = [
    "Action",
    "BoundValue",
    "CacheHit",
    "EngineEvent",
    "OrchestratorContext",
    "WorkflowEngine",
    "run_orchestrator_node",
]
