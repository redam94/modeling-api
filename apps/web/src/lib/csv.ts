/**
 * A small RFC 4180 CSV reader for the local preview of a file before upload (plan §3.3:
 * the browser previews the user's own file so nothing reaches the server just to be
 * previewed). Only the head of the file is read.
 */

export interface CsvPreview {
  header: string[];
  rows: string[][];
  /** True if the sample ended mid-file (more rows exist). */
  truncated: boolean;
  delimiter: string;
}

export function detectDelimiter(firstLine: string): string {
  const candidates = [",", ";", "\t", "|"];
  let best = ",";
  let bestCount = 0;
  for (const d of candidates) {
    const count = firstLine.split(d).length - 1;
    if (count > bestCount) {
      best = d;
      bestCount = count;
    }
  }
  return best;
}

/** Parse up to `maxRows` data rows from CSV text (which may be a truncated sample). */
export function parseCsv(text: string, maxRows = 50, delimiter?: string): CsvPreview {
  const src = text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
  const firstNl = src.search(/\r?\n/);
  const delim = delimiter ?? detectDelimiter(firstNl < 0 ? src : src.slice(0, firstNl));
  const records: string[][] = [];
  let field = "";
  let record: string[] = [];
  let inQuotes = false;
  let i = 0;
  let complete = true;
  const push = () => {
    record.push(field);
    field = "";
  };
  while (i < src.length && records.length <= maxRows) {
    const ch = src[i] as string;
    if (inQuotes) {
      if (ch === '"') {
        if (src[i + 1] === '"') {
          field += '"';
          i += 2;
          continue;
        }
        inQuotes = false;
      } else {
        field += ch;
      }
      i += 1;
      continue;
    }
    if (ch === '"' && field === "") inQuotes = true;
    else if (ch === delim) push();
    else if (ch === "\n" || ch === "\r") {
      push();
      records.push(record);
      record = [];
      if (ch === "\r" && src[i + 1] === "\n") i += 1;
    } else field += ch;
    i += 1;
  }
  if (i >= src.length && (field !== "" || record.length)) {
    if (inQuotes) complete = false;
    push();
    records.push(record);
  }
  const [header = [], ...rest] = records;
  const rows = rest.slice(0, maxRows);
  return {
    header: header.map((h) => h.trim()),
    rows,
    truncated: !complete || rest.length > maxRows || i < src.length,
    delimiter: delim,
  };
}

/** Guess a column's role in the ingest mapping from its name and sample values. */
export function guessColumn(name: string, samples: string[]): { as: "dim" | "var"; kind?: "time" | "categorical" | "integer" } {
  const n = name.toLowerCase();
  const values = samples.filter((s) => s !== "");
  const numeric = values.length > 0 && values.every((v) => /^-?\d+(\.\d+)?([eE][-+]?\d+)?$/.test(v));
  const dateLike = values.length > 0 && values.every((v) => /^\d{4}-\d{2}-\d{2}([T ][\d:.]+Z?)?$/.test(v));
  if (dateLike || /^(date|week|month|time|day|period|ts)$/.test(n)) return { as: "dim", kind: "time" };
  if (!numeric) return { as: "dim", kind: "categorical" };
  return { as: "var" };
}
