# `mapi_runner`

This package is the run harness in the sandbox (see [Runner](../runner.md)). The supervisor
modules import only `mapi_spec` and `httpx`. The worker modules run the SDK harness and the
plugin code.

## Job documents and input/output names

::: mapi_runner.jobs
    options:
      members: [IngestJob, MffOptions, DescribeJob, ScoreJob, EvaluateJob, DeriveJob, RenderJob, parse_job]
      heading_level: 3

## Supervisor <-> worker channel

::: mapi_runner.channel
    options:
      members: [StartMessage, StateMessage, ProgressMessage, DiagnosticMessage, WarningMessage, MetricMessage, CheckpointMessage, ResultMessage, ErrorMessage, FrameReader, ProtocolError]
      heading_level: 3

## Supervisor

::: mapi_runner.supervisor
    options:
      members: [Supervisor, SupervisorConfig, EXIT_FOR_CLASS]
      heading_level: 3

## Worker isolation

::: mapi_runner.sandbox
    options:
      members: [IsolationPlan, plan_isolation, harden_supervisor]
      heading_level: 3
