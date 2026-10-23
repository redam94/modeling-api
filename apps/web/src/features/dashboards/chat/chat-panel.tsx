/**
 * The chat panel of "Ask about your dashboard" (v0.20, plan D21/D23): a side sheet on a wide
 * screen and a bottom sheet on a phone (≤ 640 px). This module is the lazy `chat-panel` chunk
 * that all three surfaces share; it talks only to the {@link ChatTransport} it is given, so it
 * imports neither the session client nor the app's queries (the share link budget checks it).
 *
 * The conversation is the pure reducer of `conversation.ts`. Answers render through
 * {@link SafeText} (React elements from a tiny markdown subset; never HTML). The tool chips and
 * notices under an answer are fixed app strings; the only links are the allow-listed
 * {@link DocsLink}s of `docs-links.ts`.
 *
 * The panel is not modal: the dashboard stays readable and scrollable beside it. Escape closes
 * it and focus goes back to the launcher. Enter sends, Shift+Enter starts a new line. Answers
 * are announced politely (`aria-live`).
 */
import { useMutation } from "@tanstack/react-query";
import { Loader2, RotateCcw, SendHorizontal, Wrench, X } from "lucide-react";
import { Dialog as DialogPrimitive } from "radix-ui";
import { useEffect, useReducer, useRef, useState, type RefObject } from "react";

import { InlineMessage } from "@/components/ods";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import { canRetry, chatReducer, initialChatState, isFull, requestFor, sendBlock, type AssistantEntry, type ChatError } from "./conversation";
import { docsHref, type DocsKey } from "./docs-links";
import { SafeText } from "./render-safe";
import { CHAT_MAX_TOOL_CALLS, CHAT_MAX_USER_CHARS, CHAT_SUGGESTIONS, type ChatAvailability, type ChatNoticeKind, type ChatRequest, type ChatResponse, type ChatTransport, type ToolCallSummary } from "./types";

export interface ChatPanelProps {
  transport: ChatTransport;
  availability: ChatAvailability;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Where focus goes when the panel closes (the launcher). */
  returnFocus?: RefObject<HTMLElement | null> | undefined;
  /** The readout the viewer is looking at, sent with each question as a hint. */
  readoutSeq?: number | null | undefined;
}

// ------------------------------------------------------------------------------ copy

/** The words of each notice under an answer. */
export const NOTICE_TEXT: Readonly<Record<ChatNoticeKind, string>> = {
  unverified_numbers: "Check these numbers on the dashboard. Some of them are not in what the assistant read.",
  tool_limit: `The assistant stopped after ${CHAT_MAX_TOOL_CALLS} lookups. Ask a narrower question for a fuller answer.`,
  truncated: "Some data was too long to read in full, so the answer may leave things out.",
  budget_low: "This dashboard has used most of its chat allowance for today.",
  transcript_kept: "This dashboard keeps this chat. Its analysts can read it.",
};

const NOTICE_TONE: Readonly<Record<ChatNoticeKind, "warning" | "info">> = {
  unverified_numbers: "warning",
  tool_limit: "info",
  truncated: "info",
  budget_low: "warning",
  transcript_kept: "info",
};

function num(v: unknown): string | null {
  return typeof v === "number" && Number.isInteger(v) && v >= 0 ? String(v) : null;
}

function word(v: unknown): string | null {
  return typeof v === "string" && v.trim() ? v.trim().slice(0, 40) : null;
}

/** What a tool call did, in the viewer's words (the tool names are the 14 dashboard tools). */
export function toolLabel(call: Pick<ToolCallSummary, "name" | "args">): string {
  const a = call.args ?? {};
  const seq = num(a.seq);
  switch (call.name) {
    case "get_dashboard":
      return "Read the dashboard";
    case "get_latest_readout":
      return "Read the latest readout";
    case "list_readouts":
      return "Listed the readouts";
    case "get_readout":
      return seq ? `Read readout ${seq}` : "Read a readout";
    case "compare_readouts": {
      const x = num(a.a_seq);
      const y = num(a.b_seq);
      return x && y ? `Compared readouts ${x} and ${y}` : "Compared two readouts";
    }
    case "get_channel": {
      const c = word(a.channel);
      return c ? `Looked at ${c}` : "Looked at a channel";
    }
    case "explain_metric":
      return "Looked up a word";
    case "evaluate_whatif":
      return "Tried a spend plan";
    case "list_panels":
      return "Listed the panels";
    case "get_panel":
      return "Read a panel";
    case "get_accuracy":
      return "Checked the accuracy";
    case "list_inputs":
      return "Listed the inputs";
    case "get_prediction":
      return seq ? `Read your case ${seq}` : "Read your case";
    case "get_batch_summary":
      return seq ? `Read your file results ${seq}` : "Read your file results";
    default:
      return "Used a dashboard tool";
  }
}

function clock(seconds: number): string {
  return new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" }).format(new Date(Date.now() + seconds * 1000));
}

/** What the panel says when a question failed. */
export function errorText(e: ChatError): string {
  switch (e.kind) {
    case "budget":
      return `This dashboard has used its chat allowance for today. You can ask again ${e.retryAfter !== null ? `after ${clock(e.retryAfter)}` : "tomorrow"}.`;
    case "rate":
      return "You asked many questions in a short time. Wait a minute, then try again.";
    case "disabled":
      return "Chat is off for this dashboard now.";
    case "history":
      return "This chat cannot continue. Start a new chat to ask again.";
    case "unavailable":
      return "The assistant did not answer. Try again.";
    case "link_ended":
      return "This link has ended or was removed.";
    case "signed_out":
      return "Your session ended. Sign in again to continue.";
    case "gone":
      return "This dashboard is not available to you now.";
    case "unpublished":
      return "This dashboard is not published now.";
    case "not_shareable":
      return "Chat is not available for you on this dashboard.";
    case "network":
      return "We could not reach the service. Check your connection and try again.";
    default:
      return "Something went wrong. Try again.";
  }
}

// ------------------------------------------------------------------------------ parts

/** A "Learn more" link from the fixed allow-list (`docs-links.ts`); nothing when the key has no page. */
function DocsLink({ k }: { k: DocsKey }) {
  const href = docsHref(k);
  if (!href) return null;
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" className="font-semibold text-primary underline-offset-4 hover:underline">
      Learn more
    </a>
  );
}

function ToolChips({ calls }: { calls: ToolCallSummary[] }) {
  if (!calls.length) return null;
  return (
    <ul className="flex flex-wrap gap-1.5" aria-label="What the assistant looked at">
      {calls.map((c, i) => (
        <li
          key={i}
          data-testid="chat-tool-chip"
          className={cn(
            "inline-flex max-w-full items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs",
            c.ok ? "border-border bg-card text-muted-foreground" : "border-warning/40 bg-warning/10 text-warning",
          )}
        >
          <Wrench className="size-3 shrink-0" aria-hidden />
          <span className="truncate">
            {toolLabel(c)}
            {c.ok ? (c.truncated ? " (shortened)" : "") : " (did not work)"}
          </span>
        </li>
      ))}
    </ul>
  );
}

function Notices({ notices }: { notices: ChatNoticeKind[] }) {
  const shown = notices.filter((n, i) => notices.indexOf(n) === i && n in NOTICE_TEXT);
  if (!shown.length) return null;
  return (
    <div className="grid gap-1.5">
      {shown.map((n) => (
        <div key={n} data-testid={`chat-notice-${n}`}>
          <InlineMessage tone={NOTICE_TONE[n]} size="s" role="none" action={<DocsLink k={n} />}>
            {NOTICE_TEXT[n]}
          </InlineMessage>
        </div>
      ))}
    </div>
  );
}

function Answer({ turn }: { turn: AssistantEntry }) {
  return (
    <div data-testid="chat-turn-assistant" className="grid gap-2">
      <div className="max-w-[95%] rounded-2xl rounded-tl-sm border bg-card px-3.5 py-2.5 text-sm text-card-foreground">
        <span className="sr-only">Assistant: </span>
        <SafeText text={turn.text} />
      </div>
      <ToolChips calls={turn.toolCalls} />
      <Notices notices={turn.notices} />
    </div>
  );
}

function Question({ text, pending = false }: { text: string; pending?: boolean }) {
  return (
    <div data-testid="chat-turn-user" className="flex justify-end">
      <p className={cn("max-w-[85%] rounded-2xl rounded-tr-sm bg-accent px-3.5 py-2.5 text-sm break-words whitespace-pre-wrap text-accent-foreground", pending && "opacity-80")}>
        <span className="sr-only">You: </span>
        {text}
      </p>
    </div>
  );
}

// ------------------------------------------------------------------------------ panel

export function ChatPanel({ transport, availability, open, onOpenChange, returnFocus, readoutSeq = null }: ChatPanelProps) {
  const [state, dispatch] = useReducer(chatReducer, initialChatState);
  const [draft, setDraft] = useState("");
  const input = useRef<HTMLTextAreaElement>(null);
  const log = useRef<HTMLDivElement>(null);
  const inflight = useRef<{ gen: number; ctrl: AbortController } | null>(null);
  const gen = useRef(0);
  // A mutation, so the surface's mutation cache sees chat errors too (a share link that
  // ended answers 410 and the share app shows the link-ended page).
  const ask = useMutation<ChatResponse, unknown, { req: ChatRequest; signal: AbortSignal }>({
    mutationFn: ({ req, signal }) => transport.send(req, signal),
  });

  const run = (req: ChatRequest) => {
    inflight.current?.ctrl.abort();
    const mine = { gen: ++gen.current, ctrl: new AbortController() };
    inflight.current = mine;
    ask.mutate(
      { req, signal: mine.ctrl.signal },
      {
        onSuccess: (response) => {
          if (gen.current === mine.gen) dispatch({ type: "replied", response });
        },
        onError: (error) => {
          if (gen.current === mine.gen && !mine.ctrl.signal.aborted) dispatch({ type: "failed", error });
        },
      },
    );
  };

  const send = (text: string) => {
    if (sendBlock(state, text) !== null) return;
    run(requestFor(state, text.trim(), readoutSeq));
    dispatch({ type: "send", text });
    setDraft("");
  };

  const retry = () => {
    if (!state.failed || state.pending) return;
    run(requestFor(state, state.failed.text, readoutSeq));
    dispatch({ type: "retry" });
  };

  const reset = () => {
    gen.current += 1;
    inflight.current?.ctrl.abort();
    inflight.current = null;
    dispatch({ type: "reset" });
    setDraft("");
    input.current?.focus();
  };

  useEffect(
    () => () => {
      gen.current += 1;
      inflight.current?.ctrl.abort();
    },
    [],
  );

  // Keep the newest turn in view.
  const count = state.turns.length + (state.pending ? 1 : 0) + (state.failed ? 1 : 0);
  useEffect(() => {
    const el = log.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [count, open]);

  const full = isFull(state);
  const block = sendBlock(state, draft);
  const empty = state.turns.length === 0 && !state.pending && !state.failed;
  const suggestions = (availability.suggestions?.length ? availability.suggestions : CHAT_SUGGESTIONS).slice(0, 4);
  const days = availability.transcript_days ?? 30;
  const near = draft.length > CHAT_MAX_USER_CHARS * 0.9;

  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange} modal={false}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Content
          data-testid="chat-panel"
          aria-describedby="chat-panel-description"
          onOpenAutoFocus={(e) => {
            e.preventDefault();
            input.current?.focus();
          }}
          onCloseAutoFocus={(e) => {
            e.preventDefault();
            returnFocus?.current?.focus();
          }}
          // Not modal: reading the dashboard beside the panel never closes it.
          onInteractOutside={(e) => e.preventDefault()}
          className={cn(
            "fixed z-40 flex flex-col border bg-popover text-popover-foreground shadow-2xl",
            "inset-y-0 right-0 w-[min(440px,100vw)] border-y-0 border-r-0",
            "max-sm:inset-x-0 max-sm:top-auto max-sm:bottom-0 max-sm:h-[85svh] max-sm:w-full max-sm:rounded-t-2xl max-sm:border-x-0 max-sm:border-b-0",
          )}
        >
          <header className="flex items-start gap-3 border-b px-4 py-3">
            <div className="grid min-w-0 flex-1 gap-0.5">
              <DialogPrimitive.Title className="font-display text-base font-semibold text-foreground">Ask about this dashboard</DialogPrimitive.Title>
              <DialogPrimitive.Description id="chat-panel-description" className="text-xs text-muted-foreground">
                An AI assistant reads this dashboard for you. It can make mistakes, so check important numbers on the dashboard.
              </DialogPrimitive.Description>
            </div>
            {state.turns.length || state.failed ? (
              <Button variant="ghost" size="sm" className="px-2.5" onClick={reset}>
                <RotateCcw /> New chat
              </Button>
            ) : null}
            <DialogPrimitive.Close className="focus-ring rounded-md p-1.5 text-muted-foreground hover:bg-surface-hover hover:text-foreground" aria-label="Close the chat">
              <X className="size-4" aria-hidden />
            </DialogPrimitive.Close>
          </header>

          <div ref={log} role="log" aria-live="polite" aria-relevant="additions" aria-label="Conversation" className="grid flex-1 content-start gap-4 overflow-y-auto px-4 py-4">
            {availability.keeps_transcripts ? (
              <div data-testid="chat-transcript-notice">
                <InlineMessage tone="info" size="s" role="none" action={<DocsLink k="transcripts" />}>
                  This dashboard keeps chats for {days} days. Its analysts can read them.
                </InlineMessage>
              </div>
            ) : null}
            {empty ? (
              <div className="grid gap-3">
                <p className="text-sm text-muted-foreground">Ask a question about the numbers on this dashboard, or start with one of these:</p>
                <ul className="grid gap-2" aria-label="Suggested questions">
                  {suggestions.map((s) => (
                    <li key={s}>
                      <button
                        type="button"
                        onClick={() => send(s)}
                        className="focus-ring w-full rounded-xl border bg-card px-3.5 py-2.5 text-left text-sm text-foreground transition-colors hover:border-primary hover:bg-accent"
                      >
                        {s}
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
            {state.turns.map((t, i) => (t.role === "user" ? <Question key={i} text={t.text} /> : <Answer key={i} turn={t} />))}
            {state.pending ? (
              <>
                <Question text={state.pending.text} pending />
                <p className="flex items-center gap-2 text-sm text-muted-foreground" role="status">
                  <Loader2 className="size-4 animate-spin motion-reduce:animate-none" aria-hidden />
                  {readoutSeq ? `Looking at readout ${readoutSeq}…` : "Looking at the dashboard…"}
                </p>
              </>
            ) : null}
            {state.failed ? (
              <>
                <Question text={state.failed.text} />
                <div data-testid={`chat-error-${state.failed.error.kind}`}>
                  <InlineMessage
                    tone={state.failed.error.kind === "budget" ? "warning" : "error"}
                    action={
                      state.failed.error.kind === "history" ? (
                        <Button size="sm" variant="secondary" onClick={reset}>
                          New chat
                        </Button>
                      ) : canRetry(state.failed.error) ? (
                        <Button size="sm" variant="secondary" onClick={retry}>
                          Try again
                        </Button>
                      ) : state.failed.error.kind === "budget" ? (
                        <DocsLink k="budget" />
                      ) : undefined
                    }
                  >
                    {errorText(state.failed.error)}
                  </InlineMessage>
                </div>
              </>
            ) : null}
            {full && !state.pending ? (
              <InlineMessage tone="info" action={<Button size="sm" variant="secondary" onClick={reset}>New chat</Button>}>
                This chat is full. Start a new chat to ask more.
              </InlineMessage>
            ) : null}
          </div>

          <form
            className="grid gap-1.5 border-t px-4 py-3"
            onSubmit={(e) => {
              e.preventDefault();
              send(draft);
            }}
          >
            <div className="flex items-end gap-2">
              <textarea
                ref={input}
                data-testid="chat-input"
                aria-label="Your question"
                rows={2}
                maxLength={CHAT_MAX_USER_CHARS}
                value={draft}
                disabled={full}
                placeholder="Ask about the numbers on this dashboard"
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                    e.preventDefault();
                    send(draft);
                  }
                }}
                className="focus-ring max-h-40 min-h-11 flex-1 resize-none rounded-lg border border-input bg-card px-3 py-2 text-sm text-foreground placeholder:text-hint disabled:opacity-60"
              />
              <Button type="submit" size="icon" data-testid="chat-send" aria-label="Send" disabled={block !== null}>
                {state.pending ? <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden /> : <SendHorizontal aria-hidden />}
              </Button>
            </div>
            <p className="flex justify-between gap-3 text-2xs text-hint">
              <span>Enter sends. Shift+Enter starts a new line.</span>
              {near ? (
                <span aria-live="polite">
                  {draft.length.toLocaleString()} of {CHAT_MAX_USER_CHARS.toLocaleString()} characters
                </span>
              ) : null}
            </p>
          </form>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

export default ChatPanel;
