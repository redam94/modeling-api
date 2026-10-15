# Threat model mapping

Each probe in the suite maps to a row of the plan's §9 threat model and the control that is
expected to stop it on an enforcing executor (docker-gvisor / Cloud Run microVM). This is the
checklist the nightly suite verifies stays green (cut line B requires two weeks green, §14 M5).

| Probe | §9 threat row | Expected control (GVISOR+) |
|---|---|---|
| `metadata_token` | Credential theft | Deny-all egress firewall; restricted VIP only; no metadata route reachable from the worker |
| `http_egress` | Data exfiltration over IP | Deny-all egress; restricted VIP + VPC-SC perimeter |
| `dns_egress` | **DNS exfiltration** (critical) | Cloud DNS response policy `*.` → NXDOMAIN; port-53 firewall to Cloud DNS only; authoritative-log red-team gate (M0) |
| `write_outside_scratch` | Sandbox / filesystem containment | Read-only rootfs; outputs only via signed URLs; scratch is the only writable path |
| `reuse_foreign_url` | Artifact swap / replay; IDOR | Egress limited to the restricted VIP; signed URLs are object-scoped, short-TTL, and bound to tenant + generation at mint time (§8.6) |
| `read_proc1_environ` | Credential theft (no secrets in env/argv) | Supervisor/worker uid split; supervisor non-dumpable; no secrets in the environment |
| `read_supervisor_mem` | Sandbox or kernel escape; runner hijack | uid split + `PR_SET_NO_NEW_PRIVS`; ptrace denied; supervisor non-dumpable |
| `pth_injection` | Runner hijack by tenant packages | Overlay on the worker path only; build-inspect rejects `.pth`/`sitecustomize`/name collisions; read-only site-packages |
| `fork_bomb` | Compute abuse / DoS | Process-count limits (cgroup pids); per-run resource class |
| `memory_bomb` | Compute abuse; OOM | Memory cgroup limit; exit 137 classified `RESOURCE_EXHAUSTED` |
| `disk_bomb` | In-memory filesystem exhaustion (§8.5) | Scratch budget per resource class; in-memory FS counts against RAM |
| `log_flood` | Forged/abusive progress & log volume | Supervisor `LogGate` cap (4 KB/line, 20 MB/run) → `SANDBOX_VIOLATION(log_flood)`; sink excludes excess user stdout |
| `forged_stdout_event` | Forged progress or results | Only the supervisor writes stdout; per-line HMAC with the per-run key; `(run, attempt, seq)` dedupe; events service drops bad HMAC (counted as a sandbox violation) |
| `tampered_manifest` | Malicious artifacts; artifact swap | FINALIZING verifies every object against the signed manifest + GCS CRC32C/MD5/generation; unexpected objects → `INTEGRITY_FAILURE` + alert |
| `redos_schema` | ReDoS and schema attacks from plugins | Sanitizer: RE2 / expression allow-list, local `$ref` only, size caps, `x-ui-*` allow-list |
| `plot_xss_exfil` | Plot XSS and exfiltration | Server sanitizer strips external URLs/scripts; origin-isolated iframe; validated postMessage; Plotly strict bundle |
| `reuse_foreign_manifest` | Artifact swap or replay | Manifest bound to its run's HMAC key and, after copy, to a platform signature checked when URLs are minted |

## Why local-process is allowed to fail these

The platform never treats `local-process` as a security boundary. A startup check refuses it
in any multi-tenant profile (§8.4), and the supervisor emits a `DEV_MODE_ISOLATION` warning
when it cannot drop to the worker uid. The suite therefore *expects* most sandbox probes to be
ALLOWED under `PROCESS` isolation and records that explicitly, rather than pretending a dev
laptop is safe. The security guarantee is the GVISOR/MICROVM column, which must be all BLOCKED.

## Controls verified elsewhere

Some of these controls are also exercised by the main test suites, independently of this
corpus:

- forged events / bad HMAC → `packages/server/tests/test_events.py` and the `bad_hmac`
  end-to-end failure mode (`test_e2e_local.py`);
- tampered manifest / unlisted object → the `tamper` end-to-end failure mode and
  `packages/server/tests/test_finalizer.py` (`INTEGRITY_FAILURE` + security alert);
- plot XSS/exfil sanitization → `packages/plots-core` sanitizer tests and the end-to-end
  "unsafe plot dropped" assertion;
- log-flood cap → the runner `LogGate` tests.

This suite's role is to run them **together, as hostile tenant code**, against each executor's
real isolation, and to produce one pass/fail signal and metric for operations.
