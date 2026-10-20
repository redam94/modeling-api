/**
 * The Params tab of the run page (v0.11): the run's tags and its parameters (from the spec,
 * the model code, workflow nodes such as `search.grid` trials, or the API) in a filterable
 * table. Keys, values and tags are untrusted text: rendered as text nodes only.
 */
import { SlidersHorizontal } from "lucide-react";
import { useMemo, useState } from "react";

import { useRunParams } from "@/api/queries";
import { runTags } from "@/api/tracking";
import type { Run } from "@/api/types";
import { EmptyState, ErrorNotice, LoadingRows, Mono } from "@/components/common";
import { Card, CardContent, CardHeader, CardTitle, Input, Table, TBody, TD, TH, THead, TR } from "@/components/ui/primitives";
import { absoluteTime, relativeTime } from "@/lib/format";

import { TagBadges } from "./tag-badges";

export function ParamsPanel({ run }: { run: Run }) {
  const params = useRunParams(run.id);
  const [needle, setNeedle] = useState("");
  const tags = runTags(run);
  const rows = useMemo(() => {
    const all = params.data?.items ?? [];
    const q = needle.trim().toLowerCase();
    return q ? all.filter((p) => p.key.toLowerCase().includes(q) || p.value.toLowerCase().includes(q)) : all;
  }, [params.data, needle]);

  return (
    <div className="grid gap-5">
      {Object.keys(tags).length ? (
        <Card>
          <CardHeader>
            <CardTitle>Tags</CardTitle>
          </CardHeader>
          <CardContent>
            <TagBadges tags={tags} />
          </CardContent>
        </Card>
      ) : null}
      <Card>
        <CardHeader className="flex-wrap">
          <CardTitle>Parameters</CardTitle>
          {params.data?.items.length ? (
            <Input aria-label="Filter parameters" placeholder="Filter by key or value" value={needle} onChange={(e) => setNeedle(e.target.value)} className="h-8 max-w-64" />
          ) : null}
        </CardHeader>
        <CardContent>
          {params.isError ? (
            <ErrorNotice error={params.error} title="Cannot load the parameters of the run" />
          ) : params.isPending ? (
            <LoadingRows rows={4} />
          ) : !params.data.items.length ? (
            <EmptyState icon={<SlidersHorizontal />} title="No parameters">
              The run did not log parameters.
            </EmptyState>
          ) : (
            <>
              <Table aria-label="Run parameters" className="table-fixed">
                <THead>
                  <TR>
                    <TH className="w-[38%]">Key</TH>
                    <TH>Value</TH>
                    <TH className="hidden w-20 sm:table-cell">Source</TH>
                    <TH className="hidden w-28 sm:table-cell">Logged</TH>
                  </TR>
                </THead>
                <TBody>
                  {rows.map((p) => (
                    <TR key={p.key}>
                      <TD className="align-top">
                        <Mono className="[overflow-wrap:anywhere]">{p.key}</Mono>
                      </TD>
                      <TD className="align-top whitespace-pre-wrap [overflow-wrap:anywhere]">{p.value}</TD>
                      <TD className="hidden align-top text-muted-foreground sm:table-cell">{p.source}</TD>
                      <TD className="hidden align-top text-muted-foreground sm:table-cell" title={absoluteTime(p.logged_at)}>
                        {relativeTime(p.logged_at)}
                      </TD>
                    </TR>
                  ))}
                </TBody>
              </Table>
              {!rows.length ? <p className="pt-3 text-[13px] text-muted-foreground">No parameters match the filter.</p> : null}
              {params.data.more ? <p className="pt-3 text-xs text-muted-foreground">The run has more parameters than the page shows.</p> : null}
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
