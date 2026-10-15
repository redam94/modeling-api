"""``python -m mapi_conformance``: run the conformance corpus and print a summary table.

    python -m mapi_conformance run [--size tiny|full] [--fixture ID ...] [--seed N]
                                   [--no-contract] [--json PATH]
    python -m mapi_conformance list

Exit status 0 when every fixture passes, 1 otherwise, 2 for usage errors.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from mapi_conformance.fixtures import FIXTURES
from mapi_conformance.runner import run_corpus


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m mapi_conformance", description=__doc__.split("\n")[0]
    )
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run fixtures and print a summary table")
    run.add_argument("--size", choices=["tiny", "full"], default="tiny")
    run.add_argument(
        "--fixture",
        action="append",
        choices=FIXTURES,
        metavar="ID",
        help=f"fixture to run (repeatable; default all: {', '.join(FIXTURES)})",
    )
    run.add_argument("--seed", type=int, default=0)
    run.add_argument("--no-contract", action="store_true", help="skip check_model")
    run.add_argument("--json", type=Path, help="also write the full report as JSON")
    run.add_argument("-q", "--quiet", action="store_true", help="no progress lines")
    sub.add_parser("list", help="list the registered fixtures")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "list":
        print("\n".join(FIXTURES))
        return 0

    def log(message: str) -> None:
        if not args.quiet:
            print(message, file=sys.stderr, flush=True)

    report = run_corpus(
        args.fixture,
        size=args.size,
        seed=args.seed,
        contract=not args.no_contract,
        log=log,
    )
    print(report.table())
    details = report.failure_details()
    if details:
        print("\nfailures:\n" + details)
    if args.json is not None:
        args.json.write_text(json.dumps(report.to_json(), indent=2, default=str))
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
