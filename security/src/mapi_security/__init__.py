"""modeling-api security probe suite (plan §9, §13).

A corpus of deliberately hostile probes and a runner that reports, per executor isolation
level, which attacks were BLOCKED and which were ALLOWED, against an expected-outcome matrix.
This package is **not installed by default** (it is outside the uv workspace globs) and its
malicious model plugin is never registered through entry points — it is loaded explicitly,
only by the suite.

Entry points:

* ``python -m mapi_security run --executor local-process|docker|docker-gvisor`` — run the
  corpus and print the pass/fail table (and optionally JSON / a Prometheus textfile);
* :data:`mapi_security.probes.ALL_PROBES` — the corpus;
* :func:`mapi_security.matrix.acceptable` — the expected-outcome matrix.
"""

from __future__ import annotations

__version__ = "0.5.0"

__all__ = ["__version__"]
