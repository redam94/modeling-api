---
description: v0.20 lets readers ask an AI assistant about the numbers on a dashboard, and lets chat tools and agents read a dashboard over MCP with a read token or a share link.
---

# What's new in 0.20 <span class="mapi-badge">v0.20</span>

**Ask about your dashboard.** Readers can now ask questions about a dashboard in plain words.
An assistant reads the dashboard with the same read-only tools as the app and answers with
its numbers. Your own chat tools and agents can use the same tools over MCP.

<div class="grid cards" markdown>

-   :material-chat-question-outline:{ .lg .middle } **A chat on each dashboard**

    ---

    Select **Ask about this dashboard** and ask "What changed since the last readout?". The
    answer shows the tools that the assistant used. It works for team members, client users
    and (when you allow it) share links.

    [:octicons-arrow-right-24: Ask about your dashboard](guides/ask-about-your-dashboard.md)

-   :material-toggle-switch-outline:{ .lg .middle } **Off until you turn it on**

    ---

    The chat is off on each dashboard. An editor turns it on in **Setup › Chat**, chooses
    the readers, sets daily token limits and can keep chats for 30 days. No new revision is
    necessary.

    [:octicons-arrow-right-24: Turn on the chat](guides/ask-about-your-dashboard.md#turn-on-the-chat)

-   :material-shield-check-outline:{ .lg .middle } **It sees what you see**

    ---

    The assistant gets the dashboard from your sign-in or link, never from your question. It
    can only read. The server checks your access before each step, removes links and images
    from answers and marks numbers that it cannot find in the tool results.

    [:octicons-arrow-right-24: How the platform protects the answers](guides/ask-about-your-dashboard.md#how-the-platform-protects-the-answers)

-   :material-connection:{ .lg .middle } **Connect a chat tool with MCP**

    ---

    Make a read token on the dashboard (clients can too) and copy the configuration for a
    desktop app (the `mapi-mcp` bridge) or for HTTP (`POST /v1/mcp`). A share link and its
    passcode work too.

    [:octicons-arrow-right-24: Connect a chat tool with MCP](guides/connect-a-chat-tool-with-mcp.md)

-   :material-earth:{ .lg .middle } **You know where the data goes**

    ---

    The chat uses Gemini (`gemini-3.1-flash-lite`) from the API server, with a paid-tier key
    and `store: false`. Only workspaces with an allowed data residency (default `us`) can use
    it. MCP sends nothing to a model.

    [:octicons-arrow-right-24: What goes to Google](guides/ask-about-your-dashboard.md#what-goes-to-google)

-   :material-server-security:{ .lg .middle } **Operators stay in control**

    ---

    The chat is off on the server until you set `MAPI_CHAT_PROVIDER`. A kill switch stops
    all chats at once. Each question writes a usage row with token counts and an estimated
    cost.

    [:octicons-arrow-right-24: For operators](guides/ask-about-your-dashboard.md#for-operators)

</div>

## For developers

- New routes: `GET`/`POST /v1/dashboards/{dashboard_id}/chat`, the chat settings and kept
  conversations under `/chat/settings` and `/chat/transcripts`, `GET`/`POST
  /v1/shared/chat`, the read tokens under `/v1/dashboards/{dashboard_id}/read-tokens` and
  `POST /v1/mcp`.
- A new token scope `dashboard:<dashboard_id>`. A token with it works only on `POST
  /v1/mcp`. Only a web sign-in can make one.
- A new package `mapi-mcp` (the stdio bridge) and the pure MCP protocol core
  `mapi_spec.mcp`. The platform has no MCP SDK dependency.
- `mapi_spec.chat` (the chat DTOs) and `mapi_spec.tool_results` (`cap_tool_result`, which
  shortens a tool result to a byte budget).
- Error codes `CHAT_DISABLED`, `CHAT_UNAVAILABLE`, `CHAT_BUDGET_EXHAUSTED`,
  `CHAT_HISTORY_INVALID` and `READ_TOKEN_LIMIT`.
- `MAPI_CHAT_PROVIDER=fake` runs the chat on your computer with a fixed model and no
  network.

## Good to know

- The chat works on dashboards with the panels layout (layout 2) only.
- Answers come as one message. They do not stream.
- A dashboard that uses uploaded code has no chat for client users and share links, and
  refuses all read tokens.
- No real Gemini call was made to test this release. The adapter is tested with recorded
  answers. Do a check with your own key before you turn on the chat for clients.
