/**
 * Builder › Setup › Chat (v0.20, plan D4, D7, D8, D24): "Ask about your dashboard" for one
 * dashboard. Editors turn chat on or off, choose who can use it (team members, client users,
 * share links), set the daily token budgets, choose to keep conversations for 30 days, and see
 * what chat used today. Kept conversations are listed under the form (`chat-transcripts.tsx`).
 *
 * The settings are not part of the draft: a save applies at once to the published dashboard,
 * makes no new revision and changes no readout. A save sends the `etag` that was read
 * (`If-Match`); when someone else saved in between, the server answers 412 and the form shows
 * their settings again. Chat works only when the published revision is layout 2.
 */
import { MessageSquareText, Save } from "lucide-react";
import { useState, type FormEvent } from "react";

import { can } from "@/api/access";
import { ErrorNotice, LoadingRows, Spinner } from "@/components/common";
import { grouped, InlineMessage } from "@/components/ods";
import { Button } from "@/components/ui/button";
import { Checkbox, Switch } from "@/components/ui/overlays";
import { Field, Input, Label, Progress } from "@/components/ui/primitives";
import { useWorkspace } from "@/features/auth/workspace";
import { relativeTime } from "@/lib/format";
import { toast } from "@/stores/toast";

import { isSettingsConflict, settingsUpdateOf, useChatSettings, useSaveChatSettings } from "../chat/settings-api";
import { CHAT_MAX_SHARE_TOKENS_PER_DAY, CHAT_MAX_TOKENS_PER_DAY, CHAT_TRANSCRIPT_DAYS, type ChatAudiences, type ChatSettingsUpdate, type ChatSettingsView } from "../chat/types";
import { Section } from "../readout/parts";
import { isLayout2, type Dashboard } from "../types";
import { ChatTranscripts } from "./chat-transcripts";
import { useBuilderDashboard } from "./use-builder";

/** Why chat cannot work on this dashboard's layout, or `null` (plan decision 5). */
export function chatLayoutBlock(d: Pick<Dashboard, "draft_spec" | "published_revision">): "draft_v1" | "published_v1" | null {
  if (!isLayout2(d.draft_spec)) return "draft_v1";
  const published = d.published_revision?.spec;
  if (published && !isLayout2(published)) return "published_v1";
  return null;
}

/** The form's working copy: budgets stay text while they are typed. */
export interface ChatSettingsForm {
  enabled: boolean;
  audiences: Required<ChatAudiences>;
  keep_transcripts: boolean;
  tokens_per_day: string;
  share_tokens_per_day: string;
}

export function formOf(v: ChatSettingsView): ChatSettingsForm {
  const u = settingsUpdateOf(v);
  return {
    enabled: u.enabled,
    audiences: { members: u.audiences.members ?? true, clients: u.audiences.clients ?? true, share_links: u.audiences.share_links ?? false },
    keep_transcripts: u.keep_transcripts,
    tokens_per_day: String(u.tokens_per_day),
    share_tokens_per_day: String(u.share_tokens_per_day),
  };
}

function budgetIssue(text: string, max: number): string | undefined {
  const t = text.trim();
  if (!/^\d+$/.test(t)) return "Type a whole number of tokens.";
  const n = Number(t);
  if (n > max) return `Use at most ${grouped(max)} tokens.`;
  return undefined;
}

/** The problems of the form, by field (plain words); empty when the server will take it. */
export function chatFormIssues(f: ChatSettingsForm): { tokens_per_day?: string; share_tokens_per_day?: string } {
  const out: { tokens_per_day?: string; share_tokens_per_day?: string } = {};
  const a = budgetIssue(f.tokens_per_day, CHAT_MAX_TOKENS_PER_DAY);
  const b = budgetIssue(f.share_tokens_per_day, CHAT_MAX_SHARE_TOKENS_PER_DAY);
  if (a) out.tokens_per_day = a;
  if (b) out.share_tokens_per_day = b;
  return out;
}

export function updateOf(f: ChatSettingsForm): ChatSettingsUpdate {
  return {
    enabled: f.enabled,
    audiences: { ...f.audiences },
    keep_transcripts: f.keep_transcripts,
    tokens_per_day: Number(f.tokens_per_day.trim()),
    share_tokens_per_day: Number(f.share_tokens_per_day.trim()),
  };
}

const sameForm = (a: ChatSettingsForm, b: ChatSettingsForm) => JSON.stringify(a) === JSON.stringify(b);

/** An estimated cost in micro-dollars, in words ("about US$0.04", "less than US$0.01"). */
export function costWords(micros: number): string {
  if (micros <= 0) return "US$0.00";
  if (micros < 10_000) return "less than US$0.01";
  return `about US$${(micros / 1_000_000).toFixed(2)}`;
}

/** The Chat section of the builder's Setup tab (the dashboard comes from the route). */
export function ChatSettingsSection() {
  const d = useBuilderDashboard();
  return d ? <ChatSettings dashboard={d} /> : null;
}

const AUDIENCES: { key: keyof ChatAudiences; label: string; hint: string }[] = [
  { key: "members", label: "Team members", hint: "People of this project who open the dashboard in the app." },
  { key: "clients", label: "Client users", hint: "Clients that you gave access to this dashboard, in their portal." },
  { key: "share_links", label: "Share links", hint: "Everyone who opens a link. All people with the same link share its budget. Off by default." },
];

export function ChatSettings({ dashboard }: { dashboard: Dashboard }) {
  const ws = useWorkspace();
  const editor = can(ws.grants, "update", "dashboard");
  const block = chatLayoutBlock(dashboard);
  return (
    <Section
      data-testid="chat-settings"
      title="Chat"
      description="Let people ask questions about this dashboard in plain words. The answers use only what that person can already see on the dashboard."
    >
      {block ? (
        <InlineMessage tone="info" role="none">
          <span className="font-medium">Upgrade the layout to use chat.</span>{" "}
          {block === "draft_v1" ? "Chat works only on dashboards with the panels layout (layout 2)." : "The published dashboard still uses the old layout. Publish this draft to use chat."}
        </InlineMessage>
      ) : null}
      {!editor ? (
        <p className="text-sm text-muted-foreground">Only editors and admins change the chat settings.</p>
      ) : block === "draft_v1" ? null : (
        <ChatSettingsBody dashboard={dashboard} />
      )}
    </Section>
  );
}

function ChatSettingsBody({ dashboard }: { dashboard: Dashboard }) {
  const q = useChatSettings(dashboard.id);
  if (q.isPending) return <LoadingRows rows={3} />;
  if (q.isError) return <ErrorNotice error={q.error} title="The app could not read the chat settings" onRetry={() => void q.refetch()} />;
  return (
    <>
      <ChatSettingsForm dashboard={dashboard} view={q.data} />
      <ChatTranscripts dashboardId={dashboard.id} keeping={q.data.keep_transcripts ?? false} days={q.data.transcript_days ?? CHAT_TRANSCRIPT_DAYS} />
    </>
  );
}

function ChatSettingsForm({ dashboard, view }: { dashboard: Dashboard; view: ChatSettingsView }) {
  const save = useSaveChatSettings(dashboard.id);
  const [draft, setDraft] = useState<ChatSettingsForm | null>(null);
  const [conflict, setConflict] = useState(false);
  const saved = formOf(view);
  const form = draft ?? saved;
  const issues = chatFormIssues(form);
  const dirty = draft !== null && !sameForm(draft, saved);
  const set = (patch: Partial<ChatSettingsForm>) => {
    setConflict(false);
    setDraft({ ...form, ...patch });
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!dirty || Object.keys(issues).length) return;
    setConflict(false);
    save.mutate(
      { body: updateOf(form), etag: view.etag },
      {
        onSuccess: (v) => {
          setDraft(null);
          toast.success("Chat settings saved", v.enabled ? "The change applies from the next question." : "Chat is off for this dashboard.");
        },
        onError: (err) => {
          if (isSettingsConflict(err)) {
            setDraft(null);
            setConflict(true);
          }
        },
      },
    );
  };
  const published = dashboard.status === "published";
  const budget = view.tokens_per_day ?? 0;
  const used = view.used_today_tokens ?? 0;
  return (
    <form className="grid gap-5" onSubmit={submit} noValidate aria-label="Chat settings">
      <div className="flex items-start gap-3">
        <Switch id="chat-enabled" data-testid="chat-enabled" checked={form.enabled} onCheckedChange={(on) => set({ enabled: on })} />
        <span className="grid gap-0.5">
          <Label htmlFor="chat-enabled">Turn on chat for this dashboard</Label>
          <span className="text-xs text-muted-foreground">
            {published ? "A chat button shows on the readout for the people you choose below." : "Chat starts when you publish the dashboard."} Questions, and the dashboard numbers that an answer needs, go to Google
            (Gemini) to make the answer.
          </span>
        </span>
      </div>

      <fieldset className="grid gap-2" disabled={!form.enabled}>
        <legend className="mb-1 text-sm font-semibold">Who can chat</legend>
        {AUDIENCES.map((a) => {
          const id = `chat-aud-${a.key}`;
          return (
            <div key={a.key} className="flex items-start gap-2 text-sm">
              <Checkbox
                id={id}
                className="mt-0.5"
                checked={form.audiences[a.key]}
                onCheckedChange={(on) => set({ audiences: { ...form.audiences, [a.key]: on === true } })}
                aria-describedby={`${id}-hint`}
              />
              <span className="grid gap-0.5">
                <label htmlFor={id} className="font-medium">
                  {a.label}
                </label>
                <span id={`${id}-hint`} className="text-xs text-muted-foreground">
                  {a.hint}
                </span>
              </span>
            </div>
          );
        })}
      </fieldset>

      <div className="flex items-start gap-3">
        <Switch id="chat-keep" checked={form.keep_transcripts} onCheckedChange={(on) => set({ keep_transcripts: on })} />
        <span className="grid gap-0.5">
          <Label htmlFor="chat-keep">Keep conversations for {view.transcript_days ?? CHAT_TRANSCRIPT_DAYS} days</Label>
          <span className="text-xs text-muted-foreground">
            People see a notice before they send their first message. Editors can read and delete the conversations below. When this is off, the app keeps no conversation.
          </span>
        </span>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Tokens each day for the dashboard" htmlFor="chat-budget" error={issues.tokens_per_day} hint={`For all people together. 0 stops chat. At most ${grouped(CHAT_MAX_TOKENS_PER_DAY)}.`}>
          <Input id="chat-budget" inputMode="numeric" className="tabular" value={form.tokens_per_day} onChange={(e) => set({ tokens_per_day: e.target.value })} />
        </Field>
        <Field
          label="Tokens each day for each share link"
          htmlFor="chat-share-budget"
          error={issues.share_tokens_per_day}
          hint={`Also counts toward the dashboard's tokens. At most ${grouped(CHAT_MAX_SHARE_TOKENS_PER_DAY)}.`}
        >
          <Input id="chat-share-budget" inputMode="numeric" className="tabular" value={form.share_tokens_per_day} onChange={(e) => set({ share_tokens_per_day: e.target.value })} />
        </Field>
      </div>

      <div className="grid gap-1.5" data-testid="chat-used-today">
        <p className="flex flex-wrap items-baseline justify-between gap-2 text-sm">
          <span className="font-medium">Used today</span>
          <span className="tabular text-muted-foreground">
            {grouped(used)} of {grouped(budget)} tokens, {costWords(view.est_micros_today ?? 0)}
          </span>
        </p>
        <Progress value={budget > 0 ? used / budget : used > 0 ? 1 : 0} label="Chat tokens used today" barClassName={budget > 0 && used >= budget ? "bg-destructive" : undefined} />
        <p className="text-xs text-muted-foreground">The day starts at midnight UTC. A question with its answer uses about 2,000 to 10,000 tokens.</p>
      </div>

      {conflict ? (
        <InlineMessage tone="warning" role="alert">
          Someone changed the chat settings after you opened them. The form now shows their settings. Make your change again, then save.
        </InlineMessage>
      ) : null}
      {save.isError && !isSettingsConflict(save.error) ? <ErrorNotice error={save.error} title="The chat settings were not saved" /> : null}

      <div className="flex flex-wrap items-center gap-2">
        <Button type="submit" disabled={!dirty || Object.keys(issues).length > 0 || save.isPending}>
          {save.isPending ? <Spinner /> : <Save />} Save chat settings
        </Button>
        {dirty ? (
          <Button
            variant="ghost"
            onClick={() => {
              setDraft(null);
              setConflict(false);
            }}
          >
            Discard changes
          </Button>
        ) : null}
        <span className="ml-auto flex items-center gap-1.5 text-xs text-muted-foreground">
          <MessageSquareText className="size-3.5" aria-hidden />
          {view.updated_at ? `Changed ${relativeTime(view.updated_at)}. ` : ""}A save applies at once and does not change the draft.
        </span>
      </div>
    </form>
  );
}
