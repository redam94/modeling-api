# `mapi_client`

The Python client for the `/v1` HTTP API (see the [Python client guide](../../guides/python-client.md)
for tested walkthroughs and [CLI and Python client](../cli.md) for a summary of every
resource). Generated from the docstrings; `AsyncClient` has the same resources, awaited.

## Clients

::: mapi_client
    options:
      members: [Client, AsyncClient, ClientConfig, RetryPolicy, load_config]
      heading_level: 3

## Workflows (v0.5)

::: mapi_client._m35
    options:
      members: [Workflows]
      heading_level: 3

## Workflow run events (v0.5)

::: mapi_client.sse
    options:
      members: [WorkflowSnapshot, WorkflowEventFrame, WorkflowEnd, to_workflow_stream_item]
      heading_level: 3

## Governance and validations (v0.5)

::: mapi_client._m35
    options:
      members: [Governance, Validations]
      heading_level: 3

## About cards, explorers and optimisation (v0.5)

::: mapi_client._m35
    options:
      members: [CatalogAboutMixin, DatasetAboutMixin, ModelInsightMixin, Explorations]
      heading_level: 3

## Endpoints (v0.4)

::: mapi_client._m3
    options:
      members: [Endpoints, RemoteEndpoint]
      heading_level: 3
