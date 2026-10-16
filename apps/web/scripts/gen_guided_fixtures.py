"""Write ``apps/web/src/mocks/guided-templates.gen.json`` for the mock API.

The mock-mode Guided flow (``src/mocks/guided.ts``: the dev server in ``--mode mock``, the
Playwright smoke suite and the tutorial screenshots) serves the real built-in templates of
:data:`mapi_spec.guided.GUIDED_TEMPLATES`, serialised exactly as ``GET /v1/guided/templates``
returns them, so the screens are exercised against the questions, defaults and ``ui_levels``
the server ships.

Usage::

    uv run python apps/web/scripts/gen_guided_fixtures.py [--check]

``--check`` exits 1 when the committed file differs from what would be written.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mapi_spec.guided import GUIDED_TEMPLATES

WEB = Path(__file__).resolve().parent.parent
OUT = WEB / "src" / "mocks" / "guided-templates.gen.json"


def render() -> str:
    templates = [
        GUIDED_TEMPLATES[k].model_dump(mode="json", by_alias=True) for k in sorted(GUIDED_TEMPLATES)
    ]
    return json.dumps({"items": templates, "next_cursor": None}, indent=1, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the committed file is stale")
    args = parser.parse_args(argv)
    text = render()
    if args.check:
        if not OUT.exists() or OUT.read_text("utf-8") != text:
            print(
                f"{OUT} is stale: run uv run python apps/web/scripts/gen_guided_fixtures.py",
                file=sys.stderr,
            )
            return 1
        return 0
    OUT.write_text(text, "utf-8")
    print(f"wrote {OUT.relative_to(WEB)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
