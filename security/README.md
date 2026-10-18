# security/ — the sandbox-escape probe suite

A corpus of deliberately hostile probes and a runner that reports, **per executor isolation
level**, which attacks were BLOCKED and which were ALLOWED, checked against an
expected-outcome matrix (plan §9 threat model, §13 security suite).

> **This directory is not installed by default.** `security/` is outside the uv workspace
> globs (`packages/*`, `plugins/*`, `conformance`), so `uv sync --all-packages` never installs
> it, and the malicious plugin in `security/plugin/` is **not** wired to the `mapi.models`
> entry point, so the platform can never discover it. The suite runs against the already
> installed workspace venv with `security/src` on `PYTHONPATH` — that is exactly what
> `make security-suite` does.

## Run it

```bash
make security-suite                      # EXECUTOR=local-process (dev); always runnable
make security-suite EXECUTOR=docker-gvisor   # run the sandbox probes inside a gVisor container
make security-list                       # list the probe corpus
```

Or directly:

```bash
PYTHONPATH=security/src:security/plugin/src \
  uv run python -m mapi_security run --executor docker-gvisor --image mapi/worker:0.8.0 \
                                     --json report.json --metrics metrics.prom
```

`run` exits non-zero when any probe outcome is unacceptable for the executor's isolation
level — the signal CI and the ops alert `MapiSecurityProbeFailure` key on. The optional
`--metrics` textfile exports `mapi_security_probe_unexpected{executor=...}` for a node_exporter
textfile collector or a Pushgateway.

## The corpus (`mapi_security.probes`)

Two categories:

**Isolation probes** — OS/sandbox controls. Each attempts a hostile action and reports
ALLOWED (the control is absent) or BLOCKED (a control refused it). All are safe and bounded.

| Probe | Attempts |
|---|---|
| `metadata_token` | fetch an instance token from the cloud metadata server (169.254.169.254) |
| `http_egress` | open an outbound TCP connection to a public address |
| `dns_egress` | resolve a unique subdomain (DNS tunnelling / exfiltration) |
| `write_outside_scratch` | write a file outside the run's output area (home dir) |
| `reuse_foreign_url` | reach an object-store endpoint (precursor to reusing a foreign URL) |
| `read_proc1_environ` | read `/proc/1/environ` (the supervisor/init environment) |
| `read_supervisor_mem` | read the parent process memory via `/proc/<ppid>/mem` |
| `pth_injection` | drop a `.pth`/`sitecustomize` into site-packages for code execution |
| `fork_bomb` | spawn a bounded burst of processes (process-count limit) |
| `memory_bomb` | allocate a capped buffer (memory cgroup limit) |
| `disk_bomb` | write a capped file to scratch (scratch budget) |
| `log_flood` | write to the log channel past the per-run cap |

**Platform probes** — control-plane defences that hold regardless of the executor, so they
must be BLOCKED everywhere (including local-process). They call the real `mapi_spec` /
`mapi_plots` code:

| Probe | Attempts |
|---|---|
| `forged_stdout_event` | forge a run event with the wrong HMAC (`verify_event` rejects it) |
| `tampered_manifest` | ship an output object the signed manifest never listed (`verify_output_objects`) |
| `redos_schema` | a catastrophic-backtracking regex in a plot spec (sanitizer allow-list / RE2) |
| `plot_xss_exfil` | a plot spec with an external image URL / `<script>` caption (`sanitize_plot`) |
| `reuse_foreign_manifest` | present one run's signed manifest under another run's key |

## The expected-outcome matrix (`mapi_security.matrix`)

The plan is explicit that **local-process is not a security boundary** (§8.4: a startup check
refuses it in any multi-tenant profile). So the matrix encodes:

| Isolation | Isolation probes | Platform probes |
|---|---|---|
| **PROCESS** (local-process, dev) | ALLOWED or BLOCKED accepted — *documented*: on a dev host filesystem/fork/mem/disk probes are ALLOWED (no boundary), network probes depend on the environment | must be BLOCKED |
| **GVISOR** (docker-gvisor) | **must be BLOCKED** | must be BLOCKED |
| **MICROVM** (Cloud Run) | **must be BLOCKED** | must be BLOCKED |

`SKIPPED` is always acceptable (a probe that could not run here, e.g. no procfs or no Docker
daemon). `ERROR` never is. On an enforcing executor, any ALLOWED sandbox probe fails the run —
the gate an executor must pass before it may run public tenants (plan §8.4, §13).

## The malicious plugin (`security/plugin/`)

`mapi_security_plugin` is a **deliberately malicious** model + step plugin whose `fit`/`transform`
run the isolation probes from inside a real execution. It is the vehicle to push hostile tenant
code through the real runner (supervisor + worker) on an enforcing executor and confirm every
attack is blocked. It is never installed and never registered via entry points; the suite loads
it explicitly.

## What runs where

- **local-process / dev:** the suite process *is* the dev execution environment, so probes run
  in-process. Expect the documented PROCESS outcomes.
- **docker / docker-gvisor:** isolation probes run inside a container
  (`--network none --read-only --user 10001 --cap-drop ALL --security-opt no-new-privileges`,
  plus `--runtime runsc` for gVisor). With no Docker daemon they report SKIPPED; platform
  probes still run in-process. (This machine has no daemon — see `BUILD-PROGRAM.md`.)
- **cloud:** the probes ship in the base image and run as the real Cloud Run Jobs execution;
  the authoritative DNS-exfil check uses the red-team zone's logs (plan §9, M0 gate).

See [THREAT-MODEL.md](THREAT-MODEL.md) for how each probe maps to a threat-model row and the
control that is expected to stop it.
