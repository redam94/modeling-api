"""OS/sandbox-level probes (plan §9): egress, DNS, filesystem, /proc, fork, memory, disk.

Every probe attempts the hostile action and reports ALLOWED if it succeeded (the control is
absent) or BLOCKED if a control refused it. All are bounded and leave nothing behind:

* the fork bomb spawns a capped number of short-lived children and reaps them;
* the memory bomb allocates a modest capped buffer and frees it immediately;
* the disk bomb writes a capped file into a scratch directory and deletes it;
* network probes use reserved addresses / the ``.invalid`` TLD and a short timeout.

Under the real sandbox these run as the unprivileged worker (uid 10001) with a deny-all
egress firewall, a DNS response policy, a read-only rootfs and seccomp; here (local-process
dev) there is no such boundary, so most return ALLOWED — which is the point the suite makes.
"""

from __future__ import annotations

import contextlib
import os
import socket
import tempfile
from pathlib import Path

from mapi_security.outcomes import Category, Outcome, ProbeResult
from mapi_security.probes import Probe

_CONNECT_TIMEOUT = 1.5
_METADATA_IP = "169.254.169.254"  # GCP/AWS link-local metadata server
_FORK_CAP = 16
_MEM_CAP_BYTES = 64 * 1024 * 1024  # 64 MiB: enough to prove no cgroup limit, safe on a laptop
_DISK_CAP_BYTES = 16 * 1024 * 1024


def _result(pid: str, outcome: Outcome, detail: str, **ev: object) -> ProbeResult:
    return ProbeResult(pid, Category.ISOLATION, outcome, detail, ev)


def metadata_token() -> ProbeResult:
    """Fetch an instance token from the cloud metadata server."""
    pid = "metadata_token"
    try:
        with socket.create_connection((_METADATA_IP, 80), timeout=_CONNECT_TIMEOUT) as s:
            s.sendall(
                b"GET /computeMetadata/v1/instance/service-accounts/default/token HTTP/1.0\r\n"
                b"Metadata-Flavor: Google\r\nHost: metadata.google.internal\r\n\r\n"
            )
            data = s.recv(64)
        return _result(pid, Outcome.ALLOWED, "reached the metadata server", bytes=len(data))
    except OSError as exc:
        return _result(
            pid, Outcome.BLOCKED, f"metadata server unreachable: {exc.__class__.__name__}"
        )


def http_egress() -> ProbeResult:
    """Open an outbound TCP connection to a public address."""
    pid = "http_egress"
    try:
        # TEST-NET-1 (RFC 5737): routable syntactically, never answers, so a successful
        # connect would mean a real egress path exists.
        with socket.create_connection(("203.0.113.1", 443), timeout=_CONNECT_TIMEOUT):
            return _result(pid, Outcome.ALLOWED, "opened an outbound TCP connection")
    except OSError as exc:
        return _result(pid, Outcome.BLOCKED, f"egress refused: {exc.__class__.__name__}")


def dns_egress() -> ProbeResult:
    """Resolve a unique subdomain (DNS tunnelling / exfiltration)."""
    pid = "dns_egress"
    name = f"{os.urandom(8).hex()}.probe.mapi-security.invalid"  # .invalid: never resolves
    try:
        socket.getaddrinfo(name, None)
        return _result(pid, Outcome.ALLOWED, "resolved an arbitrary name", name=name)
    except OSError as exc:
        return _result(
            pid, Outcome.BLOCKED, f"resolution blocked: {exc.__class__.__name__}", name=name
        )


def write_outside_scratch() -> ProbeResult:
    """Write a file outside the run's scratch/output area (e.g. the home directory)."""
    pid = "write_outside_scratch"
    target = Path.home() / ".mapi-security-probe"
    try:
        target.write_text("probe")
        target.unlink()
        return _result(pid, Outcome.ALLOWED, f"wrote outside scratch at {target}")
    except OSError as exc:
        return _result(pid, Outcome.BLOCKED, f"write refused: {exc.__class__.__name__}")


def reuse_foreign_url() -> ProbeResult:
    """Reuse a signed URL minted for another run/tenant (connect to the object store)."""
    pid = "reuse_foreign_url"
    # Standalone we cannot hold a real foreign URL; the OS-level analogue is whether we can
    # even reach an object-store endpoint from inside the sandbox. In the cloud the only
    # reachable endpoint is the restricted VIP, and a URL for another tenant/generation fails
    # verification at mint time (plan §8.6) — exercised by platform.reuse_url_rejected.
    try:
        with socket.create_connection(("storage.googleapis.com", 443), timeout=_CONNECT_TIMEOUT):
            return _result(pid, Outcome.ALLOWED, "reached an object-store endpoint")
    except OSError as exc:
        return _result(pid, Outcome.BLOCKED, f"object store unreachable: {exc.__class__.__name__}")


def read_proc1_environ() -> ProbeResult:
    """Read ``/proc/1/environ`` (the init/supervisor process environment)."""
    pid = "read_proc1_environ"
    path = Path("/proc/1/environ")
    if not path.exists():
        return ProbeResult(pid, Category.ISOLATION, Outcome.SKIPPED, "no procfs on this host")
    try:
        data = path.read_bytes()
        return _result(pid, Outcome.ALLOWED, "read /proc/1/environ", bytes=len(data))
    except OSError as exc:
        return _result(pid, Outcome.BLOCKED, f"refused: {exc.__class__.__name__}")


def read_supervisor_mem() -> ProbeResult:
    """Read the parent (supervisor) process memory via ``/proc/<ppid>/mem``."""
    pid = "read_supervisor_mem"
    ppid = os.getppid()
    path = Path(f"/proc/{ppid}/mem")
    if not path.exists():
        return ProbeResult(pid, Category.ISOLATION, Outcome.SKIPPED, "no procfs on this host")
    try:
        with path.open("rb") as fh:
            fh.read(1)
        return _result(pid, Outcome.ALLOWED, f"read parent memory (pid {ppid})")
    except OSError as exc:
        return _result(pid, Outcome.BLOCKED, f"ptrace/mem refused: {exc.__class__.__name__}")


def pth_injection() -> ProbeResult:
    """Drop a ``.pth`` file into site-packages so arbitrary code runs on the next import."""
    pid = "pth_injection"
    import site
    import sysconfig

    candidates: list[str] = []
    getter = getattr(site, "getsitepackages", None)
    if getter is not None:
        candidates.extend(getter())
    candidates.append(sysconfig.get_paths()["purelib"])
    for d in candidates:
        target = Path(d) / "mapi_security_probe.pth"
        try:
            target.write_text("import os\n")
            target.unlink()
            return _result(pid, Outcome.ALLOWED, f"wrote a .pth into {d}")
        except OSError:
            continue
    return _result(pid, Outcome.BLOCKED, "site-packages is not writable")


def fork_bomb() -> ProbeResult:
    """Spawn a bounded burst of child processes (tests the process-count limit)."""
    pid = "fork_bomb"
    if not hasattr(os, "fork"):
        return ProbeResult(pid, Category.ISOLATION, Outcome.SKIPPED, "os.fork unavailable")
    children: list[int] = []
    try:
        for _ in range(_FORK_CAP):
            child = os.fork()
            if child == 0:
                os._exit(0)  # child: exit immediately
            children.append(child)
    except OSError as exc:
        for c in children:
            _reap(c)
        return _result(
            pid, Outcome.BLOCKED, f"fork limited after {len(children)}: {exc.__class__.__name__}"
        )
    for c in children:
        _reap(c)
    return _result(
        pid, Outcome.ALLOWED, f"spawned {len(children)} children", children=len(children)
    )


def _reap(child: int) -> None:
    with contextlib.suppress(OSError):
        os.waitpid(child, 0)


def memory_bomb() -> ProbeResult:
    """Allocate a capped buffer (tests the memory cgroup limit), then free it."""
    pid = "memory_bomb"
    try:
        buf = bytearray(_MEM_CAP_BYTES)
        buf[::4096] = b"\x01" * len(buf[::4096])  # touch pages so they are actually committed
        n = len(buf)
        del buf
        return _result(pid, Outcome.ALLOWED, f"allocated {n} bytes", bytes=n)
    except MemoryError:
        return _result(pid, Outcome.BLOCKED, "allocation hit a memory limit")


def disk_bomb() -> ProbeResult:
    """Write a capped file into scratch (tests the scratch budget), then delete it."""
    pid = "disk_bomb"
    try:
        with tempfile.NamedTemporaryFile(prefix="mapi-security-disk-", delete=True) as fh:
            chunk = b"\0" * (1024 * 1024)
            written = 0
            while written < _DISK_CAP_BYTES:
                fh.write(chunk)
                written += len(chunk)
            fh.flush()
        return _result(pid, Outcome.ALLOWED, f"wrote {written} bytes to scratch", bytes=written)
    except OSError as exc:
        return _result(pid, Outcome.BLOCKED, f"scratch write limited: {exc.__class__.__name__}")


def log_flood() -> ProbeResult:
    """Emit far more stdout than the per-run log cap (tests log_flood enforcement)."""
    pid = "log_flood"
    # We do not actually spew megabytes to the test's stdout; we verify we *can* write, which
    # at PROCESS level we always can. The real cap lives in the supervisor's LogGate, which
    # raises SANDBOX_VIOLATION(log_flood) past 20 MB/run — exercised by the runner tests.
    try:
        os.write(1, b"")  # stdout is writable by this process
        return _result(pid, Outcome.ALLOWED, "stdout is writable by user code")
    except OSError as exc:
        return _result(pid, Outcome.BLOCKED, f"stdout not writable: {exc.__class__.__name__}")


PROBES: tuple[Probe, ...] = (
    Probe(
        "metadata_token",
        Category.ISOLATION,
        "fetch a token from the metadata server",
        metadata_token,
    ),
    Probe("http_egress", Category.ISOLATION, "open an outbound TCP connection", http_egress),
    Probe("dns_egress", Category.ISOLATION, "resolve a unique subdomain (DNS exfil)", dns_egress),
    Probe(
        "write_outside_scratch",
        Category.ISOLATION,
        "write outside the run's output area",
        write_outside_scratch,
    ),
    Probe(
        "reuse_foreign_url",
        Category.ISOLATION,
        "reach the object store / reuse a URL",
        reuse_foreign_url,
    ),
    Probe("read_proc1_environ", Category.ISOLATION, "read /proc/1/environ", read_proc1_environ),
    Probe(
        "read_supervisor_mem",
        Category.ISOLATION,
        "read the supervisor's memory",
        read_supervisor_mem,
    ),
    Probe("pth_injection", Category.ISOLATION, "inject a .pth into site-packages", pth_injection),
    Probe("fork_bomb", Category.ISOLATION, "spawn a bounded burst of processes", fork_bomb),
    Probe("memory_bomb", Category.ISOLATION, "allocate past the memory limit", memory_bomb),
    Probe("disk_bomb", Category.ISOLATION, "fill the scratch budget", disk_bomb),
    Probe("log_flood", Category.ISOLATION, "flood the log channel", log_flood),
)
