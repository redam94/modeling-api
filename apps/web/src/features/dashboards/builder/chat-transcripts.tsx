/**
 * Builder › Setup › Chat › kept conversations (v0.20, plan D8, D24): when a dashboard keeps
 * chats, editors read and delete them here. A conversation is kept for 30 days and then removed
 * automatically (conversations kept before the setting was turned off stay until they end).
 * Who asked is a person's name or "Share link <name>", never an address.
 *
 * Every text here was typed by a viewer or written by the model: it renders as plain React text
 * (no markup, no links), like any other untrusted string.
 */
import { MessagesSquare, Trash2 } from "lucide-react";
import { useState } from "react";

import { errorMessage } from "@/api/client";
import { ErrorNotice, LoadingRows, QueryView, Spinner } from "@/components/common";
import { TagWord } from "@/components/ods";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from "@/components/ui/overlays";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/primitives";
import { absoluteTime, relativeTime } from "@/lib/format";
import { toast } from "@/stores/toast";

import { useChatTranscript, useChatTranscripts, useDeleteChatTranscript } from "../chat/settings-api";
import { CHAT_TRANSCRIPT_DAYS, type ChatTranscriptSummary, type ChatTranscriptTurn } from "../chat/types";

const KIND_WORD: Record<ChatTranscriptSummary["principal_kind"], string> = { member: "Team", client: "Client", share: "Share link" };

export function ChatTranscripts({ dashboardId, keeping }: { dashboardId: string; keeping: boolean }) {
  const list = useChatTranscripts(dashboardId);
  const [open, setOpen] = useState<ChatTranscriptSummary | null>(null);
  const items = list.data?.pages.flatMap((p) => p.items ?? []) ?? [];
  return (
    <section className="grid gap-2" aria-labelledby="chat-transcripts-title" data-testid="chat-transcripts">
      <h3 id="chat-transcripts-title" className="text-sm font-semibold">
        Kept conversations
      </h3>
      {list.isPending ? (
        <LoadingRows rows={2} />
      ) : list.isError ? (
        <ErrorNotice error={list.error} title="The app could not read the kept conversations" onRetry={() => void list.refetch()} />
      ) : items.length ? (
        <>
          <div className="min-w-0 overflow-x-auto">
            <Table aria-label="Kept conversations">
              <THead>
                <TR>
                  <TH>Asked by</TH>
                  <TH>Turns</TH>
                  <TH>Last message</TH>
                  <TH>Kept until</TH>
                  <TH>
                    <span className="sr-only">Actions</span>
                  </TH>
                </TR>
              </THead>
              <TBody>
                {items.map((t) => (
                  <TR key={t.conversation_id} data-testid="chat-transcript-row" data-conversation={t.conversation_id}>
                    <TD>
                      <span className="block font-medium [overflow-wrap:anywhere]">{t.principal_label}</span>
                      <span className="block text-xs text-muted-foreground">{KIND_WORD[t.principal_kind]}</span>
                    </TD>
                    <TD className="tabular">{t.turns}</TD>
                    <TD title={absoluteTime(t.last_at)}>{relativeTime(t.last_at)}</TD>
                    <TD title={absoluteTime(t.expires_at)}>{relativeTime(t.expires_at)}</TD>
                    <TD className="text-right">
                      <Button variant="ghost" size="sm" onClick={() => setOpen(t)} aria-label={`Read the conversation of ${t.principal_label}, ${relativeTime(t.last_at)}`}>
                        <MessagesSquare /> Read
                      </Button>
                    </TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          </div>
          {list.hasNextPage ? (
            <div>
              <Button variant="secondary" size="sm" disabled={list.isFetchingNextPage} onClick={() => void list.fetchNextPage()}>
                {list.isFetchingNextPage ? <Spinner /> : null} Show more
              </Button>
            </div>
          ) : null}
        </>
      ) : (
        <p className="text-sm text-muted-foreground">
          {keeping ? `No kept conversations yet. New conversations show here for ${CHAT_TRANSCRIPT_DAYS} days.` : "This dashboard keeps no conversations. Turn on “Keep conversations” to see them here."}
        </p>
      )}
      {open ? <TranscriptDialog dashboardId={dashboardId} summary={open} onClose={() => setOpen(null)} /> : null}
    </section>
  );
}

function TranscriptDialog({ dashboardId, summary, onClose }: { dashboardId: string; summary: ChatTranscriptSummary; onClose: () => void }) {
  const q = useChatTranscript(dashboardId, summary.conversation_id);
  const del = useDeleteChatTranscript(dashboardId);
  const [confirming, setConfirming] = useState(false);
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent wide data-testid="chat-transcript-dialog">
        <DialogTitle className="[overflow-wrap:anywhere]">Conversation of {summary.principal_label}</DialogTitle>
        <DialogDescription>
          {KIND_WORD[summary.principal_kind]}, from {absoluteTime(summary.first_at)} to {absoluteTime(summary.last_at)}. The app keeps it until {absoluteTime(summary.expires_at)}.
        </DialogDescription>
        <QueryView query={q} loading={<LoadingRows rows={3} />} errorTitle="The app could not read the conversation">
          {(t) => (
            <ol aria-label="Turns" className="grid gap-3">
              {t.turns.map((turn) => (
                <TurnItem key={turn.index} turn={turn} />
              ))}
            </ol>
          )}
        </QueryView>
        {del.isError ? <ErrorNotice error={del.error} title="The app could not delete the conversation" /> : null}
        <DialogFooter>
          {confirming ? (
            <>
              <span className="mr-auto self-center text-sm">Delete every message of this conversation? You cannot undo this.</span>
              <Button variant="ghost" onClick={() => setConfirming(false)}>
                Keep it
              </Button>
              <Button
                variant="destructive"
                disabled={del.isPending}
                onClick={() =>
                  del.mutate(summary.conversation_id, {
                    onSuccess: () => {
                      toast.success("Conversation deleted", summary.principal_label);
                      onClose();
                    },
                    onError: (e) => toast.error("The app could not delete the conversation", errorMessage(e)),
                  })
                }
              >
                {del.isPending ? <Spinner /> : <Trash2 />} Delete now
              </Button>
            </>
          ) : (
            <>
              <Button variant="ghost" className="mr-auto" onClick={() => setConfirming(true)}>
                <Trash2 /> Delete conversation
              </Button>
              <Button onClick={onClose}>Close</Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function TurnItem({ turn }: { turn: ChatTranscriptTurn }) {
  const user = turn.role === "user";
  return (
    <li data-testid={`chat-transcript-turn-${turn.role}`} className={user ? "grid gap-1 rounded-lg bg-muted p-3" : "grid gap-1 rounded-lg border p-3"}>
      <p className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <span className="font-semibold text-foreground">{user ? "Question" : "Answer"}</span>
        <span title={absoluteTime(turn.at)}>{relativeTime(turn.at)}</span>
        {turn.ok === false ? <TagWord tone="warning">Not answered</TagWord> : null}
      </p>
      {/* Untrusted text: React text only, line breaks kept. */}
      <p className="text-sm whitespace-pre-wrap [overflow-wrap:anywhere]">{turn.text}</p>
      {turn.tool_calls?.length ? (
        <p className="flex flex-wrap gap-1 text-xs text-muted-foreground" aria-label="Tools used">
          {turn.tool_calls.map((name, i) => (
            <span key={`${name}-${i}`} className="rounded bg-muted px-1.5 py-0.5 font-mono">
              {name}
            </span>
          ))}
        </p>
      ) : null}
    </li>
  );
}
