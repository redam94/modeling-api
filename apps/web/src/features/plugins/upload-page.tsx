/**
 * Upload your own code (`/plugins/new`, D20): pick a plugin (or start one: a short name gives
 * the namespace `t_<code>_<short>`), drop a wheel or copy the CLI command, and watch the checks:
 * the file check, then the tests in the sandbox. When they finish the page shows the test
 * report, the SBOM and, for a refused file, the problems in plain words.
 *
 * The browser sends the wheel straight to the storage bucket with the ticket's POST policy
 * (`api.ts uploadWheel`); the app never reads the bytes. Relaxed voice (v0.18 D28); errors
 * from the server are shown as they come (STE).
 */
import { Link, useNavigate } from "@tanstack/react-router";
import { ArrowRight, FileCheck2, Terminal, Upload } from "lucide-react";
import { useEffect, useId, useMemo, useState } from "react";

import { CopyButton, EmptyState, ErrorNotice, PageHeader } from "@/components/common";
import { InlineMessage } from "@/components/ods/inline-message";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/overlays";
import { Field, Input, NativeSelect, Progress } from "@/components/ui/primitives";
import { trustTierOf, useWorkspace } from "@/features/auth/workspace";
import { formatBytes } from "@/lib/format";
import { cn } from "@/lib/utils";

import { pluginFlags, useClaimPlugin, usePluginVersion, usePlugins, useUploadWheel } from "./api";
import { DevBanner } from "./dev-banner";
import { KitReport } from "./kit-report";
import { ProblemsList } from "./problems-list";
import { SbomSummary } from "./sbom-summary";
import { PluginStateChip } from "./state-chip";
import { displayText, initUploadCommand, isChecking, parseWheelName, pushCommand, stateDescription, uploadDistName, wheelFileProblem } from "./states";
import type { Plugin, UploadPhase } from "./types";
import { SHORT_NAME_MAX, SHORT_NAME_MIN, TITLE_MAX, UPLOAD_MAX_BYTES } from "./types";
import { CheckSteps, useRefreshOnStateChange } from "./version-page";
import { WheelDrop } from "./wheel-drop";

const LOWER = "abcdefghijklmnopqrstuvwxyz";
const DIGITS = "0123456789";

/** `PLUGIN_SHORT_RE` (`^[a-z][a-z0-9]{1,15}$`) in words. */
export function shortNameProblem(s: string): string | null {
  if (!s) return "Give the plugin a short name.";
  if (s.length < SHORT_NAME_MIN || s.length > SHORT_NAME_MAX) return `Use ${SHORT_NAME_MIN} to ${SHORT_NAME_MAX} characters.`;
  if (!LOWER.includes(s[0] ?? "")) return "Start with a lowercase letter.";
  for (const ch of s) if (!LOWER.includes(ch) && !DIGITS.includes(ch)) return "Use lowercase letters and digits only (no spaces, dashes or underscores).";
  return null;
}

const PHASE_LABEL: Record<UploadPhase, string> = {
  ticket: "Getting an upload link",
  sending: "Sending the file",
  completing: "Starting the checks",
  done: "Uploaded",
};
const PHASE_VALUE: Record<UploadPhase, number> = { ticket: 0.1, sending: 0.5, completing: 0.85, done: 1 };

function Step({ n, title, children, done }: { n: number; title: string; children: React.ReactNode; done?: boolean }) {
  return (
    <section className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-4 rounded-2xl border bg-card p-5 sm:p-6" aria-labelledby={`upload-step-${n}`}>
      <h2 id={`upload-step-${n}`} className="flex items-center gap-3 font-display text-xl leading-tight font-bold">
        <span aria-hidden className={cn("grid size-7 flex-none place-items-center rounded-full text-sm", done ? "bg-success/15 text-success" : "bg-accent text-accent-foreground")}>
          {done ? "✓" : n}
        </span>
        {title}
      </h2>
      {children}
    </section>
  );
}

function ClaimForm({ onClaimed }: { onClaimed: (p: Plugin) => void }) {
  const claim = useClaimPlugin();
  const [short, setShort] = useState("");
  const [title, setTitle] = useState("");
  const [touched, setTouched] = useState(false);
  const problem = shortNameProblem(short);
  const titleProblem = title.trim() ? null : "Give the plugin a title people will recognise.";
  return (
    <form
      className="grid gap-3"
      aria-label="Start a new plugin"
      onSubmit={(e) => {
        e.preventDefault();
        setTouched(true);
        if (problem || titleProblem) return;
        claim.mutate({ short_name: short, title: title.trim() }, { onSuccess: onClaimed });
      }}
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Short name" htmlFor="plugin-short" error={touched ? problem : null} hint="Part of every id it adds, like t_…_acme.store_mean. You can't change it later.">
          <Input id="plugin-short" value={short} maxLength={SHORT_NAME_MAX} autoComplete="off" spellCheck={false} onChange={(e) => setShort(e.target.value.trim().toLowerCase())} />
        </Field>
        <Field label="Title" htmlFor="plugin-title" error={touched ? titleProblem : null}>
          <Input id="plugin-title" value={title} maxLength={TITLE_MAX} onChange={(e) => setTitle(e.target.value)} />
        </Field>
      </div>
      {claim.isError ? <ErrorNotice error={claim.error} title="The plugin was not created" /> : null}
      <div>
        <Button type="submit" variant="outline" disabled={claim.isPending}>
          Create the plugin
        </Button>
      </div>
    </form>
  );
}

function CliCard({ plugin, projectId }: { plugin: Plugin | null; projectId: string }) {
  const short = plugin?.short_name ?? "acme";
  const init = initUploadCommand(short);
  const push = pushCommand(short, projectId || undefined);
  return (
    <div data-testid="plugin-upload-cli" className="grid gap-2 rounded-xl bg-muted p-4">
      <p className="flex items-center gap-2 text-sm font-semibold">
        <Terminal aria-hidden className="size-4" /> Or use the command line
      </p>
      <p className="text-sm text-muted-foreground">
        <code className="font-mono text-xs">mapi plugin push</code> builds the wheel, checks it on your machine and uploads it for you.
      </p>
      {[init, push].map((cmd) => (
        <div key={cmd} className="flex min-w-0 items-center gap-2 rounded-lg bg-card px-3 py-2">
          <code className="min-w-0 flex-1 font-mono text-xs [overflow-wrap:anywhere]">{cmd}</code>
          <CopyButton value={cmd} label="Copy the command" />
        </div>
      ))}
    </div>
  );
}

function LiveResult({ pluginId, versionId }: { pluginId: string; versionId: string }) {
  const version = usePluginVersion(pluginId, versionId);
  const v = version.data;
  useRefreshOnStateChange(v);
  if (!v) return version.isError ? <ErrorNotice error={version.error} title="The app could not load the new version" onRetry={() => void version.refetch()} /> : null;
  return (
    <div className="grid gap-4" data-testid="plugin-upload-result" data-state={v.state}>
      <div className="flex flex-wrap items-center gap-3" aria-live="polite">
        <PluginStateChip state={v.state} />
        <p className="text-sm text-muted-foreground">{stateDescription(v)}</p>
      </div>
      {isChecking(v) ? <CheckSteps state={v.state} /> : null}
      <ProblemsList problems={v.inspection?.problems ?? []} />
      <KitReport report={v.kit} />
      <SbomSummary sbom={v.inspection?.sbom} />
      {!isChecking(v) ? (
        <div>
          <Button className="h-auto min-h-11 rounded-full bg-highlight px-5 text-highlight-foreground hover:bg-highlight/85" asChild>
            <Link to="/plugins/$pluginId/versions/$versionId" params={{ pluginId, versionId }}>
              Open the version page <ArrowRight />
            </Link>
          </Button>
        </div>
      ) : null}
    </div>
  );
}

export function UploadPage({ initialPlugin }: { initialPlugin?: string | undefined }) {
  const ws = useWorkspace();
  const navigate = useNavigate();
  const flags = pluginFlags(ws.me);
  const tier = trustTierOf(ws.current?.tenant);
  const plugins = usePlugins({}, flags.enabled && tier !== "public");
  const upload = useUploadWheel();
  const [pluginId, setPluginId] = useState<string>(initialPlugin ?? "");
  const [creating, setCreating] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [requestApproval, setRequestApproval] = useState(false);
  const [phase, setPhase] = useState<UploadPhase | null>(null);
  const [versionId, setVersionId] = useState<string | null>(null);
  const approvalId = useId();

  const list = useMemo(() => plugins.data ?? [], [plugins.data]);
  useEffect(() => {
    if (!pluginId && list.length === 1 && list[0]) setPluginId(list[0].id);
    // Follow the data instead of latching: open the claim form only while nothing is picked
    // and the fresh list is empty, and close it once the picked plugin shows up (the page
    // remounts on the just-claimed plugin while the cached list is still empty).
    if (pluginId && list.some((p) => p.id === pluginId)) setCreating(false);
    else if (!pluginId && plugins.isSuccess && !plugins.isFetching && list.length === 0) setCreating(true);
  }, [list, pluginId, plugins.isSuccess, plugins.isFetching]);
  const plugin = list.find((p) => p.id === pluginId) ?? null;

  if (!flags.enabled || tier === "public") {
    return (
      <>
        <PageHeader title="Upload your own code" />
        <EmptyState title={tier === "public" ? "Public workspaces can't upload code" : "Uploads are turned off here"}>
          {tier === "public"
            ? "Built-in models, the no-code tools in Make your own and declarative specs all work in this workspace. To upload Python, use an internal or verified workspace."
            : "The people who run this platform turned code uploads off. The built-in models and Make your own still work."}
        </EmptyState>
      </>
    );
  }

  const fileProblem = file ? wheelFileProblem(file.name, file.size, UPLOAD_MAX_BYTES) : null;
  const wheel = file ? parseWheelName(file.name) : null;
  const distMismatch = plugin && wheel && wheel.dist.toLowerCase().split("_").join("-") !== uploadDistName(plugin.namespace) ? uploadDistName(plugin.namespace) : null;
  const busy = upload.isPending;

  return (
    <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-6">
      <PageHeader
        eyebrow={
          <Link to="/catalog" search={{ tab: "yours" }} className="hover:underline">
            Catalog › Yours
          </Link>
        }
        title="Upload your own code"
        description="Bring a Python package with your own models, prep steps, charts or workflow steps. We check the file, run its tests in a sandbox, and then you can use it right away. A reviewer approves it for the whole team."
      />
      <DevBanner />

      <Step n={1} title="Pick a plugin" done={Boolean(plugin)}>
        {plugins.isError ? <ErrorNotice error={plugins.error} title="The app could not load your plugins" onRetry={() => void plugins.refetch()} /> : null}
        {list.length ? (
          <Field label="Plugin" htmlFor="upload-plugin" hint={plugin ? `Namespace ${plugin.namespace}. The package must be named ${uploadDistName(plugin.namespace)}, so its wheel is ${plugin.namespace}-<version>-py3-none-any.whl.` : undefined}>
            <NativeSelect
              id="upload-plugin"
              value={creating ? "__new" : pluginId}
              onChange={(e) => {
                const v = e.target.value;
                setCreating(v === "__new");
                setPluginId(v === "__new" ? "" : v);
              }}
              className="max-w-full"
            >
              <option value="" disabled>
                Choose a plugin
              </option>
              {list.map((p) => (
                <option key={p.id} value={p.id}>
                  {displayText(p.title)} ({p.short_name})
                </option>
              ))}
              <option value="__new">Start a new plugin…</option>
            </NativeSelect>
          </Field>
        ) : null}
        {creating ? (
          <ClaimForm
            onClaimed={(p) => {
              setCreating(false);
              setPluginId(p.id);
              void navigate({ to: "/plugins/new", search: { plugin: p.id }, replace: true });
            }}
          />
        ) : null}
      </Step>

      <Step n={2} title="Add the wheel" done={Boolean(versionId)}>
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,22rem)]">
          <div className="grid content-start gap-3">
            <div data-testid="plugin-upload-drop">
              <WheelDrop
                disabled={!plugin || busy}
                label={file ? `${file.name} (${formatBytes(file.size)}). Drop another file to replace it.` : "Drop a wheel here, or choose one"}
                hint={`One pure-Python wheel (py3-none-any), up to ${Math.round(UPLOAD_MAX_BYTES / 1_048_576)} MiB.`}
                onFile={(f) => {
                  setFile(f);
                  setVersionId(null);
                  setPhase(null);
                  upload.reset();
                }}
              />
            </div>
            {fileProblem ? <InlineMessage tone="error">{fileProblem}</InlineMessage> : null}
            {!fileProblem && distMismatch ? (
              <InlineMessage tone="warning">
                This wheel's name doesn't start with {plugin?.namespace}, so the file check will refuse it. Build it with <code className="font-mono text-xs">mapi plugin push</code>.
              </InlineMessage>
            ) : null}
            <div className="flex items-start gap-2">
              <Checkbox id={approvalId} checked={requestApproval} onCheckedChange={(c) => setRequestApproval(c === true)} disabled={busy} className="mt-0.5" />
              <label htmlFor={approvalId} className="text-sm">
                Ask a reviewer to approve it for the team as soon as the tests pass
              </label>
            </div>
            {phase && busy ? (
              <div className="grid gap-1.5" aria-live="polite">
                <Progress value={PHASE_VALUE[phase]} label="Upload progress" />
                <p className="text-sm text-muted-foreground">{PHASE_LABEL[phase]}…</p>
              </div>
            ) : null}
            {upload.isError ? <ErrorNotice error={upload.error} title="The upload did not finish" /> : null}
            <div>
              <Button
                data-testid="plugin-upload-start"
                disabled={!plugin || !file || Boolean(fileProblem) || busy}
                className="h-auto min-h-11 rounded-full px-5"
                onClick={() => {
                  if (!plugin || !file) return;
                  upload.mutate(
                    { pluginId: plugin.id, file, requestApproval, onPhase: setPhase },
                    { onSuccess: (v) => setVersionId(v.id) },
                  );
                }}
              >
                <Upload /> Upload and check
              </Button>
            </div>
          </div>
          <CliCard plugin={plugin} projectId={ws.current?.project.project_id ?? ""} />
        </div>
      </Step>

      <Step n={3} title="Checks and tests" done={false}>
        {plugin && versionId ? (
          <LiveResult pluginId={plugin.id} versionId={versionId} />
        ) : (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <FileCheck2 aria-hidden className="size-4" /> The results show up here, usually within a minute or two.
          </p>
        )}
      </Step>
    </div>
  );
}
