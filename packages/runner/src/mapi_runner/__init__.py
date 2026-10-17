"""Sandboxed run harness for modeling-api: a trusted supervisor and an untrusted worker.

* :mod:`mapi_runner.supervisor` - bootstrap read-and-delete, worker isolation, signed event
  stream, log/scratch/output caps, uploads and the output manifest (``mapi-runner``);
* :mod:`mapi_runner.worker` - the harness that downloads inputs and runs ``ingest``,
  ``describe``, ``validate``, ``fit``, ``score``, ``evaluate``, ``derive`` and ``render``;
* :mod:`mapi_runner.jobs` - what ``RunSpec.spec`` holds per kind, input/output names;
* :mod:`mapi_runner.channel` - the supervisor <-> worker message protocol.

Importing this package (or the supervisor) never imports the SDK, plugins, numpy or xarray.
"""

__version__ = "0.5.0"
