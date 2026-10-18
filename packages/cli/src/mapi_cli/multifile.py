"""The flag grammar of ``mapi datasets upload`` with several files (M4).

One version is made from every FILE (an ``IngestAssembly``)::

    mapi datasets upload 2024.nc 2025.nc --name cubes --concat year=2024,2025
    mapi datasets upload a.nc b.nc --name obs --merge --join outer
    mapi datasets upload img.nc psf.nc --name lens --node / --node /instrument

* ``--node PATH`` - where a file goes in the tree: once (every file) or once per file, in
  file order. Default ``/``.
* Files that share a node are combined, by ``--concat`` or ``--merge`` (a file alone at
  its node is mounted there as is).
* ``--concat DIM=LABEL`` - stack the files that share a node along ``DIM``, one label per
  combined file in file order: repeat it (``--concat year=2024 --concat year=2025``) or
  list the labels once (``--concat year=2024,2025``). Labels that look like numbers are
  numbers.
* ``--merge`` - merge the variables of the files that share a node (shared coords).
* ``--join exact|inner|outer`` - how coords align for ``--concat``/``--merge`` (default
  ``exact``).
* ``--mapping FILE`` - once (used for every file whose format it reads; self-describing
  files default to an empty mapping) or once per file, in file order.

A single FILE without ``--node``/``--concat``/``--merge`` is a plain single-file upload.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from mapi_client import AssemblyFile
from mapi_spec.api import IngestMapping
from mapi_spec.ingest import CoordValue
from mapi_spec.tree import ROOT

__all__ = ["assembly_files", "is_multi_file", "parse_concat", "parse_label"]

Join = Literal["exact", "inner", "outer"]


def parse_label(text: str) -> CoordValue:
    """``"2024"`` -> 2024, ``"0.5"`` -> 0.5, anything else stays text."""
    raw = text.strip()
    if not raw:
        raise ValueError("a --concat label is empty")
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        number = float(raw)
    except ValueError:
        return raw
    return number if math.isfinite(number) else raw


def parse_concat(items: Sequence[str]) -> tuple[str, list[CoordValue]]:
    """``["year=2024", "year=2025"]`` or ``["year=2024,2025"]`` -> ``("year", [2024, 2025])``."""
    dims: set[str] = set()
    labels: list[CoordValue] = []
    for item in items:
        dim, sep, rest = item.partition("=")
        dim = dim.strip()
        if not sep or not dim:
            raise ValueError(f"--concat must look like DIM=LABEL[,LABEL...], not {item!r}")
        dims.add(dim)
        labels += [parse_label(x) for x in rest.split(",")]
    if len(dims) != 1:
        raise ValueError(f"--concat names one dim, got {sorted(dims)}")
    return dims.pop(), labels


def _per_file[T](values: Sequence[T], n: int, flag: str) -> list[T] | None:
    if not values:
        return None
    if len(values) == 1:
        return [values[0]] * n
    if len(values) != n:
        raise ValueError(f"give {flag} once (every file) or once per file ({n}), not {len(values)}")
    return list(values)


def is_multi_file(
    files: Sequence[Path], nodes: Sequence[str], concat: Sequence[str], merge: bool
) -> bool:
    """Whether the upload makes an assembly (anything but one file at the root)."""
    return len(files) > 1 or bool(nodes) or bool(concat) or merge


def assembly_files(
    files: Sequence[Path],
    *,
    nodes: Sequence[str] = (),
    concat: Sequence[str] = (),
    merge: bool = False,
    join: Join | None = None,
    mappings: Sequence[IngestMapping] = (),
) -> tuple[list[AssemblyFile], IngestMapping | None]:
    """The :class:`AssemblyFile` items of the flags, plus the mapping shared by every file
    (``--mapping`` given once) - see the module doc for the grammar."""
    n = len(files)
    if concat and merge:
        raise ValueError("combine files by --concat or by --merge, not both")
    if join is not None and not (concat or merge):
        raise ValueError("--join only applies with --concat or --merge")
    node_of = _per_file(nodes, n, "--node") or [ROOT] * n
    own = _per_file(mappings, n, "--mapping") if len(mappings) > 1 else None
    shared = mappings[0] if len(mappings) == 1 else None
    count = Counter(node_of)
    combined = [i for i in range(n) if count[node_of[i]] > 1]
    if combined and not (concat or merge):
        shared_at = sorted({node_of[i] for i in combined})
        raise ValueError(
            f"several files go to node {', '.join(shared_at)}: combine them with "
            "--concat DIM=LABEL or --merge, or give each its own --node"
        )
    labels: dict[int, CoordValue] = {}
    dim: str | None = None
    if concat:
        if not combined:
            raise ValueError("--concat needs at least two files at the same node")
        dim, values = parse_concat(concat)
        if len(values) != len(combined):
            raise ValueError(
                f"--concat {dim}: give one label per combined file ({len(combined)}), "
                f"got {len(values)}"
            )
        labels = dict(zip(combined, values, strict=True))
    elif merge and not combined:
        raise ValueError("--merge needs at least two files at the same node")
    out: list[AssemblyFile] = []
    for i, path in enumerate(files):
        mapping = own[i] if own is not None else None
        if i in labels:
            item = AssemblyFile(
                path, mapping, node_of[i], "concat", dim, labels[i], join or "exact"
            )
        elif merge and i in combined:
            item = AssemblyFile(path, mapping, node_of[i], "merge", join=join or "exact")
        else:
            item = AssemblyFile(path, mapping, node_of[i], "node")
        out.append(item)
    return out, shared
