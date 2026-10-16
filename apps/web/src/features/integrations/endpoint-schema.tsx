/**
 * What an endpoint expects (its input contract frozen at promotion), a ready example call,
 * "Try it" (validate or score the example, editable), the generated snippets in every language
 * and the "Open in notebook" cell.
 */
import { Link } from "@tanstack/react-router";
import { CheckCircle2, Clock, FlaskConical, KeyRound, Play } from "lucide-react";
import { useMemo, useState } from "react";

import { can } from "@/api/access";
import { useEndpointSchema, useEndpointSnippets, useScoreEndpoint, useValidateEndpointCall } from "@/api/integrations";
import type { Endpoint, EndpointSchema, EndpointScoreCall, Snippet, SnippetLanguage } from "@/api/types";
import { CopyButton, ErrorNotice, IssueList, JsonBlock, KeyValues, Mono, QueryView, Spinner, StateBadge } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/overlays";
import { Badge, Card, CardContent, CardDescription, CardHeader, CardTitle, Field, TBody, TD, TH, THead, TR, Table, Textarea } from "@/components/ui/primitives";
import { useWorkspace } from "@/features/auth/workspace";
import { env } from "@/env";
import { formatBytes } from "@/lib/format";
import { notebookSnippet, parseCallJson, SNIPPET_LANGUAGES } from "@/lib/integrations";
import { toast } from "@/stores/toast";

// ----------------------------------------------------------------------- code block

export function CodeBlock({ code, label, language }: { code: string; label: string; language?: string }) {
  return (
    <div className="relative min-w-0">
      <pre
        aria-label={label}
        data-language={language}
        className="max-h-[28rem] overflow-auto rounded-md border bg-muted/50 p-3 pr-11 font-mono text-xs leading-5 whitespace-pre"
      >
        {code}
      </pre>
      <div className="absolute top-1.5 right-1.5">
        <CopyButton value={code} label={`Copy ${label}`} onCopied={() => toast.success("Copied to the clipboard", label)} />
      </div>
    </div>
  );
}

// ----------------------------------------------------------------------- schema

function SchemaTables({ schema }: { schema: EndpointSchema }) {
  const snap = schema.snapshot;
  const dims = Object.entries(snap.input_schema.dims ?? {});
  const vars = Object.entries(snap.input_schema.vars ?? {});
  return (
    <div className="grid gap-4">
      <KeyValues
        items={[
          ["Score modes", <span key="m" className="flex flex-wrap gap-1">{(snap.score_modes ?? []).map((m) => <Badge key={m} variant="outline">{m}</Badge>)}</span>],
          ["Forecasts extend", snap.horizon_dim ? <Mono key="h">{snap.horizon_dim}</Mono> : "—"],
          ["Needed in the future", snap.future_required?.length ? <Mono key="f">{snap.future_required.join(", ")}</Mono> : "Nothing beyond the horizon"],
          ["Inputs at stage", <Mono key="s">{snap.inputs_stage ?? "raw"}</Mono>],
          ["Inline data", `up to ${formatBytes(schema.max_inline_bytes ?? 5 * 1024 * 1024)} of XrJSON per call`],
          ["Contract hash", <Mono key="x" title={snap.schema_hash}>{snap.schema_hash.slice(0, 19)}…</Mono>],
        ]}
      />
      <div className="grid gap-4 lg:grid-cols-2">
        <Table aria-label="Dimensions">
          <THead>
            <TR>
              <TH>Dimension</TH>
              <TH>Kind</TH>
              <TH>New levels</TH>
            </TR>
          </THead>
          <TBody>
            {dims.map(([name, d]) => (
              <TR key={name}>
                <TD>
                  <Mono>{name}</Mono>
                </TD>
                <TD>
                  {d.kind}
                  {d.freq ? <span className="text-muted-foreground"> · {d.freq}</span> : null}
                </TD>
                <TD className="text-muted-foreground">{d.policy ?? "fixed"}</TD>
              </TR>
            ))}
          </TBody>
        </Table>
        <Table aria-label="Variables">
          <THead>
            <TR>
              <TH>Variable</TH>
              <TH>Dims</TH>
              <TH>Role</TH>
            </TR>
          </THead>
          <TBody>
            {vars.map(([name, v]) => (
              <TR key={name}>
                <TD>
                  <Mono>{name}</Mono>
                  <span className="block text-xs text-muted-foreground">
                    {v.dtype}
                    {v.units ? ` · ${v.units}` : ""}
                  </span>
                </TD>
                <TD>
                  <Mono className="text-muted-foreground">({(v.dims ?? []).join(", ")})</Mono>
                </TD>
                <TD>{v.role ?? "covariate"}</TD>
              </TR>
            ))}
          </TBody>
        </Table>
      </div>
    </div>
  );
}

// ----------------------------------------------------------------------- try it

function TryIt({ endpoint, schema }: { endpoint: Endpoint; schema: EndpointSchema }) {
  const ws = useWorkspace();
  const canRun = can(ws.grants, "run", "endpoint");
  const initial = useMemo(() => JSON.stringify(schema.example, null, 2), [schema.example]);
  const [text, setText] = useState(initial);
  const validate = useValidateEndpointCall(endpoint.alias);
  const score = useScoreEndpoint(endpoint.alias);
  const parsed = parseCallJson(text);
  const body = parsed.value as EndpointScoreCall | null;
  const busy = validate.isPending || score.isPending;
  return (
    <div className="grid gap-4">
      <Field
        label="Call body"
        htmlFor="try-body"
        error={parsed.error}
        hint={
          <>
            An <Mono>EndpointScoreCall</Mono>: the example validates as is. <Mono>data</Mono> and <Mono>scenario</Mono> take <Mono>dataset://</Mono> references or inline XrJSON.
          </>
        }
      >
        <Textarea id="try-body" className="min-h-48 font-mono text-xs" spellCheck={false} value={text} onChange={(e) => setText(e.target.value)} />
      </Field>
      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="outline"
          disabled={!body || busy}
          onClick={() => {
            if (!body) return;
            score.reset();
            validate.mutate(body);
          }}
        >
          {validate.isPending ? <Spinner /> : <CheckCircle2 />} Validate
        </Button>
        <Button
          disabled={!body || busy || !canRun}
          title={canRun ? "Runs a real score (spends compute)" : "Scoring needs the runner role"}
          onClick={() => {
            if (!body) return;
            validate.reset();
            score.mutate({ body, waitSeconds: 30 });
          }}
        >
          {score.isPending ? <Spinner /> : <Play />} Score
        </Button>
        {text !== initial ? (
          <Button variant="ghost" size="sm" onClick={() => setText(initial)}>
            Reset to the example
          </Button>
        ) : null}
        <span className="text-xs text-muted-foreground">Validate runs only the static checks; Score starts a real run (up to 30 s wait).</span>
      </div>
      <section aria-label="Try it result" aria-live="polite" className="grid gap-2">
        {validate.isError ? <ErrorNotice error={validate.error} title="The call could not be checked" /> : null}
        {validate.data ? (
          validate.data.valid ? (
            <div className="grid gap-2 rounded-lg border border-success/30 bg-success/6 p-3">
              <p className="flex items-center gap-2 font-medium">
                <CheckCircle2 className="size-4 text-success" /> The call is valid
              </p>
              {validate.data.issues?.length ? <IssueList issues={validate.data.issues} /> : null}
              {validate.data.request ? (
                <details>
                  <summary className="cursor-pointer text-xs text-muted-foreground">The score request it becomes</summary>
                  <JsonBlock value={validate.data.request} className="mt-2" maxHeight="16rem" />
                </details>
              ) : null}
            </div>
          ) : (
            <div className="grid gap-2 rounded-lg border border-destructive/35 bg-destructive/6 p-3" role="alert">
              <p className="font-medium">The call would be refused</p>
              <IssueList issues={validate.data.issues ?? []} />
            </div>
          )
        ) : null}
        {score.isError ? <ErrorNotice error={score.error} title="The endpoint could not score the call" /> : null}
        {score.data ? (
          <div className="grid gap-2 rounded-lg border p-3">
            <div className="flex flex-wrap items-center gap-2">
              {score.data.result.done ? <CheckCircle2 className="size-4 text-success" /> : <Clock className="size-4 text-info" />}
              <span className="font-medium">{score.data.result.done ? "Scored" : "Still running"}</span>
              <StateBadge state={score.data.result.run.state} />
              <Link to="/runs/$runId" params={{ runId: score.data.result.run.id }} className="text-[13px] text-primary hover:underline">
                Open run <Mono>{score.data.result.run.id}</Mono>
              </Link>
            </div>
            {score.data.result.outputs?.length ? (
              <ul className="grid gap-1 text-[13px]">
                {score.data.result.outputs.map((o) => (
                  <li key={o.name} className="flex flex-wrap gap-2">
                    <Mono>{o.name}</Mono>
                    <span className="text-muted-foreground">{formatBytes(o.size)}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-[13px] text-muted-foreground">The run page shows its progress and outputs once it finishes.</p>
            )}
          </div>
        ) : null}
      </section>
    </div>
  );
}

// ----------------------------------------------------------------------- snippets

export function SnippetView({ snippet }: { snippet: Snippet }) {
  return (
    <div className="grid gap-2">
      <p className="flex flex-wrap items-center gap-2 text-[13px]">
        <span className="font-medium">{snippet.title}</span>
        {snippet.requires ? (
          <span className="text-muted-foreground">
            First: <Mono>{snippet.requires}</Mono>
          </span>
        ) : null}
      </p>
      <CodeBlock code={snippet.code} label={`${snippet.title} snippet`} language={snippet.language} />
    </div>
  );
}

export function SnippetTabs({ endpoint, schema }: { endpoint: Endpoint; schema: EndpointSchema | undefined }) {
  const snippets = useEndpointSnippets(endpoint.alias, endpoint.revision);
  const ws = useWorkspace();
  const [tab, setTab] = useState<SnippetLanguage | "notebook">("python");
  const project = ws.current?.key ?? `${endpoint.tenant_id}/${endpoint.project_id}`;
  return (
    <QueryView query={snippets}>
      {(set) => {
        const byLang = new Map((set.items ?? []).map((s) => [s.language, s]));
        return (
          <Tabs value={tab} onValueChange={(v) => setTab(v as SnippetLanguage | "notebook")}>
            <TabsList aria-label="Snippet languages">
              {SNIPPET_LANGUAGES.filter((l) => byLang.has(l.id)).map((l) => (
                <TabsTrigger key={l.id} value={l.id}>
                  {l.label}
                </TabsTrigger>
              ))}
              <TabsTrigger value="notebook">Notebook</TabsTrigger>
            </TabsList>
            {SNIPPET_LANGUAGES.map((l) => {
              const s = byLang.get(l.id);
              return s ? (
                <TabsContent key={l.id} value={l.id}>
                  <SnippetView snippet={s} />
                </TabsContent>
              ) : null;
            })}
            <TabsContent value="notebook">
              <div className="grid gap-2">
                <p className="text-[13px] text-muted-foreground">
                  Open in a notebook: paste this cell into Jupyter or marimo. It scores the endpoint with the Python client and the example call.
                </p>
                <CodeBlock code={notebookSnippet({ baseUrl: env.apiBase, project, alias: endpoint.alias, example: schema?.example ?? {} })} label="Notebook cell" language="python" />
              </div>
            </TabsContent>
          </Tabs>
        );
      }}
    </QueryView>
  );
}

// ----------------------------------------------------------------------- sections

export function EndpointSchemaPanel({ endpoint }: { endpoint: Endpoint }) {
  const schema = useEndpointSchema(endpoint.alias, endpoint.revision);
  return (
    <div className="grid gap-5">
      <Card>
        <CardHeader>
          <div>
            <CardTitle>What to send</CardTitle>
            <CardDescription>The input contract of r{endpoint.revision}, frozen when it was promoted.</CardDescription>
          </div>
        </CardHeader>
        <CardContent>
          <QueryView query={schema}>{(s) => <SchemaTables schema={s} />}</QueryView>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <div>
            <CardTitle className="flex items-center gap-2">
              <FlaskConical className="size-4" /> Try it
            </CardTitle>
            <CardDescription>Check or run the example call against the live endpoint, as an integration would.</CardDescription>
          </div>
        </CardHeader>
        <CardContent>
          <QueryView query={schema}>{(s) => <TryIt key={s.revision} endpoint={endpoint} schema={s} />}</QueryView>
        </CardContent>
      </Card>
    </div>
  );
}

export function EndpointSnippetsPanel({ endpoint }: { endpoint: Endpoint }) {
  const schema = useEndpointSchema(endpoint.alias, endpoint.revision);
  return (
    <Card>
      <CardHeader className="flex-wrap">
        <div>
          <CardTitle>Snippets</CardTitle>
          <CardDescription>Ready-to-run code with the example call. Snippets never contain a secret: they read the token from MAPI_TOKEN (a script property in Apps Script).</CardDescription>
        </div>
        <Button variant="outline" size="sm" asChild>
          <Link to="/tokens" search={{ preset: `score:${endpoint.alias}` }}>
            <KeyRound /> Create a score:{endpoint.alias} token
          </Link>
        </Button>
      </CardHeader>
      <CardContent>
        <SnippetTabs endpoint={endpoint} schema={schema.data} />
      </CardContent>
    </Card>
  );
}
