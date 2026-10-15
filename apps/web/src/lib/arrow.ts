/**
 * Decode Arrow IPC (plot data, score outputs, UI grids) into plain rows of scalars for
 * tables and for posting to the plot sandbox. Values are normalised so they survive
 * structured cloning and the sandbox's message validator: BigInt → number (or string when
 * unsafe), timestamps/dates → ISO strings, NaN/±Inf → null, nested values → JSON text.
 */
import { DataType, tableFromIPC, type Table } from "apache-arrow";

export type Scalar = string | number | boolean | null;
export type Row = Record<string, Scalar>;

export interface DecodedTable {
  columns: { name: string; type: string }[];
  rows: Row[];
  totalRows: number;
  truncated: boolean;
}

export const DEFAULT_MAX_ROWS = 200_000;

export class ArrowDecodeError extends Error {
  override name = "ArrowDecodeError";
}

function toScalar(value: unknown, type: DataType): Scalar {
  if (value === null || value === undefined) return null;
  if (typeof value === "bigint") {
    const n = Number(value);
    return Number.isSafeInteger(n) ? n : value.toString();
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return null;
    if (DataType.isTimestamp(type) || DataType.isDate(type)) {
      // apache-arrow returns epoch milliseconds for timestamps and dates.
      const d = new Date(value);
      return Number.isFinite(d.getTime()) ? d.toISOString() : null;
    }
    return value;
  }
  if (typeof value === "string" || typeof value === "boolean") return value;
  if (value instanceof Date) return Number.isFinite(value.getTime()) ? value.toISOString() : null;
  if (ArrayBuffer.isView(value) || (typeof value === "object" && value !== null)) {
    try {
      const plain: unknown =
        typeof (value as { toJSON?: () => unknown }).toJSON === "function"
          ? (value as { toJSON: () => unknown }).toJSON()
          : value;
      return JSON.stringify(plain, (_k, v: unknown) => (typeof v === "bigint" ? v.toString() : v));
    } catch {
      return null;
    }
  }
  return null;
}

export function tableToRows(table: Table, maxRows = DEFAULT_MAX_ROWS): DecodedTable {
  const fields = table.schema.fields;
  const columns = fields.map((f) => ({ name: f.name, type: String(f.type) }));
  const n = Math.min(table.numRows, maxRows);
  const vectors = fields.map((f, i) => ({ name: f.name, type: f.type as DataType, vec: table.getChildAt(i) }));
  const rows: Row[] = new Array<Row>(n);
  for (let r = 0; r < n; r++) {
    const row: Row = {};
    for (const { name, type, vec } of vectors) row[name] = toScalar(vec?.get(r) as unknown, type);
    rows[r] = row;
  }
  return { columns, rows, totalRows: table.numRows, truncated: table.numRows > n };
}

export function decodeArrow(bytes: Uint8Array | ArrayBuffer, maxRows = DEFAULT_MAX_ROWS): DecodedTable {
  let table: Table;
  try {
    table = tableFromIPC(bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes));
  } catch (err) {
    throw new ArrowDecodeError(`not a valid Arrow IPC stream: ${err instanceof Error ? err.message : String(err)}`);
  }
  return tableToRows(table, maxRows);
}

export function base64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}
