/**
 * One uploaded plugin (`/plugins/:pluginId`, D20): its namespace and every version the caller
 * may see, newest first, with the state chip, "Only you can use this" on private ones, what
 * each adds, and "Upload a new version".
 */
import { Link } from "@tanstack/react-router";
import { ArrowRight, Upload } from "lucide-react";

import { ErrorNotice, LoadingRows, Mono, PageHeader } from "@/components/common";
import { Button } from "@/components/ui/button";
import { useWorkspace } from "@/features/auth/workspace";
import { absoluteTime, relativeTime } from "@/lib/format";

import { usePlugin, usePluginVersions } from "./api";
import { DevBanner } from "./dev-banner";
import { useNameOf } from "./people";
import { OnlyYouChip, PluginStateChip } from "./state-chip";
import { displayText, exportsSummary } from "./states";

export function PluginPage({ pluginId }: { pluginId: string }) {
  const ws = useWorkspace();
  const plugin = usePlugin(pluginId);
  const versions = usePluginVersions(pluginId, true);
  const nameOf = useNameOf(ws.current?.tenant.id ?? "");
  if (plugin.isPending) return <LoadingRows rows={5} />;
  if (plugin.isError) return <ErrorNotice error={plugin.error} title="The app could not load this plugin" onRetry={() => void plugin.refetch()} />;
  const p = plugin.data;
  return (
    <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-6">
      <PageHeader
        eyebrow={
          <Link to="/catalog" search={{ tab: "yours" }} className="hover:underline">
            Catalog › Yours › Uploaded
          </Link>
        }
        title={<span className="[overflow-wrap:anywhere]">{displayText(p.title)}</span>}
        description={<span>Uploaded code in your workspace. Every version is checked on its own; the team uses a version only after a reviewer approves it.</span>}
        meta={
          <>
            <Mono>{p.namespace}</Mono>
            <span>
              Started by {nameOf(p.created_by)} {relativeTime(p.created_at)}
            </span>
          </>
        }
        actions={
          <Button className="h-auto min-h-11 rounded-full px-5" asChild>
            <Link to="/plugins/new" search={{ plugin: p.id }}>
              <Upload /> Upload a new version
            </Link>
          </Button>
        }
      />
      <DevBanner />
      <section aria-labelledby="plugin-versions-title" className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-3">
        <h2 id="plugin-versions-title" className="text-xl font-bold">
          Versions <span className="text-xs font-semibold text-muted-foreground">{versions.data?.length ?? ""}</span>
        </h2>
        {versions.isPending ? (
          <LoadingRows rows={4} />
        ) : versions.isError ? (
          <ErrorNotice error={versions.error} title="The app could not load the versions" onRetry={() => void versions.refetch()} />
        ) : versions.data.length === 0 ? (
          <p className="rounded-2xl border border-dashed border-border-strong p-5 text-sm text-muted-foreground">No versions yet. Upload the first one.</p>
        ) : (
          <ul className="rounded-2xl border bg-card px-5" aria-label="Versions">
            {versions.data.map((v) => (
              <li key={v.id} className="grid grid-cols-[minmax(0,1fr)] items-center gap-3 border-b py-4 last:border-b-0 sm:grid-cols-[minmax(0,1fr)_auto]" data-version={v.version}>
                <div className="grid min-w-0 gap-1">
                  <Link
                    to="/plugins/$pluginId/versions/$versionId"
                    params={{ pluginId: p.id, versionId: v.id }}
                    className="w-fit font-display text-lg leading-snug font-bold [overflow-wrap:anywhere] hover:underline"
                  >
                    {displayText(v.version)}
                  </Link>
                  <p className="text-xs text-muted-foreground">
                    Uploaded by {nameOf(v.uploaded_by)}{" "}
                    <time dateTime={v.uploaded_at} title={absoluteTime(v.uploaded_at)}>
                      {relativeTime(v.uploaded_at)}
                    </time>{" "}
                    · adds {exportsSummary(v)}
                  </p>
                  <div className="flex flex-wrap gap-1.5">
                    <PluginStateChip state={v.state} />
                    <OnlyYouChip scope={v.scope} mine={v.uploaded_by === ws.user.id} />
                  </div>
                </div>
                <Button size="sm" variant="ghost" className="h-auto min-h-9 w-fit rounded-full bg-muted px-4 text-xs text-foreground hover:bg-surface-hover" asChild>
                  <Link to="/plugins/$pluginId/versions/$versionId" params={{ pluginId: p.id, versionId: v.id }} aria-label={`Open version ${v.version}`}>
                    Open <ArrowRight />
                  </Link>
                </Button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
