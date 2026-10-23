/**
 * Catalog › Yours › Uploaded (D20): the plugins your workspace uploaded, one card each with the
 * newest version's state chip, "Only you can use this" on a private one, and who uploaded it.
 * Loaded lazily by the Yours tab (the feature is signed-in only). Titles are untrusted text.
 */
import { Link } from "@tanstack/react-router";
import { ArrowRight, Upload } from "lucide-react";

import { ErrorNotice, LoadingRows } from "@/components/common";
import { Button } from "@/components/ui/button";
import { useWorkspace } from "@/features/auth/workspace";
import { relativeTime } from "@/lib/format";

import { usePlugins } from "./api";
import { DevBanner } from "./dev-banner";
import { UploadedByChip } from "./exports-list";
import { useNameOf } from "./people";
import { uploadsAvailable } from "./uploaded-count";
import { OnlyYouChip, PluginStateChip } from "./state-chip";
import { displayText, exportsSummary } from "./states";

export default function UploadedSection() {
  const ws = useWorkspace();
  const on = uploadsAvailable(ws);
  const plugins = usePlugins({}, on);
  const nameOf = useNameOf(ws.current?.tenant.id ?? "");
  if (!on) return null;
  const list = plugins.data ?? [];
  return (
    <section aria-labelledby="yours-uploaded" className="grid gap-3" data-testid="uploaded-section">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="grid gap-1">
          <h3 id="yours-uploaded" className="text-xl leading-tight font-bold">
            Uploaded
            {list.length ? <span className="ml-2 align-middle text-xs font-semibold text-muted-foreground"> {list.length}</span> : null}
          </h3>
          <p className="max-w-[62ch] text-sm text-muted-foreground">
            Python your team uploaded. Each version is checked and tested in a sandbox; you can use your own right away, and everyone can once a reviewer approves it.
          </p>
        </div>
        <Button size="sm" className="h-auto min-h-9 rounded-full px-4 text-xs" asChild>
          <Link to="/plugins/new">
            <Upload /> Upload your own code
          </Link>
        </Button>
      </div>
      <DevBanner />
      {plugins.isPending ? (
        <LoadingRows rows={2} />
      ) : plugins.isError ? (
        <ErrorNotice error={plugins.error} title="The app cannot load the uploads" onRetry={() => void plugins.refetch()} />
      ) : list.length === 0 ? (
        <p className="rounded-2xl border border-dashed border-border-strong p-5 text-sm text-muted-foreground">
          Nothing uploaded yet. Got a model or a chart in Python?{" "}
          <Link to="/plugins/new" className="font-semibold text-primary hover:underline">
            Upload your own code
          </Link>{" "}
          and try it out on your data in a few minutes.
        </p>
      ) : (
        <ul className="grid gap-3 md:grid-cols-2" aria-label="Uploaded plugins">
          {list.map((p) => {
            const v = p.latest_version;
            return (
              <li key={p.id} data-testid="uploaded-card" data-plugin={p.short_name} className="grid content-start gap-2 rounded-2xl border bg-card p-5">
                <Link to="/plugins/$pluginId" params={{ pluginId: p.id }} className="w-fit font-display text-lg leading-snug font-bold [overflow-wrap:anywhere] hover:underline">
                  {displayText(p.title)}
                </Link>
                <p className="font-mono text-xs text-muted-foreground [overflow-wrap:anywhere]">{p.namespace}</p>
                {v ? (
                  <>
                    <p className="text-sm text-muted-foreground">
                      Version <span className="font-semibold text-foreground">{displayText(v.version)}</span> · {relativeTime(v.uploaded_at)} · adds {exportsSummary(v)}
                    </p>
                    <div className="flex flex-wrap gap-1.5">
                      <PluginStateChip state={v.state} />
                      <OnlyYouChip scope={v.scope} mine={v.uploaded_by === ws.user.id} />
                      <UploadedByChip name={nameOf(v.uploaded_by)} />
                    </div>
                  </>
                ) : (
                  <p className="text-sm text-muted-foreground">No version uploaded yet.</p>
                )}
                <div className="flex flex-wrap gap-2 pt-1">
                  <Button size="sm" variant="ghost" className="h-auto min-h-9 rounded-full bg-muted px-4 text-xs text-foreground hover:bg-surface-hover" asChild>
                    <Link to="/plugins/$pluginId" params={{ pluginId: p.id }} aria-label={`Versions of ${displayText(p.title)}`}>
                      Versions ({p.versions}) <ArrowRight />
                    </Link>
                  </Button>
                  {v ? (
                    <Button size="sm" variant="ghost" className="h-auto min-h-9 rounded-full bg-muted px-4 text-xs text-foreground hover:bg-surface-hover" asChild>
                      <Link to="/plugins/$pluginId/versions/$versionId" params={{ pluginId: p.id, versionId: v.id }} aria-label={`Open version ${v.version} of ${displayText(p.title)}`}>
                        Open {displayText(v.version)}
                      </Link>
                    </Button>
                  ) : null}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
