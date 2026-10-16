/**
 * Decode the inline Arrow IPC datasets of an embedded plot into plain rows of scalars (the
 * shape the renderers take). Values are normalised like the web app does before posting
 * them to the sandbox: BigInt → number (or string when unsafe), timestamps and dates → ISO
 * strings, NaN/±Inf → null, nested values → JSON text.
 */
import { DataType, tableFromIPC, type Table } from "apache-arrow";

import { LIMITS, type Row, type Scalar } from "./protocol";

const FORBIDDEN_COLUMNS = new Set(["__proto__", "constructor", "prototype"]);

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
      const d = new Date(value);
      return Number.isFinite(d.getTime()) ? d.toISOString() : null;
    }
    return value;
  }
  if (typeof value === "string") return value.slice(0, LIMITS.maxStringLength);
  if (typeof value === "boolean") return value;
  if (value instanceof Date) return Number.isFinite(value.getTime()) ? value.toISOString() : null;
  try {
    const plain: unknown =
      typeof (value as { toJSON?: () => unknown }).toJSON === "function"
        ? (value as { toJSON: () => unknown }).toJSON()
        : value;
    const text = JSON.stringify(plain, (_k, v: unknown) => (typeof v === "bigint" ? v.toString() : v));
    return text.slice(0, LIMITS.maxStringLength);
  } catch {
    return null;
  }
}

export function tableToRows(table: Table, maxRows: number): Row[] {
  if (table.numRows > maxRows) {
    throw new ArrowDecodeError(`the dataset has ${table.numRows} rows; at most ${maxRows} are drawn`);
  }
  const fields = table.schema.fields;
  if (fields.length > LIMITS.maxColumns) throw new ArrowDecodeError("the dataset has too many columns");
  for (const f of fields) {
    if (FORBIDDEN_COLUMNS.has(f.name)) throw new ArrowDecodeError(`column name ${JSON.stringify(f.name)} is not allowed`);
  }
  const vectors = fields.map((f, i) => ({ name: f.name, type: f.type as DataType, vec: table.getChildAt(i) }));
  const rows: Row[] = new Array<Row>(table.numRows);
  for (let r = 0; r < table.numRows; r++) {
    const row: Row = {};
    for (const { name, type, vec } of vectors) row[name] = toScalar(vec?.get(r) as unknown, type);
    rows[r] = row;
  }
  return rows;
}

export function base64ToBytes(b64: string): Uint8Array {
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(b64)) throw new ArrowDecodeError("the dataset is not base64");
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/** Rows of a base64 Arrow IPC stream (or file). */
export function decodeArrowBase64(b64: string, maxRows: number = LIMITS.maxRowsTotal): Row[] {
  let table: Table;
  try {
    table = tableFromIPC(base64ToBytes(b64));
  } catch (err) {
    if (err instanceof ArrowDecodeError) throw err;
    throw new ArrowDecodeError(`not a valid Arrow IPC stream: ${err instanceof Error ? err.message : String(err)}`);
  }
  return tableToRows(table, maxRows);
}
