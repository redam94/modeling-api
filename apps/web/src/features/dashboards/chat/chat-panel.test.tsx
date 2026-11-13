import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { env } from "@/env";
import { ShareApiError } from "@/features/dashboards/client";
import { ShareApp } from "@/features/share/share-app";
import { LINK_ENDED_TEXT } from "@/features/share/link-ended";
import { MOCK_CHAT_HOSTILE_REPLY, MOCK_CHAT_ON_KEY } from "@/mocks/chat";
import { MOCK_SHARE_TOKENS } from "@/mocks/dashboards";
import { createHandlers } from "@/mocks/handlers";

import { ChatLauncher } from "./chat-launcher";
import { errorText, NOTICE_TEXT, toolLabel } from "./chat-panel";
import { chatErrorOf } from "./conversation";
import { setViewedReadout } from "./viewed-readout";
import type { ChatAvailability, ChatRequest, ChatResponse, ChatTransport } from "./types";

const SIG = "s".repeat(43);
const CNV = `cnv_${"0".repeat(26)}`;

const AVAILABLE: ChatAvailability = { enabled_for_me: true, reason: null, keeps_transcripts: false, transcript_days: 30, model: "fake-chat", suggestions: ["What does the latest readout show?", "What changed since the last readout?"] };

function reply(text: string, over: Partial<ChatResponse> = {}): ChatResponse {
  return {
    conversation_id: CNV,
    reply: { role: "assistant", text, sig: SIG },
    tool_calls: [{ name: "get_latest_readout", args: {}, ok: true, ms: 12, truncated: false }],
    usage: { input_tokens: 10, output_tokens: 5, thought_tokens: 0, total_tokens: 15, model: "fake-chat" },
    notices: [],
    transcript_kept: false,
    ...over,
  };
}

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function setup(availability: ChatAvailability | Error = AVAILABLE, send: ChatTransport["send"] = vi.fn(async () => reply("Sales were 1,200."))) {
  const transport: ChatTransport = {
    availability: vi.fn(async () => {
      if (availability instanceof Error) throw availability;
      return availability;
    }),
    send: vi.fn(send),
  };
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const user = userEvent.setup();
  const view = render(
    <QueryClientProvider client={qc}>
      <main>
        <p>The readout</p>
        <ChatLauncher transport={transport} cacheKey={["test", "dash"]} />
      </main>
    </QueryClientProvider>,
  );
  return { transport, user, ...view };
}

async function openPanel(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByTestId("chat-launcher", {}, { timeout: 4000 }));
  return screen.findByTestId("chat-panel");
}

const sent = (t: ChatTransport, call = 0) => vi.mocked(t.send).mock.calls[call]?.[0] as ChatRequest;

describe("ChatLauncher", () => {
  it("shows nothing when chat is not available to this viewer", async () => {
    const { transport, container } = setup({ ...AVAILABLE, enabled_for_me: false, reason: "off" });
    await waitFor(() => expect(transport.availability).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByTestId("chat-launcher")).toBeNull();
    expect(container.querySelector("main")?.children).toHaveLength(1);
  });

  it("shows nothing when the availability request fails", async () => {
    const { transport } = setup(new ShareApiError(404, null));
    await waitFor(() => expect(transport.availability).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByTestId("chat-launcher")).toBeNull();
  });

  it("opens the lazy panel with the suggestions, the input focused and a polite live log", async () => {
    const { user } = setup();
    const launcher = await screen.findByTestId("chat-launcher");
    expect(launcher).toHaveAccessibleName("Ask about this dashboard");
    expect(launcher).toHaveAttribute("aria-expanded", "false");
    const panel = await openPanel(user);
    expect(launcher).toHaveAttribute("aria-expanded", "true");
    expect(within(panel).getByRole("heading", { name: "Ask about this dashboard" })).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "What does the latest readout show?" })).toBeInTheDocument();
    expect(within(panel).getByRole("log")).toHaveAttribute("aria-live", "polite");
    await waitFor(() => expect(screen.getByTestId("chat-input")).toHaveFocus());
    expect(screen.queryByTestId("chat-transcript-notice")).toBeNull();
    // A side sheet on a wide screen, a bottom sheet at 640 px and below (390 px phones).
    expect(panel.className).toContain("right-0");
    expect(panel.className).toContain("max-sm:bottom-0");
    expect(panel.className).toContain("max-sm:w-full");
    // Not modal: the dashboard stays readable beside it.
    expect(screen.getByText("The readout")).toBeVisible();
  });

  it("sends the readout the history shows as readout_seq (web+docs-1)", async () => {
    const { user, transport } = setup();
    setViewedReadout(["test", "dash"], 11);
    setViewedReadout(["other", "dash"], 3); // another viewer's client: never this one's
    try {
      await openPanel(user);
      await user.click(screen.getByRole("button", { name: "What does the latest readout show?" }));
      expect(sent(transport).readout_seq).toBe(11);
    } finally {
      setViewedReadout(["test", "dash"], null);
      setViewedReadout(["other", "dash"], null);
    }
  });

  it("says the dashboard keeps chats before the first question", async () => {
    const { user } = setup({ ...AVAILABLE, keeps_transcripts: true, transcript_days: 30 });
    await openPanel(user);
    expect(screen.getByTestId("chat-transcript-notice")).toHaveTextContent("This dashboard keeps chats for 30 days. Its analysts can read them.");
  });
});

describe("ChatPanel", () => {
  it("asks a suggestion: pending line, then the answer with its tool chips and notices", async () => {
    const d = deferred<ChatResponse>();
    const { user, transport } = setup(AVAILABLE, vi.fn(() => d.promise));
    await openPanel(user);
    await user.click(screen.getByRole("button", { name: "What changed since the last readout?" }));
    expect(sent(transport)).toEqual({ messages: [{ role: "user", text: "What changed since the last readout?" }] });
    expect(screen.getByTestId("chat-turn-user")).toHaveTextContent("What changed since the last readout?");
    expect(screen.getByRole("status")).toHaveTextContent("Looking at the dashboard…");
    expect(screen.getByTestId("chat-send")).toBeDisabled();

    d.resolve(
      reply("Sales were **1,200** this week.\n\n- TV: 400\n- Search: 800", {
        tool_calls: [
          { name: "get_latest_readout", args: {}, ok: true, ms: 10, truncated: false },
          { name: "compare_readouts", args: { a_seq: 11, b_seq: 12 }, ok: true, ms: 10, truncated: true },
          { name: "get_channel", args: { channel: "TV" }, ok: false, code: "SCHEMA_INVALID", ms: 3, truncated: false },
        ],
        notices: ["unverified_numbers", "tool_limit"],
      }),
    );
    const answer = await screen.findByTestId("chat-turn-assistant");
    expect(answer.querySelector("strong")).toHaveTextContent("1,200");
    expect(answer.querySelectorAll("ul.list-disc > li")).toHaveLength(2);
    expect(screen.getAllByTestId("chat-tool-chip").map((c) => c.textContent)).toEqual(["Read the latest readout", "Compared readouts 11 and 12 (shortened)", "Looked at TV (did not work)"]);
    expect(screen.getByTestId("chat-notice-unverified_numbers")).toHaveTextContent(NOTICE_TEXT.unverified_numbers);
    expect(screen.getByTestId("chat-notice-tool_limit")).toHaveTextContent("stopped after 6 lookups");
    expect(screen.queryByRole("status")).toBeNull();
    // The suggestions are for the first question only.
    expect(screen.queryByRole("list", { name: "Suggested questions" })).toBeNull();
  });

  it("sends the conversation so far with each question (signatures unchanged); Enter sends, Shift+Enter breaks the line", async () => {
    const { user, transport } = setup();
    await openPanel(user);
    const input = screen.getByTestId("chat-input");
    await user.type(input, "How are sales?{Enter}");
    await screen.findByTestId("chat-turn-assistant");
    expect(input).toHaveValue("");

    await user.type(input, "And by{Shift>}{Enter}{/Shift}channel?");
    expect(input).toHaveValue("And by\nchannel?");
    expect(transport.send).toHaveBeenCalledTimes(1);
    await user.click(screen.getByTestId("chat-send"));
    await waitFor(() => expect(screen.getAllByTestId("chat-turn-assistant")).toHaveLength(2));
    expect(sent(transport, 1)).toEqual({
      conversation_id: CNV,
      messages: [
        { role: "user", text: "How are sales?" },
        { role: "assistant", text: "Sales were 1,200.", sig: SIG },
        { role: "user", text: "And by\nchannel?" },
      ],
    });
    expect(screen.getAllByTestId("chat-turn-user")).toHaveLength(2);
  });

  it("closes on Escape and gives focus back to the launcher; the conversation stays", async () => {
    const { user } = setup();
    await openPanel(user);
    await user.type(screen.getByTestId("chat-input"), "Hi{Enter}");
    await screen.findByTestId("chat-turn-assistant");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("chat-panel")).toBeNull());
    expect(screen.getByTestId("chat-launcher")).toHaveFocus();
    await user.click(screen.getByTestId("chat-launcher"));
    expect(await screen.findByTestId("chat-turn-assistant")).toHaveTextContent("Sales were 1,200.");
  });

  it("the close button closes the panel too", async () => {
    const { user } = setup();
    await openPanel(user);
    await user.click(screen.getByRole("button", { name: "Close the chat" }));
    await waitFor(() => expect(screen.queryByTestId("chat-panel")).toBeNull());
    expect(screen.getByTestId("chat-launcher")).toHaveFocus();
  });

  it("says when the daily budget is spent, with the time it resets", async () => {
    const err = new ShareApiError(429, { type: "about:blank", title: "Chat budget exhausted", status: 429, code: "CHAT_BUDGET_EXHAUSTED" } as never, undefined, 3600);
    const { user } = setup(AVAILABLE, vi.fn(async () => Promise.reject(err)));
    await openPanel(user);
    await user.type(screen.getByTestId("chat-input"), "How are sales?{Enter}");
    const box = await screen.findByTestId("chat-error-budget");
    expect(box).toHaveTextContent(/This dashboard has used its chat allowance for today\. You can ask again after \d{1,2}:\d{2}( [AP]M)?\./);
    expect(within(box).queryByRole("button", { name: "Try again" })).toBeNull();
    // The question stays on screen.
    expect(screen.getByTestId("chat-turn-user")).toHaveTextContent("How are sales?");
  });

  it("offers Try again when the assistant did not answer, and sends the same question", async () => {
    const send = vi.fn<ChatTransport["send"]>().mockRejectedValueOnce(new ShareApiError(503, null)).mockResolvedValueOnce(reply("Now it works."));
    const { user, transport } = setup(AVAILABLE, send);
    await openPanel(user);
    await user.type(screen.getByTestId("chat-input"), "How are sales?{Enter}");
    const box = await screen.findByTestId("chat-error-unavailable");
    expect(box).toHaveTextContent("The assistant did not answer. Try again.");
    await user.click(within(box).getByRole("button", { name: "Try again" }));
    expect(await screen.findByTestId("chat-turn-assistant")).toHaveTextContent("Now it works.");
    expect(sent(transport, 1)).toEqual(sent(transport, 0));
    expect(screen.getAllByTestId("chat-turn-user")).toHaveLength(1);
  });

  it("starts a new chat when the server refuses the history", async () => {
    const err = new ShareApiError(400, { type: "about:blank", title: "Invalid chat history", status: 400, code: "CHAT_HISTORY_INVALID" } as never);
    const { user } = setup(AVAILABLE, vi.fn(async () => Promise.reject(err)));
    await openPanel(user);
    await user.type(screen.getByTestId("chat-input"), "Hi{Enter}");
    const box = await screen.findByTestId("chat-error-history");
    await user.click(within(box).getByRole("button", { name: "New chat" }));
    expect(screen.queryByTestId("chat-turn-user")).toBeNull();
    expect(screen.getByRole("list", { name: "Suggested questions" })).toBeInTheDocument();
  });

  it("renders a hostile answer as text: no image, link or script", async () => {
    const { user } = setup(AVAILABLE, vi.fn(async () => reply(MOCK_CHAT_HOSTILE_REPLY)));
    await openPanel(user);
    await user.type(screen.getByTestId("chat-input"), "Show me{Enter}");
    const answer = await screen.findByTestId("chat-turn-assistant");
    expect(answer.querySelector("img, a, script, iframe")).toBeNull();
    expect(answer).toHaveTextContent("<script>alert('chat')</script>");
    const attrs = [answer, ...answer.querySelectorAll("*")].flatMap((el) => [...el.attributes].map((x) => x.value)).join(" ");
    expect(attrs).not.toMatch(/evil|javascript:|onerror/);
    expect(answer.textContent).not.toMatch(/evil\.example\/\?d=1|javascript:/);
  });
});

describe("copy", () => {
  it("names each tool call in plain words", () => {
    expect(toolLabel({ name: "get_readout", args: { seq: 12 } })).toBe("Read readout 12");
    expect(toolLabel({ name: "get_readout", args: { seq: "12; drop" } })).toBe("Read a readout");
    expect(toolLabel({ name: "get_channel", args: { channel: "x".repeat(80) } })).toBe(`Looked at ${"x".repeat(40)}`);
    expect(toolLabel({ name: "something_else", args: {} })).toBe("Used a dashboard tool");
  });

  it("explains each error kind", () => {
    const e = { status: null, code: null, retryAfter: null };
    expect(errorText({ ...e, kind: "budget" })).toBe("This dashboard has used its chat allowance for today. You can ask again tomorrow.");
    expect(errorText({ ...e, kind: "rate" })).toBe("You asked many questions in a short time. Wait a minute, then try again.");
    expect(errorText({ ...e, kind: "link_ended" })).toBe("This link has ended or was removed.");
  });

  it("shows a reset time that does not move as time passes (web+docs-8)", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date(2026, 9, 23, 23, 0, 0));
      const error = chatErrorOf(Object.assign(new Error("x"), { status: 429, problem: { code: "CHAT_BUDGET_EXHAUSTED" }, retryAfter: 3600 }));
      const first = errorText(error);
      vi.setSystemTime(new Date(2026, 9, 23, 23, 7, 0));
      expect(errorText(error)).toBe(first);
      const midnight = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" }).format(new Date(2026, 9, 24, 0, 0, 0));
      expect(first).toContain(`after ${midnight}`);
    } finally {
      vi.useRealTimers();
    }
  });
});

// ------------------------------------------------------------------------------ share link

describe("share link chat (MSW)", () => {
  localStorage.setItem(MOCK_CHAT_ON_KEY, "1");
  const server = setupServer(...createHandlers());
  localStorage.removeItem(MOCK_CHAT_ON_KEY);
  const seen: Request[] = [];
  beforeAll(() => {
    server.listen({ onUnhandledRequest: "bypass" });
    server.events.on("request:start", ({ request }) => {
      seen.push(request.clone());
    });
  });
  beforeEach(() => {
    seen.length = 0;
    localStorage.setItem("mapi.mock.readoutMs", "40");
  });
  afterEach(() => {
    server.resetHandlers();
    window.history.replaceState(null, "", "/");
  });
  afterAll(() => server.close());

  const chatRequests = () => seen.filter((r) => new URL(r.url).pathname === "/v1/shared/chat");

  it("asks over the share session only (no cookie, CSRF or project header)", async () => {
    const user = userEvent.setup();
    window.history.replaceState(null, "", `/s#${MOCK_SHARE_TOKENS.forecastV2}`);
    render(<ShareApp />);
    await openPanel(user);
    await user.type(screen.getByTestId("chat-input"), "What does the latest readout show?{Enter}");
    expect(await screen.findByTestId("chat-turn-assistant", {}, { timeout: 4000 })).toBeInTheDocument();
    expect(chatRequests().map((r) => r.method)).toEqual(["GET", "POST"]);
    for (const r of chatRequests()) {
      expect(r.headers.get("X-Mapi-Share")).toMatch(/^mps_/);
      expect(r.headers.get("Cookie")).toBeNull();
      expect(r.headers.get("X-CSRF-Token")).toBeNull();
      expect(r.headers.get("X-Mapi-Project")).toBeNull();
    }
  });

  it("a link revoked mid-conversation shows the link-ended page", async () => {
    const user = userEvent.setup();
    window.history.replaceState(null, "", `/s#${MOCK_SHARE_TOKENS.forecastV2}`);
    render(<ShareApp />);
    await openPanel(user);
    server.use(http.post(`${env.apiBase}/v1/shared/chat`, () => HttpResponse.json({ type: "about:blank", title: "Link expired", status: 410, code: "LINK_EXPIRED" }, { status: 410 })));
    await user.type(screen.getByTestId("chat-input"), "Hi{Enter}");
    expect(await screen.findByText(LINK_ENDED_TEXT)).toBeInTheDocument();
    expect(screen.queryByTestId("chat-panel")).toBeNull();
  });

  it("shows no launcher on a layout 1 link", async () => {
    window.history.replaceState(null, "", `/s#${MOCK_SHARE_TOKENS.readOnly}`);
    render(<ShareApp />);
    expect(await screen.findByRole("heading", { level: 1, name: "Weekly media readout" }, { timeout: 4000 })).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByTestId("chat-launcher")).toBeNull();
    expect(chatRequests()).toEqual([]);
  });
});
