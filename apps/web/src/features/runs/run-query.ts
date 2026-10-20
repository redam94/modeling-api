/**
 * TS port of the run-search grammar of `mapi_spec.tracking` (v0.11): refs (`metric.rmse{split=val}.min`,
 * `param.fit.draws`, `tag.team`, `name`, `created_at`), filters (`<ref> <op> <value>`) and
 * `order_by` (`-<ref>`). The runs page checks what the user types before it asks the server; the
 * server stays the authority and its 422 message is shown as is.
 *
 * Error messages are the Python ones, character for character (`repr()` quoting included), so
 * the shared vectors (`run-query.vectors.gen.json`, generated from the Python parsers) can
 * compare them. Python's `re` `$` also matches before one trailing newline: `pyMatch` copies that.
 */

export type RunRefKind = "metric" | "param" | "tag" | "name" | "created_at";
export type MetricAgg = "last" | "min" | "max" | "count";
export type RunFilterOp = "<" | "<=" | ">" | ">=" | "=" | "!=" | "~";

export const METRIC_KEY_PATTERN = "^[a-z][a-z0-9_]{0,63}$";
export const METRIC_LABEL_KEY_PATTERN = "^[a-z][a-z0-9_]{0,31}$";
export const METRIC_LABEL_VALUE_PATTERN = "^[A-Za-z0-9_.\\-]{1,63}$";
export const PARAM_KEY_PATTERN = "^[A-Za-z0-9_][A-Za-z0-9_.\\-/]{0,249}$";
export const TAG_KEY_PATTERN = "^[A-Za-z0-9_][A-Za-z0-9_.\\-/]{0,127}$";
export const RESERVED_TAG_PREFIX = "mapi.";
export const MAX_METRIC_LABELS = 8;
/** `mapi_spec.tracking.MAX_RUN_FILTERS` / `MAX_RUN_COLUMNS`. */
export const MAX_RUN_FILTERS = 8;
export const MAX_RUN_COLUMNS = 16;

export const METRIC_AGGS: readonly MetricAgg[] = ["last", "min", "max", "count"];
/** Filter operators, longest first (the parser tries them in this order). */
export const RUN_FILTER_OPS: readonly RunFilterOp[] = ["<=", ">=", "!=", "<", ">", "=", "~"];
const ORDER_OPS = new Set<RunFilterOp>(["<", "<=", ">", ">="]);
const OPS_BY_KIND: Record<RunRefKind, ReadonlySet<RunFilterOp>> = {
  metric: new Set(["<", "<=", ">", ">=", "=", "!="]),
  param: new Set(RUN_FILTER_OPS),
  tag: new Set(["=", "!=", "~"]),
  name: new Set(["=", "~"]),
  created_at: new Set(),
};

const METRIC_KEY = new RegExp(METRIC_KEY_PATTERN);
const LABEL_KEY = new RegExp(METRIC_LABEL_KEY_PATTERN);
const LABEL_VALUE = new RegExp(METRIC_LABEL_VALUE_PATTERN);
const PARAM_KEY = new RegExp(PARAM_KEY_PATTERN);
const TAG_KEY = new RegExp(TAG_KEY_PATTERN);
const METRIC_REF = /^metric\.([^.{}]+)(?:\{([^{}]*)\})?(?:\.([a-z]+))?$/;

export interface RunRef {
  kind: RunRefKind;
  key: string | null;
  /** Sorted by key. */
  labels: [string, string][];
  agg: MetricAgg;
}

export interface RunFilter {
  ref: RunRef;
  op: RunFilterOp;
  value: string | number;
}

export interface RunOrder {
  ref: RunRef;
  descending: boolean;
}

/** A grammar error (the message of the Python `ValueError`). */
export class RunQueryError extends Error {
  override name = "RunQueryError";
}

/** `re.match(pattern, s)` for a pattern that ends in `$`: one trailing newline is allowed. */
function pyMatch(re: RegExp, s: string): boolean {
  return re.test(s) || (s.endsWith("\n") && re.test(s.slice(0, -1)));
}

function hex(n: number, width: number): string {
  return n.toString(16).padStart(width, "0");
}

/** Python's `repr()` of a str. */
export function pyRepr(s: string): string {
  const quote = s.includes("'") && !s.includes('"') ? '"' : "'";
  let out = quote;
  for (const ch of s) {
    const cp = ch.codePointAt(0)!;
    if (ch === "\\") out += "\\\\";
    else if (ch === quote) out += `\\${quote}`;
    else if (ch === "\n") out += "\\n";
    else if (ch === "\r") out += "\\r";
    else if (ch === "\t") out += "\\t";
    else if (ch !== " " && /[\p{C}\p{Z}]/u.test(ch)) out += cp <= 0xff ? `\\x${hex(cp, 2)}` : cp <= 0xffff ? `\\u${hex(cp, 4)}` : `\\U${hex(cp, 8)}`;
    else out += ch;
  }
  return out + quote;
}

/** Python's `str.strip()` (whitespace only). */
const PY_SPACE = "[\\t\\n\\v\\f\\r \\u001c-\\u001f\\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000]";
const PY_STRIP = new RegExp(`^${PY_SPACE}+|${PY_SPACE}+$`, "g");

function pyStrip(s: string): string {
  return s.replace(PY_STRIP, "");
}

/** Python's `float(text)`: the number, or `null` when Python raises `ValueError`. */
export function pyFloat(text: string): number | null {
  const s = pyStrip(text);
  const digits = "\\d(?:_?\\d)*";
  const num = new RegExp(`^[+-]?(?:(?:${digits})(?:\\.(?:${digits})?)?|\\.(?:${digits}))(?:[eE][+-]?${digits})?$`);
  if (num.test(s)) return Number(s.replace(/_/g, ""));
  const special = /^([+-]?)(inf|infinity|nan)$/i.exec(s);
  if (!special) return null;
  if (special[2]!.toLowerCase() === "nan") return NaN;
  return special[1] === "-" ? -Infinity : Infinity;
}

function checkMetricKey(key: string): string {
  if (!pyMatch(METRIC_KEY, key)) {
    throw new RunQueryError(`metric key ${pyRepr(key)} must match ${METRIC_KEY_PATTERN} (use labels, not '/' or '.', for qualifiers such as the split or the chain)`);
  }
  return key;
}

function checkParamKey(key: string): string {
  if (!pyMatch(PARAM_KEY, key)) throw new RunQueryError(`param key ${pyRepr(key)} must match ${PARAM_KEY_PATTERN}`);
  return key;
}

function checkTagKey(key: string): string {
  if (!pyMatch(TAG_KEY, key)) throw new RunQueryError(`tag key ${pyRepr(key)} must match ${TAG_KEY_PATTERN}`);
  if (key.startsWith(RESERVED_TAG_PREFIX)) throw new RunQueryError(`tag key ${pyRepr(key)} is reserved (prefix ${pyRepr(RESERVED_TAG_PREFIX)})`);
  return key;
}

const pySorted = (keys: Iterable<string>) => [...keys].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));

function checkLabels(labels: Map<string, string>): [string, string][] {
  if (labels.size > MAX_METRIC_LABELS) throw new RunQueryError(`a metric has at most ${MAX_METRIC_LABELS} labels, got ${labels.size}`);
  const out: [string, string][] = [];
  for (const k of pySorted(labels.keys())) {
    const v = labels.get(k)!;
    if (!pyMatch(LABEL_KEY, k)) throw new RunQueryError(`metric label key ${pyRepr(k)} must match ${METRIC_LABEL_KEY_PATTERN}`);
    if (!pyMatch(LABEL_VALUE, v)) throw new RunQueryError(`metric label value ${pyRepr(v)} (label ${pyRepr(k)}) must match ${METRIC_LABEL_VALUE_PATTERN}`);
    out.push([k, v]);
  }
  return out;
}

/** `parse_labels_key`: `"k1=v1,k2=v2"` to sorted pairs. */
export function parseLabelsKey(text: string): [string, string][] {
  if (!text) return [];
  const out = new Map<string, string>();
  for (const part of text.split(",")) {
    const eq = part.indexOf("=");
    if (eq < 0) throw new RunQueryError(`labels ${pyRepr(text)}: ${pyRepr(part)} is not 'key=value'`);
    const k = part.slice(0, eq);
    if (out.has(k)) throw new RunQueryError(`labels ${pyRepr(text)}: label ${pyRepr(k)} is repeated`);
    out.set(k, part.slice(eq + 1));
  }
  return checkLabels(out);
}

/** `labels_key`: the canonical `"k1=v1,k2=v2"` form of a label set. */
export function labelsKey(labels: Record<string, string> | readonly (readonly [string, string])[]): string {
  const pairs = Array.isArray(labels) ? (labels as readonly (readonly [string, string])[]) : Object.entries(labels as Record<string, string>);
  const map = new Map(pairs);
  return pySorted(map.keys())
    .map((k) => `${k}=${map.get(k)}`)
    .join(",");
}

/** `parse_run_ref`. */
export function parseRunRef(text: string): RunRef {
  const s = pyStrip(text);
  if (s === "name" || s === "created_at") return { kind: s, key: null, labels: [], agg: "last" };
  if (s.startsWith("metric.")) {
    const m = METRIC_REF.exec(s);
    if (!m) throw new RunQueryError(`run ref ${pyRepr(text)}: write a metric as 'metric.KEY', 'metric.KEY{label=value}' or 'metric.KEY.AGG'`);
    let key: string;
    let labels: [string, string][];
    try {
      key = checkMetricKey(m[1]!);
      labels = parseLabelsKey(m[2] ?? "");
    } catch (err) {
      throw new RunQueryError(`run ref ${pyRepr(text)}: ${(err as Error).message}`);
    }
    const agg = m[3] ?? "last";
    if (!(METRIC_AGGS as readonly string[]).includes(agg)) {
      throw new RunQueryError(`run ref ${pyRepr(text)}: aggregate ${pyRepr(agg)} is not one of ${METRIC_AGGS.join(", ")}`);
    }
    return { kind: "metric", key, labels, agg: agg as MetricAgg };
  }
  for (const [kind, check] of [
    ["param", checkParamKey],
    ["tag", checkTagKey],
  ] as const) {
    if (s.startsWith(`${kind}.`)) {
      try {
        return { kind, key: check(s.slice(kind.length + 1)), labels: [], agg: "last" };
      } catch (err) {
        throw new RunQueryError(`run ref ${pyRepr(text)}: ${(err as Error).message}`);
      }
    }
  }
  throw new RunQueryError(`run ref ${pyRepr(text)} must be 'metric.KEY', 'param.KEY', 'tag.KEY', 'name' or 'created_at'`);
}

/** `RunRef.text()`: the canonical form, and the key of `Run.columns`. */
export function refText(ref: RunRef): string {
  if (ref.kind === "name" || ref.kind === "created_at") return ref.kind;
  if (ref.kind !== "metric") return `${ref.kind}.${ref.key}`;
  let out = `metric.${ref.key}`;
  if (ref.labels.length) out += `{${labelsKey(ref.labels)}}`;
  if (ref.agg !== "last") out += `.${ref.agg}`;
  return out;
}

function opPosition(s: string): number {
  let depth = 0;
  for (let i = 0; i < s.length; i++) {
    const ch = s[i]!;
    if (ch === "{") depth += 1;
    else if (ch === "}") depth -= 1;
    else if (depth === 0 && "<>=!~".includes(ch)) return i;
  }
  return -1;
}

/** `parse_run_filter`. */
export function parseRunFilter(text: string): RunFilter {
  const i = opPosition(text);
  if (i < 0) throw new RunQueryError(`filter ${pyRepr(text)} has no operator (one of ${RUN_FILTER_OPS.join(" ")})`);
  const op = RUN_FILTER_OPS.find((o) => text.startsWith(o, i));
  if (!op) throw new RunQueryError(`filter ${pyRepr(text)}: the operator is not one of ${RUN_FILTER_OPS.join(" ")}`);
  let ref: RunRef;
  try {
    ref = parseRunRef(text.slice(0, i));
  } catch (err) {
    throw new RunQueryError(`filter ${pyRepr(text)}: ${(err as Error).message}`);
  }
  const raw = pyStrip(text.slice(i + op.length));
  if (!OPS_BY_KIND[ref.kind].has(op)) {
    if (ref.kind === "created_at") throw new RunQueryError(`filter ${pyRepr(text)}: 'created_at' is valid only in order_by and column`);
    const allowed = RUN_FILTER_OPS.filter((o) => OPS_BY_KIND[ref.kind].has(o)).join(" ");
    throw new RunQueryError(`filter ${pyRepr(text)}: a ${ref.kind} filter takes one of ${allowed}`);
  }
  const numeric = ref.kind === "metric" || (ref.kind === "param" && ORDER_OPS.has(op));
  if (!numeric) return { ref, op, value: raw };
  const value = pyFloat(raw);
  if (value === null) throw new RunQueryError(`filter ${pyRepr(text)}: ${pyRepr(raw)} is not a number`);
  if (!Number.isFinite(value)) throw new RunQueryError(`filter ${pyRepr(text)}: the value must be a finite number`);
  return { ref, op, value };
}

/** `parse_run_order`. */
export function parseRunOrder(text: string): RunOrder {
  const s = pyStrip(text);
  const descending = s.startsWith("-");
  try {
    return { ref: parseRunRef(descending ? s.slice(1) : s), descending };
  } catch (err) {
    throw new RunQueryError(`order_by ${pyRepr(text)}: ${(err as Error).message}`);
  }
}

/** The grammar error of `text` parsed with `parse`, or `null` when it is valid. */
export function grammarError(parse: (text: string) => unknown, text: string): string | null {
  try {
    parse(text);
    return null;
  } catch (err) {
    if (err instanceof RunQueryError) return err.message;
    throw err;
  }
}

/** The canonical `order_by` text (`-metric.rmse`). */
export function orderText(order: RunOrder): string {
  return `${order.descending ? "-" : ""}${refText(order.ref)}`;
}

/** The column header of a ref: the canonical text without the `metric.`/`param.`/`tag.` prefix. */
export function refLabel(ref: RunRef): string {
  const t = refText(ref);
  return ref.kind === "name" || ref.kind === "created_at" ? t : t.slice(ref.kind.length + 1);
}

/** Is `key` a metric key whose best value is the smallest (loss, error, rmse, ...)? */
export const LOWER_IS_BETTER_RE = /loss|error|rmse|mae|mape|smape|wape|crps|brier/;
