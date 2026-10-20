/**
 * The Params section of the compare page (v0.14): the union of the runs' param keys as a table
 * (rows: keys, columns: runs). "Show only differences" (on at first) hides the keys that every
 * run logged with the same value. Values are untrusted text.
 */
import { useId, useMemo, useState } from "react";

import type { RunCompare } from "@/api/types";
import { Mono } from "@/components/common";
import { Switch } from "@/components/ui/overlays";
import { Card, CardContent, Input, Label, Table, TBody, TD, TH, THead, TR } from "@/components/ui/primitives";

import { paramRows } from "./compare-model";
import { RunSwatch } from "./run-chips";

/** Rows the table shows before "Show all". */
const PAGE_ROWS = 300;

export function ParamsSection({ data, labels }: { data: RunCompare; labels: readonly string[] }) {
  const [onlyDiffs, setOnlyDiffs] = useState(true);
  const [text, setText] = useState("");
  const [all, setAll] = useState(false);
  const id = useId();
  const params = useMemo(() => data.params ?? [], [data.params]);
  const rows = useMemo(() => paramRows(params, { onlyDiffs, text }), [params, onlyDiffs, text]);
  const shown = all ? rows : rows.slice(0, PAGE_ROWS);
  const differing = params.filter((p) => p.differs).length;

  if (!params.length) {
    return <p className="rounded-md border border-dashed px-3 py-6 text-center text-[13px] text-muted-foreground">The runs did not log params.</p>;
  }
  return (
    <section className="grid gap-4" aria-label="Params">
      <div className="flex flex-wrap items-center gap-3">
        <span className="flex items-center gap-2">
          <Switch id={id} checked={onlyDiffs} onCheckedChange={setOnlyDiffs} />
          <Label htmlFor={id} className="text-xs font-normal">
            Show only differences
          </Label>
        </span>
        <Input aria-label="Filter params" placeholder="Filter params" value={text} onChange={(e) => setText(e.target.value)} className="h-8 max-w-64" />
        <span className="text-xs text-muted-foreground">
          {differing} of {params.length} params differ.
        </span>
      </div>
      <Card>
        <CardContent className="pt-4">
          <Table aria-label="Params by run">
            <THead>
              <TR>
                <TH>Param</TH>
                {data.runs.map((r, i) => (
                  <TH key={r.id}>
                    <span className="inline-flex max-w-48 items-center gap-1.5">
                      <RunSwatch index={i} />
                      <span className="truncate" title={labels[i]}>
                        {labels[i]}
                      </span>
                    </span>
                  </TH>
                ))}
              </TR>
            </THead>
            <TBody>
              {shown.map((p) => (
                <TR key={p.key} data-differs={p.differs ? "true" : undefined} className={p.differs ? "bg-warning/6" : undefined}>
                  <TD>
                    <Mono className="[overflow-wrap:anywhere]">{p.key}</Mono>
                  </TD>
                  {data.runs.map((r) => {
                    const v = p.values[r.id];
                    return (
                      <TD key={r.id} className="max-w-72 align-top">
                        {v === null || v === undefined ? (
                          <span className="text-muted-foreground" title="The run did not log this param">
                            —
                          </span>
                        ) : (
                          <span className="font-mono text-xs whitespace-pre-wrap [overflow-wrap:anywhere]">{v}</span>
                        )}
                      </TD>
                    );
                  })}
                </TR>
              ))}
            </TBody>
          </Table>
          {!rows.length ? <p className="p-4 text-[13px] text-muted-foreground">{onlyDiffs ? "All the runs have the same values for these params." : "No params match."}</p> : null}
        </CardContent>
      </Card>
      {rows.length > shown.length ? (
        <button type="button" className="justify-self-start text-xs text-primary hover:underline" onClick={() => setAll(true)}>
          Show all {rows.length.toLocaleString("en")} params
        </button>
      ) : null}
      {data.truncated?.params ? <p className="text-xs text-muted-foreground">The runs have more params than the server sends. The table shows a part of them.</p> : null}
    </section>
  );
}
