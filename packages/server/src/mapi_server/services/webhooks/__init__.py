"""Outgoing webhooks (plan §8.2 Integrations, §9 "Webhook SSRF"; M3).

* :mod:`~mapi_server.services.webhooks.outbox`: events written in the transaction that
  causes them;
* :mod:`~mapi_server.services.webhooks.delivery`: the sender, retries, auto-disable and the
  relay loop;
* :mod:`~mapi_server.services.webhooks.ssrf`: resolve, check and pin the receiver's address;
* :mod:`~mapi_server.services.webhooks.crypto`: signing secrets sealed at rest;
* :mod:`~mapi_server.services.webhooks.manage`: rows to DTOs, creation and secret rotation.

This package imports nothing eagerly: the run state machine imports the outbox, and the
delivery side imports the service container.
"""
