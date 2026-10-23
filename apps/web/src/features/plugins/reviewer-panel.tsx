/**
 * The reviewer panel (D13, D20): what a workspace admin looks at before uploaded code is
 * approved for the team. Inspect facts first (they are true whatever the code does), then the
 * SBOM, every URL, the test report and what changed against the last approved version; then
 * approve or reject. The decision sends the digests this page showed, so a version that
 * changed underneath is refused (409) and the page says so and reloads. Approval needs a
 * recent sign-in (401 `REAUTH_REQUIRED`): the panel keeps the note and sends the reviewer
 * through sign-in and back. The uploader sees why the buttons are off.
 *
 * Every string from the upload is text only.
 */
import { CheckCircle2, LogIn, RefreshCw, XCircle } from "lucide-react";
import { useEffect, useId, useState } from "react";

import { ErrorNotice, Mono } from "@/components/common";
import { InlineMessage } from "@/components/ods/inline-message";
import { Button } from "@/components/ui/button";
import { Field, Textarea } from "@/components/ui/primitives";
import { useWorkspace } from "@/features/auth/workspace";
import { cn } from "@/lib/utils";
import { toast } from "@/stores/toast";

import { isReauthRequired, isStateConflict, reauthHref, reviewRefusal, useDecide } from "./api";
import { ExportsDiff } from "./exports-list";
import { InspectFacts } from "./inspect-facts";
import { KitReport } from "./kit-report";
import { SbomSummary } from "./sbom-summary";
import { decisionBlockReason, decisionBody, isDecidable } from "./states";
import type { PluginExports, PluginVersion } from "./types";
import { NOTE_MAX } from "./types";
import { UrlList } from "./url-list";

/** The note survives the sign-in round trip (this tab only). */
const draftKey = (versionId: string) => `mapi.plugins.reviewNote.${versionId}`;

function readDraft(versionId: string): string {
  try {
    return sessionStorage.getItem(draftKey(versionId)) ?? "";
  } catch {
    return "";
  }
}

function writeDraft(versionId: string, note: string | null) {
  try {
    if (note) sessionStorage.setItem(draftKey(versionId), note);
    else sessionStorage.removeItem(draftKey(versionId));
  } catch {
    // storage unavailable: the note is only lost on a reload
  }
}

/** The review checklist; docs/extend/review-uploads.md lists the same items in the same order (a conformance test checks it). */
export const CHECKLIST = [
  "The file facts look right: one package with the plugin's name, entry points only to that package.",
  "Every requirement is one the platform already has, and each one is necessary for what the plugin does.",
  "You know where each link goes.",
  "The tests that failed or were skipped don't matter for how the team will use it.",
  "You know who uploaded it and why.",
  "The changes are what you expect: What changed matches what the uploader told you. A new model or recipe that nobody told you about is a reason to reject.",
];

export function ReviewerPanel({
  version,
  namespace,
  tenantId,
  uploader,
  previous,
  previousStatus,
  onReload,
}: {
  version: PluginVersion;
  namespace: string;
  tenantId: string;
  /** "Bea Builder", "you". */
  uploader: string;
  /** The last approved version of the plugin (for the diff), if any. */
  previous: { version: string; exports: PluginExports | null | undefined } | null;
  /** Whether the versions list or the last approved version is still loading, or failed. */
  previousStatus?: { loading: boolean; error: unknown; onRetry: () => void };
  onReload: () => void;
}) {
  const ws = useWorkspace();
  const decide = useDecide();
  const [note, setNote] = useState(() => readDraft(version.id));
  const [noteError, setNoteError] = useState<string | null>(null);
  const reasonId = useId();
  const blocked = decisionBlockReason(version, ws.me, tenantId);
  useEffect(() => writeDraft(version.id, note.trim() ? note : null), [version.id, note]);

  const send = (decision: "approve" | "reject") => {
    if (!isDecidable(version)) return;
    if (decision === "reject" && !note.trim()) {
      setNoteError("Write a note for the uploader: say what to change.");
      return;
    }
    setNoteError(null);
    decide.mutate(
      { pluginId: version.plugin_id, versionId: version.id, decision, body: decisionBody(version, note) },
      {
        onSuccess: () => {
          writeDraft(version.id, null);
          setNote("");
          toast.success(decision === "approve" ? "Approved for the team" : "Not approved", decision === "approve" ? `Everyone in the workspace can use ${version.version} now.` : "The uploader gets your note.");
        },
      },
    );
  };

  const err = decide.error;
  return (
    <section data-testid="plugin-reviewer-panel" aria-labelledby="plugin-review-title" className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-5 [&>*]:min-w-0">
      <div className="grid gap-2 rounded-2xl border bg-card p-5">
        <h2 id="plugin-review-title" className="font-display text-2xl leading-tight font-bold">
          Review this upload
        </h2>
        <p className="max-w-[68ch] text-sm text-muted-foreground">
          Approving makes this exact file available to everyone in the workspace. Take your time with the facts below. They come from reading the file, not from running it, so they hold whatever the code does.
        </p>
        <ul className="grid gap-1 pt-1 text-sm" aria-label="Review checklist">
          {CHECKLIST.map((c) => (
            <li key={c} className="flex gap-2">
              <CheckCircle2 aria-hidden className="mt-0.5 size-4 flex-none text-muted-foreground" />
              <span>{c}</span>
            </li>
          ))}
        </ul>
      </div>

      {version.inspection ? <InspectFacts inspection={version.inspection} namespace={namespace} /> : null}
      <SbomSummary sbom={version.inspection?.sbom} />
      <UrlList urls={version.urls ?? []} />
      <KitReport report={version.kit} />
      <ExportsDiff
        current={version.exports_detail}
        previous={previous?.exports}
        previousVersion={previous?.version ?? null}
        uploader={uploader}
        loading={previousStatus?.loading}
        error={previousStatus?.error}
        onRetry={previousStatus?.onRetry}
      />

      <div className="grid gap-4 rounded-2xl border-2 border-primary/30 bg-card p-5" aria-labelledby="plugin-decision-title">
        <div className="grid gap-1">
          <h3 id="plugin-decision-title" className="font-display text-lg leading-tight font-bold">
            Your decision
          </h3>
          <p className="text-sm text-muted-foreground">
            You're deciding on exactly this file and these test results:
          </p>
          <dl className="grid gap-0.5 text-xs">
            <div className="flex flex-wrap gap-x-2">
              <dt className="text-muted-foreground">File</dt>
              <dd>
                <Mono className="[overflow-wrap:anywhere]">{version.overlay_sha256 ?? "—"}</Mono>
              </dd>
            </div>
            <div className="flex flex-wrap gap-x-2">
              <dt className="text-muted-foreground">Exports</dt>
              <dd>
                <Mono className="[overflow-wrap:anywhere]">{version.describe_digest ?? "—"}</Mono>
              </dd>
            </div>
            <div className="flex flex-wrap gap-x-2">
              <dt className="text-muted-foreground">Base image</dt>
              <dd>
                <Mono className="[overflow-wrap:anywhere]">{version.base_image_digest ?? "—"}</Mono>
              </dd>
            </div>
          </dl>
        </div>
        <Field label="Note" htmlFor="plugin-review-note" error={noteError} hint="Needed when you don't approve. The uploader and the other admins see it.">
          <Textarea id="plugin-review-note" value={note} maxLength={NOTE_MAX} disabled={Boolean(blocked)} onChange={(e) => setNote(e.target.value)} />
        </Field>
        {err && isReauthRequired(err) ? (
          <InlineMessage
            tone="warning"
            role="alert"
            action={
              <Button size="sm" asChild>
                <a href={reauthHref()} data-testid="plugin-reauth">
                  <LogIn /> Sign in again
                </a>
              </Button>
            }
          >
            For your safety, approving needs a sign-in from the last 15 minutes. Sign in again and you come back here with your note.
          </InlineMessage>
        ) : err && isStateConflict(err) ? (
          <InlineMessage
            tone="warning"
            role="alert"
            action={
              <Button
                size="sm"
                variant="outline"
                onClick={() => {
                  decide.reset();
                  onReload();
                }}
              >
                <RefreshCw /> Reload
              </Button>
            }
          >
            This version changed while you were looking at it, so nothing was decided. Reload and check it again.
          </InlineMessage>
        ) : err && reviewRefusal(err) ? (
          <InlineMessage tone="warning" role="alert">
            <span data-testid="plugin-review-refused">{reviewRefusal(err)}</span>
          </InlineMessage>
        ) : err ? (
          <ErrorNotice error={err} title="The decision was not saved" />
        ) : null}
        {blocked ? (
          <p id={reasonId} className="text-sm font-medium text-muted-foreground" data-testid="plugin-decision-blocked">
            {blocked}
          </p>
        ) : null}
        <div className="flex flex-wrap gap-2">
          <Button
            data-testid="plugin-approve"
            disabled={Boolean(blocked) || decide.isPending}
            aria-describedby={blocked ? reasonId : undefined}
            className={cn("h-auto min-h-11 rounded-full bg-highlight px-5 text-highlight-foreground hover:bg-highlight/85 active:bg-highlight disabled:bg-muted disabled:text-muted-foreground")}
            onClick={() => send("approve")}
          >
            <CheckCircle2 /> Approve for the team
          </Button>
          <Button
            data-testid="plugin-reject"
            variant="ghost"
            disabled={Boolean(blocked) || decide.isPending}
            aria-describedby={blocked ? reasonId : undefined}
            className="h-auto min-h-11 rounded-full px-5 text-attention ring-1 ring-attention/40 ring-inset hover:bg-attention-soft disabled:text-muted-foreground disabled:ring-border"
            onClick={() => send("reject")}
          >
            <XCircle /> Don't approve
          </Button>
        </div>
      </div>
    </section>
  );
}
