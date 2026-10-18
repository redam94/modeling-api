"""``python -m mapi_security`` — run the probe corpus or list it.

    python -m mapi_security run --executor local-process
    python -m mapi_security run --executor docker-gvisor --image mapi/worker:0.8.0 --json out.json
    python -m mapi_security list

``run`` exits non-zero when any probe outcome is unacceptable for the executor's isolation
level (the signal ``make security-suite`` and CI key on). On an enforcing executor
(docker-gvisor, cloud) that means **any** sandbox probe that was ALLOWED fails the run.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from mapi_security.probes import ALL_PROBES
from mapi_security.report import build_report
from mapi_security.runner import (
    EXECUTORS,
    CloudExecutionRequired,
    _main_json,
    evaluate,
    isolation_for,
)

__all__ = ["main"]


def _cmd_list() -> int:
    print(f"{len(ALL_PROBES)} probes:\n")
    for p in ALL_PROBES:
        print(f"  {p.probe_id:<24} [{p.category.value:<9}] {p.description}")
    print("\nexecutors:", ", ".join(EXECUTORS))
    return 0


def _cmd_run(executor: str, image: str | None, json_path: str | None, metrics: str | None) -> int:
    try:
        results = evaluate(executor, image=image)
    except CloudExecutionRequired as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    report = build_report(executor, isolation_for(executor), results)
    print(report.table())
    if json_path:
        report.write_json(Path(json_path))
    if metrics:
        report.write_metrics(Path(metrics))
    return 0 if report.ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mapi_security", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the probe corpus against an executor")
    run.add_argument("--executor", choices=sorted(EXECUTORS), default="local-process")
    run.add_argument("--image", default=None, help="runner image for the docker executors")
    run.add_argument("--json", default=None, help="write the full report as JSON to this path")
    run.add_argument("--metrics", default=None, help="write a Prometheus textfile to this path")

    sub.add_parser("list", help="list the probe corpus")

    # internal: emit probe results as JSON (used when running inside a container)
    probe = sub.add_parser("probe", help=argparse.SUPPRESS)
    probe.add_argument("--category", choices=["isolation", "platform"], default=None)
    probe.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "list":
        return _cmd_list()
    if args.command == "probe":  # pragma: no cover - runs inside containers
        return _main_json(args.category)
    return _cmd_run(args.executor, args.image, args.json, args.metrics)


if __name__ == "__main__":
    sys.exit(main())
