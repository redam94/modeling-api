/**
 * Score summaries arrive in long format: a `stat` column (`mean`, `hdi_90_lower`, …) next to
 * the dims, one numeric column per variable. Charts want one row per coordinate with a
 * column per (variable, stat) instead.
 */
import type { DecodedTable, Row, Scalar } from "@/lib/arrow";

export const STAT_COLUMN = "stat";

const NUMERIC = /float|int|decimal/i;

/** Pivot `stat` into columns (`y_hat_mean`, `y_hat_hdi_90_lower`, …); other tables pass through. */
export function pivotStats(table: DecodedTable): DecodedTable {
  const stat = table.columns.find((c) => c.name === STAT_COLUMN && !NUMERIC.test(c.type));
  if (!stat) return table;
  // a numeric dim (a grid position, a year) is a key, not a value: the view names its dims
  const dims = new Set(table.dims ?? []);
  const isValue = (c: { name: string; type: string }) => NUMERIC.test(c.type) && !dims.has(c.name);
  const numeric = table.columns.filter((c) => c.name !== STAT_COLUMN && isValue(c));
  const keys = table.columns.filter((c) => c.name !== STAT_COLUMN && !isValue(c));
  const stats: string[] = [];
  const byKey = new Map<string, Row>();
  for (const row of table.rows) {
    const s = String(row[STAT_COLUMN] ?? "");
    if (!stats.includes(s)) stats.push(s);
    const id = JSON.stringify(keys.map((k) => row[k.name] ?? null));
    let out = byKey.get(id);
    if (!out) {
      out = Object.fromEntries(keys.map((k) => [k.name, row[k.name] ?? null]));
      byKey.set(id, out);
    }
    for (const c of numeric) out[`${c.name}_${s}`] = row[c.name] ?? null;
  }
  const columns = [
    ...keys,
    ...numeric.flatMap((c) => stats.map((s) => ({ name: `${c.name}_${s}`, type: c.type }))),
  ];
  const rows = [...byKey.values()];
  return { columns, rows, totalRows: rows.length, truncated: table.truncated, ...(table.dims ? { dims: table.dims } : {}) };
}

/**
 * Rows unique on the given fields. A variable broadcast over a dim it does not have (`y_hat`
 * repeated for every `component`) would otherwise draw one vertical zigzag per coordinate.
 */
export function distinctOn(rows: Row[], fields: (string | undefined)[]): Row[] {
  const used = fields.filter((f): f is string => Boolean(f));
  const seen = new Set<string>();
  const out: Row[] = [];
  for (const row of rows) {
    const id = JSON.stringify(used.map((f): Scalar => row[f] ?? null));
    if (seen.has(id)) continue;
    seen.add(id);
    out.push(row);
  }
  return out;
}
