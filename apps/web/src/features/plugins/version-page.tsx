/**
 * One uploaded version (`/plugins/:pluginId/versions/:versionId`, D20): the ink header (the
 * page's one block) with the state chip, who uploaded it and the actions (ask for a review:
 * the page's one inverted ink action; withdraw), then what the state calls for: the live checks,
 * the problems in plain words, the test report, what it adds, the SBOM and the file facts, or,
 * while it waits for a reviewer, the reviewer panel.
 */
import { Link } from "@tanstack/react-router";
import { Send, Undo2 } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";

import { ApiError, errorMessage } from "@/api/client";
import { qk, useMembers, useProjectScope } from "@/api/queries";
import { Breadcrumbs, EmptyState, ErrorNotice, LoadingRows, ProblemNotice } from "@/components/common";
import { InlineMessage } from "@/components/ods/inline-message";
import { Button } from "@/components/ui/button";
import { useWorkspace } from "@/features/auth/workspace";
import { absoluteTime, relativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import { toast } from "@/stores/toast";

import { pk } from "./keys";
import { pluginFlags, usePlugin, usePluginVersion, usePluginVersions, useRequestApproval } from "./api";
import { DevBanner } from "./dev-banner";
import { ExportsList } from "./exports-list";
import { InspectFacts } from "./inspect-facts";
import { KitReport } from "./kit-report";
import { useNameOf, type NameOf } from "./people";
import { ProblemsList } from "./problems-list";
import { ReviewerPanel } from "./reviewer-panel";
import { SbomSummary } from "./sbom-summary";
import { OnlyYouChip, PluginStateChip } from "./state-chip";
import { canRequestApproval, canYank, displayText, isChecking, PAUSED_DESCRIPTION, PAUSED_LABEL, singleAdminNote, stateDescription } from "./states";
import type { Plugin, PluginDecisionRecord, PluginVersion } from "./types";
import { UrlList } from "./url-list";
import { YankDialog } from "./yank-dialog";

const ExamplePlot = lazy(() => import("./example-plot"));

const YANK_REASON: Record<string, string> = {
  withdrawn: "Withdrawn by the uploader or an admin",
  superseded: "Replaced by a newer version",
  security: "Withdrawn for safety",
  rekit_failed: "Its tests failed on the platform's new base image",
};

/**
 * When a polled version moves to another state (the checks finished), the plugin lists and the
 * catalog (the tenant view) are stale: refresh them.
 */
export function useRefreshOnStateChange(v: Pick<PluginVersion, "id" | "state"> | undefined) {
  const qc = useQueryClient();
  const s = useProjectScope();
  const last = useRef<string | null>(null);
  const key = v ? `${v.id}:${v.state}` : null;
  useEffect(() => {
    if (!key) return;
    if (last.current && last.current !== key && last.current.split(":")[0] === key.split(":")[0]) {
      void qc.invalidateQueries({ queryKey: pk.lists(s.key) });
      void qc.invalidateQueries({ queryKey: qk.catalog });
    }
    last.current = key;
  }, [key, qc, s.key]);
}

/**
 * The live checks: file check, then tests (the step that runs is the active one). While the
 * operator has paused uploaded code (`GET /v1/me` `plugins_paused`), the checks wait in the
 * queue: the steps say so instead of pulsing.
 */
export function CheckSteps({ state }: { state: PluginVersion["state"] }) {
  const paused = pluginFlags(useWorkspace().me).paused;
  const steps = [
    { key: "files", label: "Checking the files", done: state !== "uploaded" && state !== "inspecting", active: state === "uploaded" || state === "inspecting" },
    { key: "tests", label: "Running the tests", done: !isChecking({ state } as PluginVersion), active: state === "describing" },
  ];
  return (
    <div className="grid gap-2">
      {paused ? (
        <div data-testid="plugin-checks-paused">
          <InlineMessage tone="warning">
            <span className="font-semibold">{PAUSED_LABEL}.</span> {PAUSED_DESCRIPTION}
          </InlineMessage>
        </div>
      ) : null}
      <ol className="grid gap-2 sm:grid-cols-2" aria-label="Checks" data-testid="plugin-check-steps" data-paused={paused || undefined}>
        {steps.map((s, i) => (
          <li
            key={s.key}
            aria-current={s.active ? "step" : undefined}
            className={cn("flex items-center gap-3 rounded-xl border px-4 py-3 text-sm", s.active ? "border-primary bg-accent font-semibold" : s.done ? "bg-card" : "bg-card text-muted-foreground")}
          >
            <span
              aria-hidden
              className={cn(
                "grid size-6 flex-none place-items-center rounded-full text-xs font-semibold",
                s.done ? "bg-success/15 text-success" : s.active ? cn("bg-primary text-primary-foreground", !paused && "animate-pulse") : "bg-muted text-muted-foreground",
              )}
            >
              {s.done ? "✓" : i + 1}
            </span>
            {s.label}
            <span className="sr-only">{s.done ? "(done)" : s.active ? (paused ? "(paused)" : "(running)") : "(waiting)"}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}

function Decisions({ records, nameOf }: { records: readonly PluginDecisionRecord[]; nameOf: NameOf }) {
  if (!records.length) return null;
  return (
    <section aria-labelledby="plugin-decisions-title" className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-3 rounded-2xl border bg-card p-5" data-testid="plugin-decisions">
      <h3 id="plugin-decisions-title" className="font-display text-lg leading-tight font-semibold">
        Reviews
      </h3>
      <ul className="grid gap-3">
        {records.map((d, i) => (
          <li key={`${d.decided_at}:${i}`} className="grid gap-1 border-t pt-3 first:border-t-0 first:pt-0">
            <p className="text-sm">
              <span className={cn("font-semibold", d.decision === "approve" ? "text-success" : "text-attention")}>{d.decision === "approve" ? "Approved" : "Not approved"}</span> by{" "}
              {d.approver_name ? displayText(d.approver_name) : nameOf(d.approver_id)}{" "}
              <time className="text-muted-foreground" dateTime={d.decided_at} title={absoluteTime(d.decided_at)}>
                {relativeTime(d.decided_at)}
              </time>
            </p>
            {d.note ? <p className="text-sm whitespace-pre-line [overflow-wrap:anywhere] text-muted-foreground">{displayText(d.note)}</p> : null}
          </li>
        ))}
      </ul>
    </section>
  );
}

function Examples({ version }: { version: PluginVersion }) {
  const plots = version.exports_detail?.plots ?? [];
  if (!plots.length || !version.describe_digest) return null;
  return (
    <section aria-labelledby="plugin-examples-title" className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-3 overflow-hidden rounded-2xl border bg-card p-5">
      <div className="grid gap-1">
        <h3 id="plugin-examples-title" className="font-display text-lg leading-tight font-semibold">
          Chart examples
        </h3>
        <p className="text-sm text-muted-foreground">Drawn on toy data during the tests, in the same sandbox as every chart.</p>
      </div>
      <div className="grid min-w-0 gap-4 md:grid-cols-2">
        {plots.slice(0, 4).map((p) => (
          <figure key={p.id} className="grid min-w-0 gap-2 overflow-hidden">
            <Suspense fallback={<LoadingRows rows={3} />}>
              <ExamplePlot pluginId={version.plugin_id} versionId={version.id} plotId={p.id} title={displayText(p.title)} />
            </Suspense>
            <figcaption className="text-xs text-muted-foreground [overflow-wrap:anywhere]">{displayText(p.title)}</figcaption>
          </figure>
        ))}
      </div>
    </section>
  );
}

function Header({ plugin, version, uploader, onYank }: { plugin: Plugin; version: PluginVersion; uploader: string; onYank: (() => void) | null }) {
  const ws = useWorkspace();
  const tenantId = ws.current?.tenant.id ?? "";
  const request = useRequestApproval();
  const canAsk = canRequestApproval(version, ws.me, tenantId);
  return (
    <header className="grid gap-4 rounded-3xl bg-block p-6 text-block-foreground sm:p-8" data-testid="plugin-version-header">
      <div className="grid gap-2">
        <p className="font-mono text-xs text-block-muted [overflow-wrap:anywhere]">{plugin.namespace}</p>
        <h1 className="display-title text-[clamp(2rem,5vw,2.75rem)] leading-[1.05] [overflow-wrap:anywhere]">
          {displayText(plugin.title)} <span className="text-block-muted">{displayText(version.version)}</span>
        </h1>
        <div className="flex flex-wrap items-center gap-2">
          <span className="rounded-full bg-card p-0.5">
            <PluginStateChip state={version.state} />
          </span>
          <OnlyYouChip scope={version.scope} mine={version.uploaded_by === ws.user.id} className="bg-block-raised text-block-foreground" />
          <span className="text-sm text-block-muted">
            Uploaded by {uploader}{" "}
            <time dateTime={version.uploaded_at} title={absoluteTime(version.uploaded_at)}>
              {relativeTime(version.uploaded_at)}
            </time>
          </span>
        </div>
        <p className="max-w-[64ch] text-base text-block-muted">{stateDescription(version)}</p>
      </div>
      {canAsk || onYank ? (
        <div className="flex flex-wrap gap-2">
          {canAsk ? (
            <Button
              data-testid="plugin-request-approval"
              disabled={request.isPending}
              // the last two classes: the focus ring of the block (`BLOCK_RING` in features/library/ui.tsx)
              className="h-auto min-h-11 bg-block-foreground px-5 text-block hover:bg-block-foreground/85 active:bg-block-foreground [--card:var(--block)] [--ring:var(--block-foreground)]"
              onClick={() =>
                request.mutate(
                  { pluginId: version.plugin_id, versionId: version.id },
                  {
                    onSuccess: () => toast.success("Sent for review", "A workspace admin other than you looks at it next. You can keep using it yourself meanwhile."),
                    onError: (err) => toast.error("The app could not ask for a review", errorMessage(err)),
                  },
                )
              }
            >
              <Send /> Ask a reviewer to approve it for the team
            </Button>
          ) : null}
          {onYank ? (
            <Button data-testid="plugin-yank" variant="ghost" className="h-auto min-h-11 rounded-full bg-block-raised px-5 text-block-foreground hover:bg-block-raised/80 hover:text-block-foreground" onClick={onYank}>
              <Undo2 /> Withdraw this version
            </Button>
          ) : null}
        </div>
      ) : null}
    </header>
  );
}

export function VersionPage({ pluginId, versionId }: { pluginId: string; versionId: string }) {
  const ws = useWorkspace();
  const tenantId = ws.current?.tenant.id ?? "";
  const plugin = usePlugin(pluginId);
  const version = usePluginVersion(pluginId, versionId);
  const versions = usePluginVersions(pluginId);
  const nameOf = useNameOf(tenantId);
  const admins = useMembers(ws.mode === "session" && ws.current?.tenantRole === "admin" ? tenantId : "", null);
  const [yankOpen, setYankOpen] = useState(false);
  const v = version.data;
  useRefreshOnStateChange(v);
  // The last approved version other than this one (the reviewer's diff).
  const previousSummary = useMemo(
    () => (versions.data ?? []).filter((x) => x.id !== versionId && x.state === "approved").sort((a, b) => b.uploaded_at.localeCompare(a.uploaded_at))[0] ?? null,
    [versions.data, versionId],
  );
  const previous = usePluginVersion(v?.state === "awaiting_approval" && previousSummary ? pluginId : undefined, previousSummary?.id);

  if (plugin.isPending || version.isPending) return <LoadingRows rows={6} />;
  const missing = (e: unknown) => e instanceof ApiError && e.status === 404;
  if (missing(plugin.error) || missing(version.error)) return <VersionNotFound />;
  if (plugin.isError) return <ErrorNotice error={plugin.error} title="The app could not load this plugin" onRetry={() => void plugin.refetch()} />;
  if (version.isError || !v) return <ErrorNotice error={version.error} title="The app could not load this version" onRetry={() => void version.refetch()} />;
  const p = plugin.data;
  const uploader = nameOf(v.uploaded_by);
  const problems = v.inspection?.problems ?? [];
  const adminCount = (admins.data?.items ?? []).filter((m) => m.role === "admin" && !m.project_id).length;
  const oneAdmin = admins.data && (v.state === "kit_passed" || v.state === "awaiting_approval") ? singleAdminNote(adminCount) : null;

  return (
    <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-6" data-testid="plugin-version-page" data-state={v.state}>
      <DevBanner />
      <Breadcrumbs
        crumbs={[
          { label: "Catalog", to: "/catalog" }, { label: "Yours", to: "/catalog", search: { tab: "yours" } },
          { label: displayText(p.title), to: "/plugins/$pluginId", params: { pluginId: p.id } },
          { label: `Version ${displayText(v.version)}` },
        ]}
      />
      <Header plugin={p} version={v} uploader={uploader} onYank={canYank(v, ws.me, tenantId) ? () => setYankOpen(true) : null} />
      {oneAdmin ? <InlineMessage tone="info">{oneAdmin}</InlineMessage> : null}
      {v.problem ? <ProblemNotice problem={v.problem} /> : null}
      {v.state === "yanked" || v.state === "purged" ? (
        <InlineMessage tone="warning" role="none">
          {v.yank_reason ? YANK_REASON[v.yank_reason] ?? "Withdrawn" : "Removed"}
          {v.yank_note ? <span className="block whitespace-pre-line [overflow-wrap:anywhere] text-muted-foreground">{displayText(v.yank_note)}</span> : null}
        </InlineMessage>
      ) : null}
      {v.purge_after && v.state !== "purged" ? (
        <p className="text-sm text-muted-foreground">
          We keep the file until <time dateTime={v.purge_after}>{absoluteTime(v.purge_after)}</time>, then remove it. This record stays.
        </p>
      ) : null}
      {isChecking(v) ? <CheckSteps state={v.state} /> : null}
      {v.state === "awaiting_approval" ? (
        <ReviewerPanel
          version={v}
          namespace={p.namespace}
          tenantId={tenantId}
          uploader={uploader}
          previous={previousSummary ? { version: previousSummary.version, exports: previous.data?.exports_detail } : null}
          previousStatus={{
            loading: versions.isPending || (Boolean(previousSummary) && previous.isPending),
            error: versions.error ?? (previousSummary ? previous.error : null),
            onRetry: () => void (versions.isError ? versions.refetch() : previous.refetch()),
          }}
          onReload={() => void version.refetch()}
        />
      ) : (
        <>
          <ProblemsList problems={problems} />
          <KitReport report={v.kit} />
          <ExportsList exports={v.exports_detail} uploader={uploader} />
          <Examples version={v} />
          <SbomSummary sbom={v.inspection?.sbom} />
          {v.inspection ? <InspectFacts inspection={v.inspection} namespace={p.namespace} /> : null}
          {(v.urls ?? []).length ? <UrlList urls={v.urls ?? []} /> : null}
        </>
      )}
      {v.state === "awaiting_approval" ? <Examples version={v} /> : null}
      <Decisions records={v.approvals ?? []} nameOf={nameOf} />
      {(v.issues ?? []).length ? (
        <section className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-2 rounded-2xl border bg-card p-5" aria-label="Issues">
          <h3 className="font-display text-lg font-semibold">Issues found in the description</h3>
          <ul className="grid gap-1 text-sm">
            {(v.issues ?? []).map((i, n) => (
              <li key={`${i.code}:${n}`} className="grid min-w-0 gap-0.5" data-testid="plugin-issue">
                <span className={cn(i.severity === "error" ? "text-destructive" : "text-muted-foreground")}>{displayText(i.message)}</span>
                {/* where it is, and the uploaded text itself (as text, normalized), as the CLI shows them */}
                {i.pointer ? <code className="font-mono text-xs [overflow-wrap:anywhere] text-muted-foreground">{displayText(i.pointer)}</code> : null}
                {i.untrusted_text ? <span className="text-xs [overflow-wrap:anywhere] text-muted-foreground">{displayText(i.untrusted_text)}</span> : null}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      {yankOpen ? <YankDialog version={v} open={yankOpen} onOpenChange={setYankOpen} /> : null}
    </div>
  );
}

/** The version page when the plugin or version id is not one the caller can see. */
export function VersionNotFound() {
  return (
    <EmptyState title="We can't find this upload" action={<Link to="/catalog" search={{ tab: "yours" }} className="link">Back to Catalog › Yours</Link>}>
      It may be private to the person who uploaded it, or the link is out of date.
    </EmptyState>
  );
}
