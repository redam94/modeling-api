"""Release records of deploy/gcp (stdlib only; run with ``python3 -I``).

deploy.sh records every successful apply as ``<UTC stamp>-<id>[-rollback].json`` under
``gs://<state bucket>/mapi/ENV/releases/``. ``<id>`` names *what was deployed*: the first 12
hex digits of the SHA-256 of the images map (so a retried deploy of the same images gets the
same id, whatever the operator's checkout), or, for a rollback, the id of the release it
redeployed. ``-rollback`` marks records written by rollback.sh.

Commands:
  id FILE      print the release id of an images file
  previous     read release names (one per line, any order) on stdin and print the newest
               record of the release before the one now deployed

``previous`` replays the history as a stack: a deploy pushes its release (unless it is
already on top: a retry), a rollback pops back to the release it redeployed. So after
R1, R2 (bad), a rollback to R1 and another ``--previous`` you get R0, never R2 again.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys

NAME = re.compile(r"^(?P<stamp>[0-9]{8}T[0-9]{6}Z)-(?P<id>[A-Za-z0-9]+)(?P<rb>-rollback)?\.json$")


def release_id(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        images = json.load(fh).get("images", {})
    canonical = json.dumps(images, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()[:12]


def previous(names: list[str]) -> str:
    """The record to redeploy for ``rollback.sh --previous``."""
    parsed = sorted((m.group("stamp"), m) for n in names if (m := NAME.match(n.strip())))
    stack: list[tuple[str, str]] = []  # (release id, newest record name of it)
    for _, m in parsed:
        rid, name = m.group("id"), m.group(0)
        if m.group("rb") and any(r == rid for r, _ in stack):
            while stack[-1][0] != rid:
                stack.pop()
            stack[-1] = (rid, name)
        elif stack and stack[-1][0] == rid:
            stack[-1] = (rid, name)  # the same release again (a retried deploy)
        else:
            stack.append((rid, name))
    if len(stack) < 2:
        raise LookupError("no earlier release than the one deployed now is recorded")
    return stack[-2][1]


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "id":
        print(release_id(argv[1]))
        return 0
    if argv == ["previous"]:
        try:
            print(previous(sys.stdin.read().split()))
        except LookupError as exc:
            print(exc, file=sys.stderr)
            return 1
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
