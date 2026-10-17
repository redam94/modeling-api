/**
 * Forecasts with actuals on an endpoint (`mapi_spec.actuals`): the input table a spreadsheet's
 * Input sheet uses (`EndpointSchema.input_table`), shown as a preview, edited in place (actuals
 * where you have them, blank targets on the dates to forecast) and turned into the call; and the
 * answer's note on what the forecast was conditioned on.
 */
import { CalendarClock, ClipboardPaste, Plus, Trash2, Wand2 } from "lucide-react";
import { useMemo, useState } from "react";

import type { EndpointSchema, EndpointScoreCall, ForecastConditioning, InputTable } from "@/api/types";
import { Mono } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Badge, Field, Input, TBody, TD, TH, THead, TR, Table, Textarea } from "@/components/ui/primitives";
import { cellText, conditioningHeadline, nextLabel, parseCell, parsePasted, tableForecastCall, type Cell } from "@/lib/actuals";
import { cn } from "@/lib/utils";

const ROLE_LABEL: Record<string, string> = { coord: "date / level", target: "target", driver: "driver", input: "input" };

/** Whether the endpoint forecasts from a table (it has a dim to extend and a target column). */
export function forecastTable(schema: EndpointSchema): InputTable | null {
  const t = schema.input_table;
  if (!t?.horizon_dim || !(t.columns ?? []).some((c) => c.role === "target")) return null;
  if (!(schema.snapshot.score_modes ?? []).includes("forecast")) return null;
  return t;
}

function targetsOf(table: InputTable): string {
  return (table.columns ?? []).filter((c) => c.role === "target").map((c) => c.name).join(", ");
}

/** How to fill the table, in one sentence. */
export function actualsHint(table: InputTable): string {
  const targets = targetsOf(table);
  const drivers = (table.columns ?? []).some((c) => c.role === "driver") ? " On those rows, the model uses the latest value for blank driver cells." : "";
  return table.conditions_on_actuals
    ? `Type ${targets} on the periods that have actuals. Leave ${targets} blank on the dates to forecast. The model updates its state with the actuals and forecasts the blank rows. It does not fit again.${drivers}`
    : `Type the dates to forecast and leave ${targets} blank. This model forecasts from the end of its fitted history. It refuses actuals. It does not ignore them.${drivers}`;
}

/** A read-only preview of the Input sheet: the header row and the first periods after the fit. */
export function InputTablePreview({ table, maxRows = 4, label = "Input sheet layout" }: { table: InputTable; maxRows?: number; label?: string }) {
  const columns = table.columns ?? [];
  const rows = (table.rows ?? []).slice(0, maxRows);
  const target = new Set(columns.filter((c) => c.role === "target").map((c) => c.name));
  return (
    <Table aria-label={label} className="text-xs">
      <THead>
        <TR>
          {columns.map((c) => (
            <TH key={c.name} title={c.description}>
              <Mono>{c.name}</Mono>
              {" "}<span className="font-normal text-muted-foreground">({ROLE_LABEL[c.role] ?? c.role})</span>
            </TH>
          ))}
        </TR>
      </THead>
      <TBody>
        {rows.length ? (
          rows.map((r, i) => (
            <TR key={i}>
              {columns.map((c, j) => (
                <TD key={c.name} className={cn("font-mono", target.has(c.name) && "bg-primary/6")}>
                  {cellText(r[j] ?? null) || <span className="text-muted-foreground">{target.has(c.name) ? (i < 2 && table.conditions_on_actuals ? "actual" : "blank") : ""}</span>}
                </TD>
              ))}
            </TR>
          ))
        ) : (
          <TR>
            <TD colSpan={columns.length} className="text-muted-foreground">
              One row for each period (and for each level of the other columns).
            </TD>
          </TR>
        )}
      </TBody>
    </Table>
  );
}

/**
 * Actuals and the dates to forecast, edited as the Input sheet's table. "Use in the call" turns the
 * rows into a forecast call (actual rows, then rows with the target blank).
 */
export function ActualsEditor({ schema, onUse }: { schema: EndpointSchema; onUse: (call: EndpointScoreCall) => void }) {
  const table = forecastTable(schema);
  const columns = useMemo(() => table?.columns ?? [], [table]);
  const initial = useMemo(() => (table?.rows ?? []).map((r) => r.map((c) => cellText(c))), [table]);
  const [rows, setRows] = useState<string[][]>(initial);
  const [pasting, setPasting] = useState(false);
  const [pasted, setPasted] = useState("");
  const [error, setError] = useState<string | null>(null);
  if (!table) return null;
  const hdim = table.horizon_dim!;
  const hIndex = columns.findIndex((c) => c.name === hdim);
  const targetIdx = columns.map((c, i) => (c.role === "target" ? i : -1)).filter((i) => i >= 0);
  const actuals = rows.filter((r) => targetIdx.some((i) => (r[i] ?? "").trim() !== "")).length;
  const blank = rows.filter((r) => r.some((c) => c.trim() !== "") && targetIdx.every((i) => (r[i] ?? "").trim() === "")).length;

  const parsed = (): Cell[][] => rows.map((r) => columns.map((c, i) => parseCell(r[i] ?? "", c)));
  const set = (i: number, j: number, value: string) => setRows((rs) => rs.map((r, k) => (k === i ? r.map((c, m) => (m === j ? value : c)) : r)));
  const addRow = () =>
    setRows((rs) => {
      const last = rs[rs.length - 1];
      const row = columns.map(() => "");
      if (last && hIndex >= 0) {
        row[hIndex] = nextLabel(last[hIndex] ?? "", columns[hIndex]?.freq);
        columns.forEach((c, j) => {
          if (c.role === "coord" && j !== hIndex) row[j] = last[j] ?? "";
        });
      }
      return [...rs, row];
    });

  return (
    <section aria-label="Actuals and dates to forecast" className="grid gap-3 rounded-lg border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <CalendarClock className="size-4 text-primary" />
        <span className="font-medium">{table.conditions_on_actuals ? "Your latest actuals and the dates to forecast" : "The dates to forecast"}</span>
        <Badge variant={table.conditions_on_actuals ? "success" : "outline"}>{table.conditions_on_actuals ? "conditions on actuals" : "forecasts from the fit"}</Badge>
      </div>
      <p className="text-[13px] text-muted-foreground">{actualsHint(table)}</p>
      <Table aria-label="Actuals table" className="text-xs">
        <THead>
          <TR>
            {columns.map((c) => (
              <TH key={c.name} title={c.description}>
                <Mono>{c.name}</Mono> <span className="font-normal">({ROLE_LABEL[c.role] ?? c.role})</span>
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
                    className={cn("h-8 min-w-24 font-mono text-xs", c.role === "target" && "bg-primary/6")}
                    placeholder={c.role === "target" ? "blank: forecast" : c.role === "driver" ? "blank: latest value" : ""}
                    value={r[j] ?? ""}
                    onChange={(e) => set(i, j, e.target.value)}
                  />
                </TD>
              ))}
              <TD className="px-1 py-1">
                <Button variant="ghost" size="icon-sm" aria-label={`Remove row ${i + 1}`} onClick={() => setRows((rs) => rs.filter((_, k) => k !== i))}>
                  <Trash2 />
                </Button>
              </TD>
            </TR>
          ))}
        </TBody>
      </Table>
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="outline" size="sm" onClick={addRow}>
          <Plus /> Add a period
        </Button>
        <Button variant="outline" size="sm" onClick={() => setPasting((p) => !p)} aria-expanded={pasting}>
          <ClipboardPaste /> Paste from a spreadsheet
        </Button>
        <Button
          size="sm"
          onClick={() => {
            try {
              onUse(tableForecastCall(table, parsed(), schema.example));
              setError(null);
            } catch (e) {
              setError(e instanceof Error ? e.message : String(e));
            }
          }}
        >
          <Wand2 /> Use in the call
        </Button>
        <span className="text-xs text-muted-foreground" aria-live="polite">
          {actuals} actual{actuals === 1 ? "" : "s"}, {blank} period{blank === 1 ? "" : "s"} to forecast
          {blank === 0 ? ` (the app adds ${String((schema.example as { horizon?: { periods?: number } }).horizon?.periods ?? 8)} periods)` : ""}
        </span>
      </div>
      {pasting ? (
        <Field label="Rows copied from a sheet" htmlFor="actuals-paste" hint={<>Paste a header row ({(table.columns ?? []).map((c) => c.name).join(", ")}), then one row for each period. Use tabs or commas between the values.</>}>
          <div className="grid gap-2">
            <Textarea id="actuals-paste" className="min-h-24 font-mono text-xs" value={pasted} onChange={(e) => setPasted(e.target.value)} spellCheck={false} />
            <Button
              variant="outline"
              size="sm"
              className="w-fit"
              onClick={() => {
                const got = parsePasted(table, pasted);
                if (got.length) setRows(got.map((r) => r.map((c) => cellText(c))));
                setPasting(false);
              }}
            >
              Replace the rows
            </Button>
          </div>
        </Field>
      ) : null}
      {error ? (
        <p role="alert" className="text-[13px] text-destructive">
          {error}
        </p>
      ) : null}
    </section>
  );
}

/** The answer's note: conditioned on actuals through …, or from the end of the fitted history. */
export function ConditioningNote({ conditioning }: { conditioning: ForecastConditioning }) {
  return (
    <div role="note" aria-label="What the forecast was conditioned on" className={cn("grid gap-1 rounded-md border p-2.5 text-[13px]", conditioning.conditioned ? "border-success/30 bg-success/6" : "bg-muted/40")}>
      <p className="flex flex-wrap items-center gap-2 font-medium">
        <CalendarClock className="size-4" /> {conditioningHeadline(conditioning)}
        {conditioning.revised ? <Badge variant="info">{conditioning.revised} revised</Badge> : null}
        <Badge variant="outline">parameters as fitted</Badge>
      </p>
      <p className="text-muted-foreground">{conditioning.message}</p>
    </div>
  );
}
