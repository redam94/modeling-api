/**
 * Saved plots, static renders and embed links. A saved plot keeps a model version's plot (or
 * one you wrote) under a name; it can be downloaded as PNG, SVG or PDF (rendered in the
 * sandbox) and - for platform recipes only - shared by a tokenised, expiring, read-only link.
 */
import { BarChart3, Download, ExternalLink, Link2, Plus, Share2, Trash2 } from "lucide-react";
import { useState, type FormEvent } from "react";

import { can } from "@/api/access";
import { ApiError, errorMessage, newIdempotencyKey } from "@/api/client";
import { getPlotRender, startPlotRender, useCreateEmbed, useCreateSavedPlot, useDeleteSavedPlot, usePlotEmbeds, useRevokeEmbed, useSavedPlots } from "@/api/integrations";
import { useModels, useModelVersions, usePlots, useProjectScope } from "@/api/queries";
import type { PlotEmbed, PlotEmbedCreated, PlotRender, SavedPlot } from "@/api/types";
import { EmptyState, ErrorNotice, Mono, QueryView, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Checkbox, Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from "@/components/ui/overlays";
import { Badge, Card, CardContent, CardDescription, CardHeader, CardTitle, Field, Input, NativeSelect, TBody, TD, TH, THead, TR, Table, Textarea } from "@/components/ui/primitives";
import { useWorkspace } from "@/features/auth/workspace";
import { fetchArtifactBytes } from "@/lib/artifacts";
import { saveBytes } from "@/lib/download";
import { absoluteTime, relativeTime } from "@/lib/format";
import { iframeSnippet, parseAllowedOrigins } from "@/lib/integrations";
import { isTerminal } from "@/lib/run-state";
import { toast } from "@/stores/toast";

import { CodeBlock } from "./endpoint-schema";
import { ShownOnceDialog } from "./shown-once";

const FORMATS = ["png", "svg", "pdf"] as const;
type Format = (typeof FORMATS)[number];
const MAX_RENDER_BYTES = 32 * 1024 * 1024;
const RENDER_POLL_MS = 1500;
const RENDER_TIMEOUT_MS = 3 * 60_000;
export const EXPIRY_DAYS = [7, 30, 90, 180, 365] as const;

const NOT_EMBEDDABLE = "Only plots of the platform's own recipes can be shared by link: a shared view must never run code a plugin or a person wrote.";

function fileStem(title: string): string {
  return title.replace(/[^A-Za-z0-9_.-]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 80) || "plot";
}

const sleep = (ms: number, signal?: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    const t = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => {
      clearTimeout(t);
      reject(new DOMException("aborted", "AbortError"));
    });
  });

// ----------------------------------------------------------------------- render

/**
 * Download buttons for PNG, SVG and PDF. A render that takes longer than the wait comes back
 * `done: false` and is polled; the file is checked against its CRC32C before it is saved.
 * When the server refuses Plotly (`MODE_NOT_SUPPORTED`), Plotly downloads are disabled with
 * the reason.
 */
export function RenderButtons({ plot, plotlyRefusal, onPlotlyRefused }: { plot: SavedPlot; plotlyRefusal: string | null; onPlotlyRefused: (reason: string) => void }) {
  const scope = useProjectScope();
  const ws = useWorkspace();
  const canRender = can(ws.grants, "run", "plot");
  const [busy, setBusy] = useState<Format | null>(null);
  const [error, setError] = useState<unknown>(null);
  const refused = plot.kind === "plotly" ? plotlyRefusal : null;

  const download = async (format: Format) => {
    setBusy(format);
    setError(null);
    try {
      let r: PlotRender = await startPlotRender(scope, { saved_plot_id: plot.id, format }, { key: newIdempotencyKey(), waitSeconds: 20 });
      const started = Date.now();
      while (!r.done) {
        if (isTerminal(r.run.state) && r.run.state !== "succeeded") break;
        if (Date.now() - started > RENDER_TIMEOUT_MS) throw new Error("The render is taking longer than expected; it keeps running, open the Runs page to follow it.");
        await sleep(RENDER_POLL_MS);
        r = await getPlotRender(scope, r.run.id);
      }
      if (r.run.state !== "succeeded") {
        throw new Error(r.run.problem ? `${r.run.problem.title}${r.run.problem.detail ? `: ${r.run.problem.detail}` : ""}` : `The render run ended ${r.run.state}.`);
      }
      const link = r.outputs?.find((o) => o.name.endsWith(`.${format}`)) ?? r.outputs?.[0];
      if (!link) throw new Error("The render finished without a file.");
      const bytes = await fetchArtifactBytes(link, { maxBytes: MAX_RENDER_BYTES });
      saveBytes(bytes, `${fileStem(plot.title)}.${format}`, link.content_type ?? "application/octet-stream");
      toast.success("Downloaded", `${plot.title} (${format.toUpperCase()})`);
    } catch (e) {
      if (e instanceof ApiError && e.code === "MODE_NOT_SUPPORTED" && plot.kind === "plotly") {
        onPlotlyRefused(e.problem?.detail ?? "This server does not render Plotly figures.");
      } else {
        setError(e);
      }
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="grid gap-2">
      <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label={`Download ${plot.title}`}>
        {FORMATS.map((f) => (
          <Button
            key={f}
            size="sm"
            variant="outline"
            disabled={!canRender || busy !== null || refused !== null}
            onClick={() => void download(f)}
            aria-label={`Download ${f.toUpperCase()}`}
            title={!canRender ? "Rendering starts a run: it needs the runner role" : (refused ?? undefined)}
          >
            {busy === f ? <Spinner /> : <Download />} {f.toUpperCase()}
          </Button>
        ))}
      </div>
      {refused ? (
        <p className="text-xs text-muted-foreground" data-testid="plotly-refused">
          Static Plotly downloads are off on this server: {refused} Open the plot and use the sandbox&apos;s own PNG export instead.
        </p>
      ) : null}
      {error ? <ErrorNotice error={error} title="The plot could not be rendered" /> : null}
    </div>
  );
}

// ----------------------------------------------------------------------- embeds

const EMBED_STATUS = { active: "success", expired: "outline", revoked: "danger" } as const;

export function EmbedsTable({ plot }: { plot: SavedPlot }) {
  const ws = useWorkspace();
  const canRevoke = can(ws.grants, "delete", "embed");
  const embeds = usePlotEmbeds(plot.id);
  const revoke = useRevokeEmbed(plot.id);
  const [confirm, setConfirm] = useState<PlotEmbed | null>(null);
  return (
    <QueryView query={embeds}>
      {(page) =>
        page.items.length ? (
          <>
            <Table aria-label={`Embed links of ${plot.title}`}>
              <THead>
                <TR>
                  <TH>Link</TH>
                  <TH>Status</TH>
                  <TH>Expires</TH>
                  <TH>Views</TH>
                  <TH>Sites</TH>
                  <TH>
                    <span className="sr-only">Actions</span>
                  </TH>
                </TR>
              </THead>
              <TBody>
                {page.items.map((e) => (
                  <TR key={e.id} data-status={e.status}>
                    <TD>
                      <span className="font-medium">{e.label ?? "Unnamed link"}</span>
                      <Mono className="block text-muted-foreground">{e.display}</Mono>
                    </TD>
                    <TD>
                      <Badge variant={EMBED_STATUS[e.status]}>{e.status}</Badge>
                    </TD>
                    <TD title={absoluteTime(e.expires_at)}>{e.status === "revoked" ? `revoked ${relativeTime(e.revoked_at)}` : relativeTime(e.expires_at)}</TD>
                    <TD className="tabular">
                      {e.views ?? 0}
                      {e.last_viewed_at ? <span className="block text-xs text-muted-foreground">last {relativeTime(e.last_viewed_at)}</span> : null}
                    </TD>
                    <TD className="text-xs">{e.allowed_origins?.length ? e.allowed_origins.join(", ") : <span className="text-muted-foreground">any site</span>}</TD>
                    <TD className="text-right">
                      {e.status === "active" ? (
                        <Button variant="ghost" size="sm" disabled={!canRevoke} onClick={() => setConfirm(e)} aria-label={`Revoke ${e.label ?? e.display}`}>
                          <Trash2 /> Revoke
                        </Button>
                      ) : null}
                    </TD>
                  </TR>
                ))}
              </TBody>
            </Table>
            {confirm ? (
              <Dialog open onOpenChange={(o) => !o && setConfirm(null)}>
                <DialogContent>
                  <DialogTitle>Revoke “{confirm.label ?? confirm.display}”?</DialogTitle>
                  <DialogDescription>Everyone with the link sees “This link has expired” from the next view. This cannot be undone; create a new link to share again.</DialogDescription>
                  {revoke.isError ? <ErrorNotice error={revoke.error} /> : null}
                  <DialogFooter>
                    <Button variant="ghost" onClick={() => setConfirm(null)}>
                      Keep it
                    </Button>
                    <Button
                      variant="destructive"
                      disabled={revoke.isPending}
                      onClick={() =>
                        revoke.mutate(confirm.id, {
                          onSuccess: () => {
                            toast.success("Link revoked", confirm.label ?? confirm.display);
                            setConfirm(null);
                          },
                        })
                      }
                    >
                      <Trash2 /> Revoke link
                    </Button>
                  </DialogFooter>
                </DialogContent>
              </Dialog>
            ) : null}
          </>
        ) : (
          <p className="text-[13px] text-muted-foreground">No links yet.</p>
        )
      }
    </QueryView>
  );
}

export function CreateEmbedDialog({ plot, onClose, onCreated }: { plot: SavedPlot; onClose: () => void; onCreated: (c: PlotEmbedCreated) => void }) {
  const create = useCreateEmbed(plot.id);
  const [label, setLabel] = useState("");
  const [days, setDays] = useState<number>(30);
  const [origins, setOrigins] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const parsed = parseAllowedOrigins(origins);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    if (parsed.error) return;
    create.mutate({ expires_in_days: days, allowed_origins: parsed.origins, ...(label.trim() ? { label: label.trim() } : {}) }, { onSuccess: onCreated });
  };
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent wide>
        <DialogTitle>Share “{plot.title}” by link</DialogTitle>
        <DialogDescription>Anyone with the link sees this plot - read-only, no sign-in - until it expires or you revoke it. Paste it into a BI tool, Slack or Notion.</DialogDescription>
        <form className="grid gap-5" onSubmit={submit} noValidate>
          <div className="grid gap-4 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
            <Field label="Label (optional)" htmlFor="emb-label" hint="Who the link is for; only you see it.">
              <Input id="emb-label" autoFocus maxLength={200} placeholder="Q4 board deck" value={label} onChange={(e) => setLabel(e.target.value)} />
            </Field>
            <Field label="Expires after" htmlFor="emb-exp">
              <NativeSelect id="emb-exp" value={days} onChange={(e) => setDays(Number(e.target.value))}>
                {EXPIRY_DAYS.map((d) => (
                  <option key={d} value={d}>
                    {d} days{d === 30 ? " (default)" : ""}
                  </option>
                ))}
              </NativeSelect>
            </Field>
          </div>
          <Field
            label="Sites that may frame it (optional)"
            htmlFor="emb-origins"
            error={submitted ? parsed.error : null}
            hint="https origins, one per line (up to 10), such as https://acme.notion.site. Empty: any site may frame the link."
          >
            <Textarea id="emb-origins" className="min-h-16 font-mono text-xs" value={origins} onChange={(e) => setOrigins(e.target.value)} />
          </Field>
          {create.isError ? <ErrorNotice error={create.error} title="The link was not created" /> : null}
          <DialogFooter>
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={create.isPending}>
              {create.isPending ? <Spinner /> : <Link2 />} Create link
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function EmbedCreatedDialog({ created, title, onClose }: { created: PlotEmbedCreated; title: string; onClose: () => void }) {
  return (
    <ShownOnceDialog
      title="Copy the link now"
      description={
        <>
          The link carries its own token, so it is shown only this once. It works until {absoluteTime(created.embed.expires_at)}
          {created.embed.allowed_origins?.length ? `, framed only by ${created.embed.allowed_origins.join(", ")}` : ""}.
        </>
      }
      label="Embed link"
      value={created.url}
      copyLabel="Copy embed link"
      acknowledge="I have copied the link"
      onClose={onClose}
    >
      <div className="grid gap-2">
        <p className="text-[13px] font-medium">Embed code</p>
        <CodeBlock code={iframeSnippet(created.url, title)} label="Embed code" language="html" wrap />
        <a href={created.url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-[13px] text-primary hover:underline">
          <ExternalLink className="size-3.5" /> Open the link in a new tab
        </a>
      </div>
    </ShownOnceDialog>
  );
}

// ----------------------------------------------------------------------- save a model plot

export interface ModelPlotPick {
  model?: string | undefined;
  version?: number | undefined;
  plot?: string | undefined;
}

/** Save a model version's plot under a name (and optionally go straight on to its link). */
export function SavePlotDialog({ initial, onClose, onSaved }: { initial?: ModelPlotPick; onClose: () => void; onSaved: (plot: SavedPlot, share: boolean) => void }) {
  const models = useModels();
  const [modelId, setModelId] = useState(initial?.model ?? "");
  const model = modelId || models.data?.items[0]?.id || "";
  const versions = useModelVersions(model || undefined);
  const [version, setVersion] = useState<number | null>(initial?.version ?? null);
  const latest = versions.data?.items.reduce((m, v) => Math.max(m, v.version), 0) || 0;
  const v = version ?? latest;
  const plots = usePlots(model, v);
  const [plotId, setPlotId] = useState(initial?.plot ?? "");
  const items = model && v ? (plots.data?.items ?? []) : [];
  const chosen = items.find((p) => p.id === plotId) ?? items[0];
  const [title, setTitle] = useState("");
  const [share, setShare] = useState(Boolean(initial?.plot));
  const create = useCreateSavedPlot();
  const effectiveTitle = title.trim() || (chosen ? `${chosen.title} (v${v})` : "");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!chosen || !model || !v) return;
    create.mutate({ title: effectiveTitle.slice(0, 300), source: { model_id: model, version: v, plot_id: chosen.id } }, { onSuccess: (saved) => onSaved(saved, share) });
  };
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent wide>
        <DialogTitle>Save a model plot</DialogTitle>
        <DialogDescription>The saved plot keeps pointing at this model version, so a link to it never changes when the model is refitted.</DialogDescription>
        <form className="grid gap-4" onSubmit={submit} noValidate>
          <div className="grid gap-4 sm:grid-cols-3">
            <Field label="Model" htmlFor="sp-model">
              <NativeSelect
                id="sp-model"
                value={model}
                onChange={(e) => {
                  setModelId(e.target.value);
                  setVersion(null);
                  setPlotId("");
                }}
              >
                {models.data?.items.length ? null : <option value="">No models yet</option>}
                {models.data?.items.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.name}
                  </option>
                ))}
              </NativeSelect>
            </Field>
            <Field label="Version" htmlFor="sp-version">
              <NativeSelect
                id="sp-version"
                value={v || ""}
                onChange={(e) => {
                  setVersion(Number(e.target.value));
                  setPlotId("");
                }}
              >
                {versions.data?.items.map((x) => (
                  <option key={x.id} value={x.version}>
                    v{x.version}
                  </option>
                ))}
              </NativeSelect>
            </Field>
            <Field label="Plot" htmlFor="sp-plot">
              <NativeSelect id="sp-plot" value={chosen?.id ?? ""} onChange={(e) => setPlotId(e.target.value)} disabled={!items.length}>
                {items.length ? null : <option value="">{plots.isPending ? "Loading…" : "No plots"}</option>}
                {items.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.title}
                  </option>
                ))}
              </NativeSelect>
            </Field>
          </div>
          <Field label="Title" htmlFor="sp-title" hint="Shown above the plot wherever it is shared.">
            <Input id="sp-title" maxLength={300} placeholder={effectiveTitle || "Response curves for the board"} value={title} onChange={(e) => setTitle(e.target.value)} />
          </Field>
          {chosen && !chosen.recipe ? <p className="text-xs text-warning">{NOT_EMBEDDABLE}</p> : null}
          <label className="flex items-center gap-2 text-[13px]">
            <Checkbox checked={share} disabled={Boolean(chosen && !chosen.recipe)} onCheckedChange={(x) => setShare(x === true)} />
            Create a share link next
          </label>
          {create.isError ? <ErrorNotice error={create.error} title="The plot was not saved" /> : null}
          <DialogFooter>
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={!chosen || create.isPending}>
              {create.isPending ? <Spinner /> : <Plus />} Save plot
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// ----------------------------------------------------------------------- section

/** A model's name from the project's models (its id while unknown, e.g. a deleted model). */
function ModelName({ id }: { id: string }) {
  const models = useModels();
  const name = models.data?.items.find((m) => m.id === id)?.name;
  return name ? (
    <span title={id} className="font-medium">
      {name}
    </span>
  ) : (
    <Mono>{id}</Mono>
  );
}

function SavedPlotCard({ plot, plotlyRefusal, onPlotlyRefused }: { plot: SavedPlot; plotlyRefusal: string | null; onPlotlyRefused: (r: string) => void }) {
  const ws = useWorkspace();
  const canEmbed = can(ws.grants, "create", "embed");
  const canDelete = can(ws.grants, "delete", "plot");
  const del = useDeleteSavedPlot();
  const [sharing, setSharing] = useState(false);
  const [created, setCreated] = useState<PlotEmbedCreated | null>(null);
  const [deleting, setDeleting] = useState(false);
  return (
    <Card data-testid="saved-plot" className="min-w-0">
      <CardHeader className="flex-wrap">
        <div className="min-w-0">
          <CardTitle>{plot.title}</CardTitle>
          <CardDescription className="break-all">
            {plot.source ? (
              <>
                <Mono>{plot.source.plot_id}</Mono> of <ModelName id={plot.source.model_id} /> v{plot.source.version}
              </>
            ) : (
              "Written by hand"
            )}{" "}
            · saved {relativeTime(plot.created_at)}
          </CardDescription>
        </div>
        <div className="flex flex-wrap gap-1">
          <Badge variant="outline">{plot.kind}</Badge>
          <Badge variant={plot.provenance === "recipe" ? "secondary" : "warning"} title={plot.recipe ?? undefined}>
            {plot.provenance === "recipe" ? "platform recipe" : plot.provenance === "plugin" ? "plugin plot" : "hand-written"}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="grid gap-4">
        <div className="grid gap-1">
          <p className="text-xs font-medium text-muted-foreground">Download</p>
          <RenderButtons plot={plot} plotlyRefusal={plotlyRefusal} onPlotlyRefused={onPlotlyRefused} />
        </div>
        <div className="grid gap-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-xs font-medium text-muted-foreground">Share links</p>
            <div className="flex gap-1">
              <Button size="sm" variant="outline" disabled={!plot.embeddable || !canEmbed} title={plot.embeddable ? undefined : NOT_EMBEDDABLE} onClick={() => setSharing(true)}>
                <Share2 /> Create link
              </Button>
              <Button size="sm" variant="ghost" disabled={!canDelete} onClick={() => setDeleting(true)} aria-label={`Delete ${plot.title}`}>
                <Trash2 />
              </Button>
            </div>
          </div>
          {plot.embeddable ? <EmbedsTable plot={plot} /> : <p className="text-xs text-muted-foreground">{NOT_EMBEDDABLE}</p>}
        </div>
      </CardContent>
      {sharing ? (
        <CreateEmbedDialog
          plot={plot}
          onClose={() => setSharing(false)}
          onCreated={(c) => {
            setSharing(false);
            setCreated(c);
          }}
        />
      ) : null}
      {created ? <EmbedCreatedDialog created={created} title={plot.title} onClose={() => setCreated(null)} /> : null}
      {deleting ? (
        <Dialog open onOpenChange={(o) => !o && setDeleting(false)}>
          <DialogContent>
            <DialogTitle>Delete “{plot.title}”?</DialogTitle>
            <DialogDescription>Its share links stop working at once. The model version and its plots are kept.</DialogDescription>
            {del.isError ? <ErrorNotice error={del.error} /> : null}
            <DialogFooter>
              <Button variant="ghost" onClick={() => setDeleting(false)}>
                Keep it
              </Button>
              <Button
                variant="destructive"
                disabled={del.isPending}
                onClick={() =>
                  del.mutate(plot.id, {
                    onSuccess: () => toast.success("Plot deleted", plot.title),
                    onError: (e) => toast.error("Delete failed", errorMessage(e)),
                  })
                }
              >
                <Trash2 /> Delete plot
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      ) : null}
    </Card>
  );
}

export function SharingSection({ initial, onConsumeInitial }: { initial?: ModelPlotPick | undefined; onConsumeInitial?: () => void }) {
  const ws = useWorkspace();
  const canSave = can(ws.grants, "create", "plot");
  const saved = useSavedPlots();
  const [saving, setSaving] = useState(Boolean(initial?.plot));
  const [shareNext, setShareNext] = useState<SavedPlot | null>(null);
  const [created, setCreated] = useState<{ c: PlotEmbedCreated; title: string } | null>(null);
  const [plotlyRefusal, setPlotlyRefusal] = useState<string | null>(null);
  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <p className="max-w-[68ch] text-muted-foreground">
          Save the plots you report on, download them for slides (PNG, SVG or PDF, rendered in the sandbox) or share them read-only by an expiring link.
        </p>
        <Button onClick={() => setSaving(true)} disabled={!canSave}>
          <Plus /> Save a model plot
        </Button>
      </div>
      <QueryView query={saved}>
        {(page) =>
          page.items.length ? (
            <div className="grid gap-4">
              {page.items.map((p) => (
                <SavedPlotCard key={p.id} plot={p} plotlyRefusal={plotlyRefusal} onPlotlyRefused={setPlotlyRefusal} />
              ))}
            </div>
          ) : (
            <EmptyState icon={<BarChart3 />} title="No saved plots yet" action={canSave ? <Button onClick={() => setSaving(true)}>Save a model plot</Button> : null}>
              Save a plot from a fitted model to download it or share it by link.
            </EmptyState>
          )
        }
      </QueryView>
      {saving ? (
        <SavePlotDialog
          {...(initial ? { initial } : {})}
          onClose={() => {
            setSaving(false);
            onConsumeInitial?.();
          }}
          onSaved={(plot, share) => {
            setSaving(false);
            onConsumeInitial?.();
            toast.success("Plot saved", plot.title);
            if (share && plot.embeddable) setShareNext(plot);
          }}
        />
      ) : null}
      {shareNext ? (
        <CreateEmbedDialog
          plot={shareNext}
          onClose={() => setShareNext(null)}
          onCreated={(c) => {
            setCreated({ c, title: shareNext.title });
            setShareNext(null);
          }}
        />
      ) : null}
      {created ? <EmbedCreatedDialog created={created.c} title={created.title} onClose={() => setCreated(null)} /> : null}
    </div>
  );
}
