"""Dashboard chat (v0.20): "Ask about your dashboard".

The chat runs in the control plane. A turn lets a language model call the read-only tools of
:mod:`mapi_spec.dashboard_tools` for the caller's own dashboard context, then answers in text.

* :mod:`.model` — the provider-neutral model interface (:class:`~.model.ChatModel`), the
  turn and usage types, and :func:`~.model.build_chat_model` (the setting
  ``MAPI_CHAT_PROVIDER`` picks ``off``, ``gemini`` or ``fake``);
* :mod:`.gemini` — the Gemini ``generateContent`` adapter (REST through ``httpx``; the API
  key goes in a header only, never in a URL, a log, a span or an error);
* :mod:`.fake` — a deterministic model for tests, development and the live check;
* the loop, prompt, post-filter, signed history, budgets and transcripts (WP4 modules).

Nothing here imports the loop: a module of this package imports only what it needs.
"""
