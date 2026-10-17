---
description: The sandbox-escape probe suite, its expected-outcome matrix per isolation level, and how to run it locally, against gVisor, and in the cloud.
---

# Security suite <span class="mapi-badge">v0.3</span>

The security suite is a set of hostile probes. For each executor, it reports which attacks
the executor **BLOCKED** and which it **ALLOWED**. It compares the result with the guarantees
of the isolation level of that executor. It runs each night in the cloud. It must stay green
for two weeks before public code upload (cut line B) can open.

The suite is in `security/` at the root of the repository, and it is **never installed**.
`security/` is not a uv workspace member, so `uv sync --all-packages` does not include it.
Its malicious plugin is also not registered as a `mapi.models` entry point. Thus the
platform can never find it. The suite runs on the installed environment with `security/src`
on `PYTHONPATH`. The Make targets do this.

## Run it

```bash
make security-list                              # the probe corpus
make security-suite                             # EXECUTOR=local-process (always runnable)
make security-suite EXECUTOR=docker-gvisor      # sandbox probes inside a gVisor container
```

If an outcome is not acceptable for the isolation level of the executor,
`make security-suite` stops with a status that is not zero. It also writes
`.local/security-<executor>.prom`. This file exports `mapi_security_probe_unexpected{executor}`
for a textfile collector or a Pushgateway. The `MapiSecurityProbeFailure` alert in `ops/alerts/`
monitors that series. The nightly workflow pushes the file to the Pushgateway that the
`MAPI_PUSHGATEWAY_URL` repository secret names. Without that secret, the alert has no data.
Then the failed nightly job is the signal.

## The probes

**Isolation probes** test the controls of the operating system and the sandbox. Each probe
tries a hostile action, safely and with limits. It reports ALLOWED or BLOCKED:

| Probe | Attempts |
|---|---|
| `metadata_token` | Get an instance token from the metadata server |
| `http_egress`, `dns_egress` | Open an outbound connection. Resolve a unique subdomain (DNS exfiltration) |
| `write_outside_scratch` | Write outside the output area of the run |
| `reuse_foreign_url` | Connect to an object-store endpoint. This is the first step to use the signed URL of a different run |
| `read_proc1_environ`, `read_supervisor_mem` | Read the environment or memory of the supervisor |
| `pth_injection` | Put a `.pth` or `sitecustomize` file into site-packages |
| `fork_bomb`, `memory_bomb`, `disk_bomb` | Go above the process, memory or scratch limits (all capped) |
| `log_flood` | Write more than the log cap of the run |

**Platform probes** call the real defences of the control plane in `mapi_spec` and
`mapi_plots`. All executors must stop them (BLOCKED):

| Probe | Attempts |
|---|---|
| `forged_stdout_event` | A run event with the wrong HMAC |
| `tampered_manifest` | An output object that the signed manifest did not list |
| `redos_schema` | A catastrophic-backtracking regex in a plot spec |
| `plot_xss_exfil` | A plot with an external image URL or a `<script>` caption |
| `reuse_foreign_manifest` | The signed manifest of one run, sent under the key of a different run |

## What each isolation level must block

| Isolation | Executors | Isolation probes | Platform probes |
|---|---|---|---|
| `PROCESS` | `local-process`, `docker` (runc) | The suite accepts the two outcomes, because these are not security boundaries | BLOCKED |
| `GVISOR` | `docker-gvisor` (shared tiers only on a network declared with `MAPI_DOCKER_NETWORK_ISOLATED`) | BLOCKED | BLOCKED |
| `MICROVM` | `cloud` (the `cloudrun-jobs` executor) | BLOCKED | BLOCKED |

SKIPPED is always acceptable, for example when there is no procfs or no Docker daemon. ERROR
is never acceptable. An executor needs two items before it can take `verified` and `public`
tenants:

- A passing run of this matrix, recorded for the current caps of the executor.
- A passing run of the 12-case executor conformance suite
  (`packages/server/tests/test_executor_conformance.py`).

Record the two with `PUT /v1/admin/executors/{name}/evidence`. Refer to
[the shared-tier gate](cloud-run.md#the-shared-tier-gate). For that gate, the network probes
(`http_egress`, `dns_egress`, `metadata_token`) must run, and the executor must stop them.

## Where the probes run

- **local-process:** the suite process is the execution environment. Thus the probes run in
  the process. On a developer machine, most isolation probes are ALLOWED. This is expected
  and documented.
- **docker, docker-gvisor:** the isolation probes run in a locked-down container
  (`--network none`, read-only root, non-root user, no capabilities, `no-new-privileges`, and
  `--runtime runsc` for gVisor). Without a Docker daemon, they report SKIPPED. The platform
  probes still run.
- **cloud:** the probes must run *inside* a Cloud Run Jobs execution of a base image. The
  suite compares them with the `MICROVM` row. Thus `--executor cloud` does not run (exit
  status 2) where `CLOUD_RUN_JOB` and `CLOUD_RUN_EXECUTION` are not set. A report from a
  laptop or a CI runner would describe the wrong sandbox. The base images do not contain the
  suite yet, and Terraform defines no probe Job. Thus we must still connect the nightly cloud
  run. Refer to [M2 verification](m2-verification.md#what-remains-for-real-gcp). The
  authoritative DNS-exfiltration check reads the query logs of a zone that we control
  ([security hardening](security-hardening.md)).

`security/THREAT-MODEL.md` connects each probe to its threat, and to the control that must
stop it.
