/**
 * The mock API's stand-in for `mapi_spec.workflow.validate_workflow` (mock mode, component
 * tests and the smoke suite only; the app always asks the server). It reports the same codes
 * and JSON pointers for the common mistakes - unknown implementations, ports, port types,
 * missing inputs, parameters, cycles and branch conditions - and the unit tests replay the
 * server's answers for broken documents (`workflows.gen.json` → `validation_vectors`).
 *
 * Conditions are tokenised and parsed for syntax and references only; nothing is evaluated.
 */
import type { Issue } from "@/api/types";
import { bindingList, expressionTexts, implId, nodeDependencies, parseBinding, parseWhen, topologicalOrder } from "@/features/workflows/doc";
import { bindingType, inputPorts, type Catalog } from "@/features/workflows/ports";
import type { Json, JsonObject, WorkflowDoc, WorkflowValidation } from "@/features/workflows/types";
import { BRANCH_NODE, SUBWORKFLOW_NODE } from "@/features/workflows/types";

type Code = Issue["code"];

function issue(code: Code, pointer: string, message: string, severity: Issue["severity"] = "error"): Issue {
  return { code, pointer, message, severity, context: {} };
}

// ------------------------------------------------------------------- expression syntax

const KEYWORDS = new Set(["and", "or", "not", "in", "true", "false", "null"]);
const FUNCTIONS = new Set(["abs", "min", "max", "len", "round", "any", "all", "exists", "coalesce", "if"]);

interface Token {
  kind: "num" | "str" | "id" | "op" | "kw" | "end";
  text: string;
}

function tokenize(src: string): Token[] {
  const out: Token[] = [];
  let i = 0;
  while (i < src.length) {
    const c = src[i]!;
    if (/\s/.test(c)) {
      i++;
      continue;
    }
    const num = /^(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?/.exec(src.slice(i));
    if (num) {
      out.push({ kind: "num", text: num[0] });
      i += num[0].length;
      continue;
    }
    if (c === "'" || c === '"') {
      const end = src.indexOf(c, i + 1);
      if (end < 0) throw new Error("unterminated text");
      out.push({ kind: "str", text: src.slice(i + 1, end) });
      i = end + 1;
      continue;
    }
    const id = /^[A-Za-z_][A-Za-z0-9_]*/.exec(src.slice(i));
    if (id) {
      out.push({ kind: KEYWORDS.has(id[0]) ? "kw" : "id", text: id[0] });
      i += id[0].length;
      continue;
    }
    const op = /^(?:==|!=|<=|>=|[<>+\-*/%()[\],.])/.exec(src.slice(i));
    if (op) {
      out.push({ kind: "op", text: op[0] });
      i += op[0].length;
      continue;
    }
    throw new Error(`unexpected character ${JSON.stringify(c)}`);
  }
  out.push({ kind: "end", text: "" });
  return out;
}

/** References `nodes.<id>.<field>` (and params/inputs/run) found while parsing. */
interface Ref {
  root: string;
  path: string[];
}

class Parser {
  private i = 0;
  readonly refs: Ref[] = [];
  constructor(private readonly t: Token[]) {}

  private peek(): Token {
    return this.t[this.i]!;
  }
  private take(): Token {
    return this.t[this.i++]!;
  }
  private is(text: string): boolean {
    const p = this.peek();
    return (p.kind === "op" || p.kind === "kw") && p.text === text;
  }
  private expect(text: string): void {
    if (!this.is(text)) throw new Error(`expected "${text}" but found ${this.peek().text ? `"${this.peek().text}"` : "the end"}`);
    this.i++;
  }

  parse(): void {
    this.or();
    if (this.peek().kind !== "end") throw new Error(`unexpected "${this.peek().text}"`);
  }
  private or(): void {
    this.and();
    while (this.is("or")) {
      this.take();
      this.and();
    }
  }
  private and(): void {
    this.not();
    while (this.is("and")) {
      this.take();
      this.not();
    }
  }
  private not(): void {
    if (this.is("not")) {
      this.take();
      this.not();
      return;
    }
    this.cmp();
  }
  private cmpOp(): boolean {
    if (["==", "!=", "<", "<=", ">", ">=", "in"].some((o) => this.is(o))) {
      this.take();
      return true;
    }
    if (this.is("not") && this.t[this.i + 1]?.text === "in") {
      this.i += 2;
      return true;
    }
    return false;
  }
  private cmp(): void {
    this.add();
    if (this.cmpOp()) {
      this.add();
      if (this.cmpOp()) throw new Error("comparisons cannot be chained (write a < b and b < c)");
    }
  }
  private add(): void {
    this.mul();
    while (this.is("+") || this.is("-")) {
      this.take();
      this.mul();
    }
  }
  private mul(): void {
    this.unary();
    while (this.is("*") || this.is("/") || this.is("%")) {
      this.take();
      this.unary();
    }
  }
  private unary(): void {
    if (this.is("-")) {
      this.take();
      this.unary();
      return;
    }
    this.postfix();
  }
  private postfix(): void {
    const ref = this.primary();
    while (this.is(".") || this.is("[")) {
      if (this.is(".")) {
        this.take();
        const name = this.take();
        if (name.kind !== "id" && name.kind !== "kw") throw new Error("expected a name after '.'");
        ref?.path.push(name.text);
      } else {
        this.take();
        this.or();
        this.expect("]");
      }
    }
    if (ref) this.refs.push(ref);
  }
  private primary(): Ref | null {
    const tok = this.take();
    if (tok.kind === "num" || tok.kind === "str") return null;
    if (tok.kind === "kw" && ["true", "false", "null"].includes(tok.text)) return null;
    if (tok.kind === "op" && tok.text === "(") {
      this.or();
      this.expect(")");
      return null;
    }
    if (tok.kind === "op" && tok.text === "[") {
      if (!this.is("]")) {
        this.or();
        while (this.is(",")) {
          this.take();
          this.or();
        }
      }
      this.expect("]");
      return null;
    }
    if (tok.kind === "id") {
      if (this.is("(")) {
        if (!FUNCTIONS.has(tok.text)) throw new Error(`unknown function ${tok.text}`);
        this.take();
        if (!this.is(")")) {
          this.or();
          while (this.is(",")) {
            this.take();
            this.or();
          }
        }
        this.expect(")");
        return null;
      }
      if (!["nodes", "params", "inputs", "run"].includes(tok.text)) throw new Error(`unknown name ${tok.text} (use nodes., params., inputs. or run.)`);
      return { root: tok.text, path: [] };
    }
    throw new Error(tok.kind === "end" ? "the condition ends too early" : `unexpected "${tok.text}"`);
  }
}

/** Syntax and references of one expression; `null` when it is fine. */
export function expressionProblem(text: string, doc: WorkflowDoc, catalog: Catalog, nodeId: string): string | null {
  if (!text.trim()) return "the condition is empty";
  if (text.length > 2000) return "the condition is longer than 2000 characters";
  let parser: Parser;
  try {
    parser = new Parser(tokenize(text));
    parser.parse();
  } catch (e) {
    return e instanceof Error ? e.message : String(e);
  }
  const ancestors = new Set<string>();
  const deps = nodeDependencies(doc);
  const stack = [...(deps.get(nodeId) ?? [])];
  while (stack.length) {
    const x = stack.pop()!;
    if (!ancestors.has(x)) {
      ancestors.add(x);
      stack.push(...(deps.get(x) ?? []));
    }
  }
  for (const ref of parser.refs) {
    if (ref.root === "params" && ref.path[0] && !(ref.path[0] in (doc.params ?? {}))) return `params.${ref.path[0]}: the workflow has no parameter ${ref.path[0]}`;
    if (ref.root === "inputs" && ref.path[0] && !(ref.path[0] in (doc.inputs ?? {}))) return `inputs.${ref.path[0]}: the workflow has no input ${ref.path[0]}`;
    if (ref.root !== "nodes") continue;
    const [id, field] = ref.path;
    if (!id) return "nodes needs a node id (nodes.<id>.<field>)";
    const node = doc.nodes.find((n) => n.id === id);
    if (!node) return `nodes.${id}: there is no node ${id}`;
    if (!ancestors.has(id) && id !== nodeId) return `nodes.${id}: ${id} does not run before ${nodeId}`;
    const impl = catalog.get(implId(node.use));
    if (field && impl && Object.keys(impl.result_fields ?? {}).length && !(field in (impl.result_fields ?? {}))) {
      return `nodes.${id}.${field}: ${impl.id} results have no field '${field}'`;
    }
  }
  return null;
}

// ------------------------------------------------------------------- parameters

function typeOk(type: unknown, v: Json): boolean {
  const types = Array.isArray(type) ? type : [type];
  return types.some((t) => {
    switch (t) {
      case "number":
        return typeof v === "number";
      case "integer":
        return typeof v === "number" && Number.isInteger(v);
      case "string":
        return typeof v === "string";
      case "boolean":
        return typeof v === "boolean";
      case "array":
        return Array.isArray(v);
      case "object":
        return v !== null && typeof v === "object" && !Array.isArray(v);
      case "null":
        return v === null;
      default:
        return true;
    }
  });
}

function isPlaceholder(v: Json): boolean {
  return v !== null && typeof v === "object" && !Array.isArray(v) && Object.keys(v).length === 1 && ("$param" in v || "$expr" in v);
}

function schemaProblem(schema: JsonObject, v: Json): string | null {
  if (isPlaceholder(v)) return null;
  if (Array.isArray(schema.anyOf)) {
    return (schema.anyOf as JsonObject[]).some((s) => schemaProblem(s, v) === null) ? null : "does not match any allowed form";
  }
  if (schema.type !== undefined && !typeOk(schema.type, v)) return `${JSON.stringify(v)} is not of type '${JSON.stringify(schema.type)}'`;
  if (Array.isArray(schema.enum) && !schema.enum.some((e) => JSON.stringify(e) === JSON.stringify(v))) return `${JSON.stringify(v)} is not one of ${JSON.stringify(schema.enum)}`;
  if (typeof v === "number") {
    if (typeof schema.minimum === "number" && v < schema.minimum) return `${v} is less than the minimum of ${schema.minimum}`;
    if (typeof schema.maximum === "number" && v > schema.maximum) return `${v} is greater than the maximum of ${schema.maximum}`;
    if (typeof schema.exclusiveMinimum === "number" && v <= schema.exclusiveMinimum) return `${v} is less than or equal to the minimum of ${schema.exclusiveMinimum}`;
    if (typeof schema.exclusiveMaximum === "number" && v >= schema.exclusiveMaximum) return `${v} is greater than or equal to the maximum of ${schema.exclusiveMaximum}`;
  }
  return null;
}

// ------------------------------------------------------------------- the validator

export function mockValidateWorkflow(doc: WorkflowDoc, catalog: Catalog): WorkflowValidation {
  const issues: Issue[] = [];
  const ids = new Set(doc.nodes.map((n) => n.id));
  doc.nodes.forEach((n, i) => {
    const base = `/nodes/${i}`;
    const impl = catalog.get(implId(n.use));
    if (!impl) {
      issues.push(issue("NODE_UNKNOWN", `${base}/use`, `node '${n.id}' uses '${implId(n.use)}', which is not in the node catalog`));
      return;
    }
    const when = parseWhen(n.when);
    if (n.when && !when) issues.push(issue("WORKFLOW_INVALID", `${base}/when`, "when must be '<branch node id>.then' or '.else'"));
    if (when) {
      const b = doc.nodes.find((x) => x.id === when.branch);
      if (!b || implId(b.use) !== BRANCH_NODE) issues.push(issue("WORKFLOW_INVALID", `${base}/when`, `node '${n.id}' uses when: ${when.branch}, but '${when.branch}' is not a branch node`));
    }
    const ports = inputPorts(n, catalog);
    const isSub = implId(n.use) === SUBWORKFLOW_NODE;
    for (const [port, b] of Object.entries(n.inputs ?? {})) {
      const spec = ports[port];
      if (!spec && !isSub) {
        issues.push(issue("WORKFLOW_INVALID", `${base}/inputs/${port}`, `node '${n.id}' (${impl.id}) has no input '${port}'`));
        continue;
      }
      const list = bindingList(b);
      list.forEach((s, k) => {
        const pointer = typeof b === "string" ? `${base}/inputs/${port}` : `${base}/inputs/${port}/${k}`;
        const ref = parseBinding(s);
        if (!ref) {
          issues.push(issue("WORKFLOW_INVALID", pointer, `${s} is not inputs.<name> or <node>.<port>`));
          return;
        }
        if (ref.kind === "node" && !ids.has(ref.node)) {
          issues.push(issue("WORKFLOW_INVALID", pointer, `${s}: there is no node '${ref.node}'`));
          return;
        }
        const t = bindingType(doc, s, catalog);
        if (!t) {
          issues.push(issue("WORKFLOW_INVALID", pointer, `${s}: the type of this binding is unknown`));
          return;
        }
        if (spec && t !== spec.type) issues.push(issue("WORKFLOW_INVALID", pointer, `input '${port}' must be ${spec.type}, but ${s} is ${t}`));
      });
    }
    for (const [port, spec] of Object.entries(ports)) {
      if (spec.required && !bindingList(n.inputs?.[port]).length) {
        issues.push(issue("WORKFLOW_INVALID", `${base}/inputs`, `node '${n.id}' needs input '${port}' (${spec.type})`));
      }
    }
    const schema = (impl.params_schema ?? {}) as JsonObject;
    const props = (schema.properties ?? {}) as Record<string, JsonObject>;
    const w = n.with ?? {};
    for (const key of (schema.required as string[] | undefined) ?? []) {
      if (!(key in w)) issues.push(issue("WORKFLOW_INVALID", `${base}/with`, `'${key}' is a required property`));
    }
    for (const [key, v] of Object.entries(w)) {
      const p = props[key];
      if (!p) {
        if (schema.additionalProperties === false) issues.push(issue("WORKFLOW_INVALID", `${base}/with/${key}`, `Additional properties are not allowed ('${key}' was unexpected)`));
        continue;
      }
      if (impl.id === BRANCH_NODE && key === "condition") continue;
      const problem = schemaProblem(p, v);
      if (problem) issues.push(issue("WORKFLOW_INVALID", `${base}/with/${key}`, problem));
    }
    for (const { path, text } of expressionTexts(n)) {
      const problem = expressionProblem(text, doc, catalog, n.id);
      if (problem) issues.push(issue("EXPRESSION_INVALID", `${base}/with/${path.join("/")}`, problem));
    }
  });
  const order = topologicalOrder(doc);
  if (!order) {
    const deps = nodeDependencies(doc);
    const cyc = doc.nodes.map((n) => n.id).find((id) => {
      const seen = new Set<string>();
      const stack = [...(deps.get(id) ?? [])];
      while (stack.length) {
        const x = stack.pop()!;
        if (x === id) return true;
        if (!seen.has(x)) {
          seen.add(x);
          stack.push(...(deps.get(x) ?? []));
        }
      }
      return false;
    });
    issues.push(issue("WORKFLOW_INVALID", "/nodes", `the workflow has a cycle through ${cyc ?? "some nodes"}`));
  }
  for (const [name, b] of Object.entries(doc.outputs ?? {})) {
    const ref = parseBinding(b);
    if (!ref || (ref.kind === "node" && !ids.has(ref.node))) issues.push(issue("WORKFLOW_INVALID", `/outputs/${name}`, `${b} names no node output`));
  }
  const errors = issues.filter((x) => x.severity === "error");
  const resolved: Record<string, string> = {};
  for (const n of doc.nodes) {
    const impl = catalog.get(implId(n.use));
    if (impl) resolved[n.id] = `${impl.id}@${impl.version ?? "1.0.0"}`;
  }
  return { valid: errors.length === 0, issues, order: order ?? [], resolved };
}
