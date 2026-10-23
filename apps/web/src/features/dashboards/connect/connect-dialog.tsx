/**
 * "Connect a chat tool" (v0.20, plan D13, D24): make, list and revoke dashboard read tokens,
 * and copy a client configuration for the MCP stdio bridge or streamable HTTP.
 *
 * A read token reads this one dashboard (published, non-private readouts) and works only on the
 * MCP endpoint. A member or a client user makes their own; the server forces the scope, the
 * owner and the lifetime (at most 90 days), and re-checks the person's access on every use.
 * The secret is in this dialog's state only: it is shown once, it is never put in the query
 * cache or in browser storage, and it is gone when the dialog closes. Editors can also list and
 * revoke everyone's tokens of the dashboard.
 */
import { KeyRound, Plug, Trash2 } from "lucide-react";
import { useState, type FormEvent } from "react";

import { can } from "@/api/access";
import { errorMessage } from "@/api/client";
import { CopyButton, ErrorNotice, Mono, QueryView, Spinner } from "@/components/common";
import { InlineMessage } from "@/components/ods";
import { Button } from "@/components/ui/button";
import { Checkbox, Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle, SegmentedControl } from "@/components/ui/overlays";
import { Badge, Field, Input, Label, NativeSelect, TBody, TD, TH, THead, TR, Table } from "@/components/ui/primitives";
import { useWorkspace } from "@/features/auth/workspace";
import { SecretField } from "@/features/integrations/shown-once";
import { env } from "@/env";
import { absoluteTime, relativeTime } from "@/lib/format";
import { toast } from "@/stores/toast";

import { isActiveToken, isReadTokenLimit, readTokenInputProblem, useCreateReadToken, useReadTokens, useRevokeReadToken } from "../chat/read-tokens-api";
import { READ_TOKEN_DEFAULT_DAYS, READ_TOKEN_MAX_DAYS, READ_TOKEN_NAME_MAX, READ_TOKENS_PER_USER, type ReadToken, type ReadTokenCreated } from "../chat/types";
import { SNIPPET_KINDS, SNIPPET_WORDS, snippetOf, type SnippetKind } from "./mcp-snippets";

/** The lifetimes offered for a new token (the server allows 1 to 90 days). */
export const READ_TOKEN_DAY_CHOICES = [7, READ_TOKEN_DEFAULT_DAYS, 60, READ_TOKEN_MAX_DAYS] as const;

const TOKEN_STATUS = { active: "success", expired: "outline", revoked: "danger" } as const;

function statusOf(t: ReadToken, now: number): keyof typeof TOKEN_STATUS {
  if (t.revoked_at) return "revoked";
  return isActiveToken(t, now) ? "active" : "expired";
}

export function ConnectDialog({ dashboardId, title, onClose }: { dashboardId: string; title?: string | null | undefined; onClose: () => void }) {
  const ws = useWorkspace();
  const editor = can(ws.grants, "update", "dashboard");
  const [created, setCreated] = useState<ReadTokenCreated | null>(null);
  const [copied, setCopied] = useState(false);
  const close = () => {
    if (!created || copied || window.confirm("Close the window? You did not copy the token, and the app cannot show it again.")) onClose();
  };
  return (
    <Dialog open onOpenChange={(o) => !o && close()}>
      <DialogContent wide data-testid="connect-dialog" onInteractOutside={(e) => created && e.preventDefault()}>
        <DialogTitle>Connect a chat tool</DialogTitle>
        <DialogDescription>
          A chat tool, for example a desktop assistant, can read this dashboard through MCP (Model Context Protocol). It reads the published readouts and panels. It cannot change anything or open another dashboard.
        </DialogDescription>
        <InlineMessage tone="warning" role="none">
          Connect only tools that you trust. If the same tool can also send e-mail or open web pages, it can send the dashboard's numbers to other people.
        </InlineMessage>
        {created ? (
          <CreatedToken
            created={created}
            onCopied={() => setCopied(true)}
            onDismiss={() => {
              setCreated(null);
              setCopied(false);
            }}
          />
        ) : (
          <CreateTokenForm dashboardId={dashboardId} onCreated={setCreated} />
        )}
        <Snippets secret={created?.secret ?? null} title={title} />
        <TokenList dashboardId={dashboardId} editor={editor} />
        <DialogFooter>
          <Button variant="ghost" onClick={close}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ------------------------------------------------------------------------------ new token

function CreateTokenForm({ dashboardId, onCreated }: { dashboardId: string; onCreated: (t: ReadTokenCreated) => void }) {
  const create = useCreateReadToken(dashboardId);
  const [name, setName] = useState("");
  const [days, setDays] = useState<number>(READ_TOKEN_DEFAULT_DAYS);
  const [submitted, setSubmitted] = useState(false);
  const problem = readTokenInputProblem({ name, expires_in_days: days });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    if (problem) return;
    create.mutate(
      { name, expires_in_days: days },
      {
        onSuccess: (t) => {
          setName("");
          setSubmitted(false);
          onCreated(t);
        },
      },
    );
  };
  return (
    <form className="grid gap-3" onSubmit={submit} noValidate data-testid="read-token-create" aria-label="Make a read token">
      <div className="grid gap-0.5">
        <p className="text-sm font-semibold">Make a read token</p>
        <p className="text-xs text-muted-foreground">One token for each tool. Name it after the tool, so you know which one to revoke.</p>
      </div>
      <div className="grid gap-3 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)_auto] sm:items-end">
        <Field label="Name" htmlFor="rt-name" error={submitted && problem?.includes("name") ? problem : undefined}>
          <Input id="rt-name" maxLength={READ_TOKEN_NAME_MAX} autoComplete="off" placeholder="Laptop assistant" value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="Ends after" htmlFor="rt-days" error={submitted && problem && !problem.includes("name") ? problem : undefined}>
          <NativeSelect id="rt-days" value={days} onChange={(e) => setDays(Number(e.target.value))}>
            {READ_TOKEN_DAY_CHOICES.map((d) => (
              <option key={d} value={d}>
                {d} days{d === READ_TOKEN_DEFAULT_DAYS ? " (default)" : ""}
              </option>
            ))}
          </NativeSelect>
        </Field>
        <Button type="submit" disabled={create.isPending}>
          {create.isPending ? <Spinner /> : <KeyRound />} Make token
        </Button>
      </div>
      {create.isError ? (
        isReadTokenLimit(create.error) ? (
          <InlineMessage tone="error">You have {READ_TOKENS_PER_USER} active tokens for this dashboard. Revoke one before you make a new one.</InlineMessage>
        ) : (
          <ErrorNotice error={create.error} title="The app could not make the token" />
        )
      ) : null}
    </form>
  );
}

/** The new token, one time. The secret lives in the dialog's state only. */
function CreatedToken({ created, onCopied, onDismiss }: { created: ReadTokenCreated; onCopied: () => void; onDismiss: () => void }) {
  return (
    <div className="grid gap-3 rounded-xl border border-primary/35 bg-accent/40 p-4" data-testid="read-token-secret">
      <InlineMessage tone="success" size="s">
        Copy the token now. The app shows it only one time. It works until {absoluteTime(created.expires_at)}.
      </InlineMessage>
      <SecretField id="read-token-secret-value" label={`Read token “${created.name}”`} value={created.secret} copyLabel="Copy read token" onCopied={onCopied} />
      <div className="flex justify-end">
        <Button size="sm" variant="secondary" onClick={onDismiss}>
          Make another token
        </Button>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------------------ snippets

function Snippets({ secret, title }: { secret: string | null; title?: string | null | undefined }) {
  const [kind, setKind] = useState<SnippetKind>("stdio");
  const text = snippetOf(kind, { apiUrl: env.apiBase, secret, title: title ?? null });
  return (
    <section className="grid gap-2" aria-labelledby="connect-snippet-title">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id="connect-snippet-title" className="text-sm font-semibold">
          Add it to your chat tool
        </h3>
        <SegmentedControl label="How the tool connects" value={kind} onValueChange={setKind} options={SNIPPET_KINDS.map((k) => ({ value: k, label: SNIPPET_WORDS[k].label }))} />
      </div>
      <p className="text-xs text-muted-foreground">
        {SNIPPET_WORDS[kind].hint}
        {secret ? " The configuration below holds your new token: keep it private." : " Put your token where the configuration says so."}
      </p>
      <div className="relative min-w-0">
        <pre data-testid={`connect-snippet-${kind}`} className="max-h-60 overflow-auto rounded-md border bg-muted/60 p-3 pr-10 font-mono text-xs leading-5 whitespace-pre text-foreground/90">
          {text}
        </pre>
        <div className="absolute top-1.5 right-1.5">
          <CopyButton value={text} label="Copy configuration" onCopied={() => toast.success("Copied to the clipboard")} />
        </div>
      </div>
    </section>
  );
}

// ------------------------------------------------------------------------------ token list

function TokenList({ dashboardId, editor }: { dashboardId: string; editor: boolean }) {
  const [all, setAll] = useState(false);
  const tokens = useReadTokens(dashboardId, { all: editor && all });
  const revoke = useRevokeReadToken(dashboardId);
  const [confirming, setConfirming] = useState<string | null>(null);
  const now = Date.now();
  return (
    <section className="grid gap-2" aria-labelledby="connect-tokens-title">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id="connect-tokens-title" className="text-sm font-semibold">
          {editor && all ? "Read tokens of this dashboard" : "Your read tokens"}
        </h3>
        {editor ? (
          <span className="flex items-center gap-2 text-sm">
            <Checkbox id="rt-all" checked={all} onCheckedChange={(v) => setAll(v === true)} />
            <Label htmlFor="rt-all">Show everyone's tokens</Label>
          </span>
        ) : null}
      </div>
      <QueryView query={tokens}>
        {(items) =>
          items.length ? (
            <div className="min-w-0 overflow-x-auto">
              <Table aria-label="Read tokens">
                <THead>
                  <TR>
                    <TH>Token</TH>
                    {editor && all ? <TH>Owner</TH> : null}
                    <TH>Status</TH>
                    <TH>Ends</TH>
                    <TH>Last used</TH>
                    <TH>
                      <span className="sr-only">Actions</span>
                    </TH>
                  </TR>
                </THead>
                <TBody>
                  {items.map((t) => {
                    const status = statusOf(t, now);
                    return (
                      <TR key={t.id} data-testid="read-token-row" data-token={t.id} data-status={status}>
                        <TD>
                          <span className="block font-medium [overflow-wrap:anywhere]">{t.name}</span>
                          <Mono className="block text-muted-foreground">{t.prefix}…</Mono>
                        </TD>
                        {editor && all ? <TD className="[overflow-wrap:anywhere]">{t.owner_label}</TD> : null}
                        <TD>
                          <Badge variant={TOKEN_STATUS[status]}>{status}</Badge>
                        </TD>
                        <TD title={absoluteTime(t.revoked_at ?? t.expires_at)}>{status === "revoked" ? `revoked ${relativeTime(t.revoked_at)}` : relativeTime(t.expires_at)}</TD>
                        <TD title={t.last_used_at ? absoluteTime(t.last_used_at) : undefined}>{t.last_used_at ? relativeTime(t.last_used_at) : "Never"}</TD>
                        <TD className="text-right whitespace-nowrap">
                          {status !== "active" ? null : confirming === t.id ? (
                            <span className="inline-flex gap-1">
                              <Button variant="ghost" size="sm" onClick={() => setConfirming(null)}>
                                Keep
                              </Button>
                              <Button
                                variant="destructive"
                                size="sm"
                                disabled={revoke.isPending}
                                onClick={() =>
                                  revoke.mutate(t.id, {
                                    onSuccess: () => {
                                      toast.success("Token revoked", `${t.name}: the next request with it fails.`);
                                      setConfirming(null);
                                    },
                                    onError: (e) => toast.error("The app could not revoke the token", errorMessage(e)),
                                  })
                                }
                              >
                                {revoke.isPending ? <Spinner /> : <Trash2 />} Revoke now
                              </Button>
                            </span>
                          ) : (
                            <Button variant="ghost" size="sm" onClick={() => setConfirming(t.id)} aria-label={`Revoke ${t.name}`}>
                              <Trash2 /> Revoke
                            </Button>
                          )}
                        </TD>
                      </TR>
                    );
                  })}
                </TBody>
              </Table>
            </div>
          ) : (
            <p className="flex items-center gap-2 text-sm text-muted-foreground">
              <Plug className="size-4" aria-hidden /> {editor && all ? "Nobody has a read token for this dashboard." : "You have no read tokens for this dashboard."}
            </p>
          )
        }
      </QueryView>
      <p className="text-xs text-muted-foreground">A token stops working when you revoke it, when it ends, or when you lose access to the dashboard.</p>
    </section>
  );
}
