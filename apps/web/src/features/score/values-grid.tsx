/**
 * Values typed in instead of an uploaded dataset: the model's input table as an editable grid
 * (one row per period and level), with paste from a spreadsheet and "fill the column" for the
 * common case of one value everywhere.
 */
import { ClipboardPaste, PaintBucket, Plus, RotateCcw, Trash2 } from "lucide-react";
import { useState } from "react";

import type { InputTable, InputTableColumn } from "@/api/types";
import { Mono } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Field, Input, NativeSelect, TBody, TD, TH, THead, TR, Table, Textarea } from "@/components/ui/primitives";
import { cellText, nextLabel, parsePasted } from "@/lib/actuals";
import { cn } from "@/lib/utils";

import { fillColumn, summarize } from "./inputs";

const ROLE_HINT: Record<string, string> = {
  coord: "",
  target: "actual (blank: forecast)",
  driver: "needed on future periods",
  input: "",
};

export function ValuesGrid({
  columns,
  rows,
  onRows,
  onReset,
  horizonDim,
  blankDrivers,
}: {
  columns: InputTableColumn[];
  rows: string[][];
  onRows: (rows: string[][]) => void;
  onReset: () => void;
  horizonDim: string | null;
  /** What a blank driver cell becomes (a forecast's future covariate policy), for the placeholder. */
  blankDrivers: string;
}) {
  const [pasting, setPasting] = useState(false);
  const [pasted, setPasted] = useState("");
  const values = columns.map((c, i) => ({ c, i })).filter(({ c }) => c.role !== "coord");
  const [fillCol, setFillCol] = useState<number>(values[0]?.i ?? -1);
  const [fillValue, setFillValue] = useState("");
  const summary = summarize(columns, rows);
  const hIndex = columns.findIndex((c) => c.name === horizonDim);

  const set = (i: number, j: number, value: string) => onRows(rows.map((r, k) => (k === i ? r.map((c, m) => (m === j ? value : c)) : r)));
  const addRow = () => {
    const last = rows[rows.length - 1];
    const row = columns.map(() => "");
    if (last) {
      columns.forEach((c, j) => {
        if (c.role === "coord") row[j] = j === hIndex ? nextLabel(last[j] ?? "", c.freq) || (last[j] ?? "") : (last[j] ?? "");
      });
    }
    onRows([...rows, row]);
  };

  return (
    <div className="grid gap-3">
      <div className="max-h-96 overflow-auto rounded-md border">
        <Table aria-label="Values to score" className="text-xs">
          <THead className="sticky top-0 z-10 bg-card">
            <TR>
              {columns.map((c) => (
                <TH key={c.name} title={c.description}>
                  <Mono>{c.name}</Mono>
                  {ROLE_HINT[c.role] ? <span className="block font-normal text-muted-foreground">{ROLE_HINT[c.role]}</span> : null}
                </TH>
              ))}
              <TH className="w-10">
                <span className="sr-only">Remove</span>
              </TH>
            </TR>
          </THead>
          <TBody>
            {rows.map((r, i) => (
              <TR key={i}>
                {columns.map((c, j) => (
                  <TD key={c.name} className="px-1 py-1">
                    <Input
                      aria-label={`${c.name}, row ${i + 1}`}
                      inputMode={c.role === "coord" ? undefined : "decimal"}
                      className={cn("h-8 min-w-24 font-mono text-xs", c.role === "coord" && "bg-muted/40", c.role === "driver" && "bg-warning/6", c.role === "target" && "bg-primary/6")}
                      placeholder={c.role === "driver" ? blankDrivers : c.role === "target" ? "forecast" : ""}
                      value={r[j] ?? ""}
                      onChange={(e) => set(i, j, e.target.value)}
                    />
                  </TD>
                ))}
                <TD className="px-1 py-1">
                  <Button type="button" variant="ghost" size="icon-sm" aria-label={`Remove row ${i + 1}`} onClick={() => onRows(rows.filter((_, k) => k !== i))}>
                    <Trash2 />
                  </Button>
                </TD>
              </TR>
            ))}
          </TBody>
        </Table>
      </div>

      <div className="flex flex-wrap items-end gap-2">
        {values.length ? (
          <div className="flex flex-wrap items-end gap-1.5 rounded-md border p-1.5" role="group" aria-label="Fill a column">
            <NativeSelect aria-label="Column to fill" className="h-8 w-36 font-mono text-xs" value={fillCol} onChange={(e) => setFillCol(Number(e.target.value))}>
              {values.map(({ c, i }) => (
                <option key={c.name} value={i}>
                  {c.name}
                </option>
              ))}
            </NativeSelect>
            <Input aria-label="Value to fill with" inputMode="decimal" placeholder="value" className="h-8 w-24 font-mono text-xs" value={fillValue} onChange={(e) => setFillValue(e.target.value)} />
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={fillCol < 0 || fillValue.trim() === ""}
              title="Put this value in every blank cell of the column"
              onClick={() => onRows(fillColumn(rows, fillCol, fillValue.trim()))}
            >
              <PaintBucket /> Fill blanks
            </Button>
          </div>
        ) : null}
        <Button type="button" variant="outline" size="sm" onClick={addRow}>
          <Plus /> Add a row
        </Button>
        <Button type="button" variant="outline" size="sm" onClick={() => setPasting((p) => !p)} aria-expanded={pasting}>
          <ClipboardPaste /> Paste from a spreadsheet
        </Button>
        <Button type="button" variant="ghost" size="sm" onClick={onReset} title="Start again from the periods and levels the model knows">
          <RotateCcw /> Reset
        </Button>
      </div>
      <p className="text-xs text-muted-foreground" aria-live="polite">
        {summary.rows} row{summary.rows === 1 ? "" : "s"}, {summary.filled} value{summary.filled === 1 ? "" : "s"} typed
        {summary.emptyColumns.length ? ` · not sent (empty): ${summary.emptyColumns.join(", ")}` : ""}
      </p>

      {pasting ? (
        <Field
          label="Rows copied from a sheet"
          htmlFor="values-paste"
          hint={
            <>
              A header row ({columns.map((c) => c.name).join(", ")}) then one row per period and level; tabs or commas. Columns are matched by name.
            </>
          }
        >
          <div className="grid gap-2">
            <Textarea id="values-paste" className="min-h-24 font-mono text-xs" value={pasted} onChange={(e) => setPasted(e.target.value)} spellCheck={false} />
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="w-fit"
              onClick={() => {
                const table: InputTable = { horizon_dim: horizonDim, conditions_on_actuals: false, columns, rows: [] };
                const got = parsePasted(table, pasted);
                if (got.length) onRows(got.map((r) => r.map((c) => cellText(c))));
                setPasting(false);
              }}
            >
              Replace the rows
            </Button>
          </div>
        </Field>
      ) : null}
    </div>
  );
}
