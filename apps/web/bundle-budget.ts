/**
 * Build-time guard on what every page downloads before first paint.
 *
 * The entry chunk plus everything it imports statically is what `index.html` modulepreloads.
 * Heavy, page-specific libraries (Monaco, monaco-yaml, Apache Arrow) must stay behind dynamic
 * imports, and that closure must stay under a byte budget. A misplaced `manualChunks` rule once
 * pulled Vite's shared preload helper into the Monaco chunk, which made the entry import 3 MB of
 * editor statically; this check fails the build if anything like that happens again.
 */
import type { Plugin } from "vite";

export interface ChunkInfo {
  fileName: string;
  isEntry: boolean;
  /** File names of chunks this chunk imports statically. */
  imports: string[];
  /** Module ids bundled into this chunk. */
  moduleIds: string[];
  /** Size of the chunk code in bytes. */
  size: number;
}

export interface BudgetOptions {
  /** Upper bound, in bytes, for the entry chunk plus its static import closure. */
  maxEntryBytes: number;
  /** Module ids that must never be in the entry's static import closure. */
  forbidden: RegExp[];
}

/** Returns human-readable problems; an empty list means the build is within budget. */
export function checkEntryBudget(chunks: ChunkInfo[], opts: BudgetOptions): string[] {
  const byName = new Map(chunks.map((c) => [c.fileName, c]));
  const entries = chunks.filter((c) => c.isEntry);
  if (entries.length === 0) return ["no entry chunk was produced"];
  const problems: string[] = [];
  for (const entry of entries) {
    const seen = new Set<string>();
    const stack = [entry.fileName];
    while (stack.length) {
      const name = stack.pop()!;
      if (seen.has(name)) continue;
      seen.add(name);
      for (const dep of byName.get(name)?.imports ?? []) stack.push(dep);
    }
    let total = 0;
    for (const name of seen) {
      const c = byName.get(name);
      if (!c) continue;
      total += c.size;
      for (const pattern of opts.forbidden) {
        const hit = c.moduleIds.find((id) => pattern.test(id));
        if (hit) {
          problems.push(
            `${c.fileName} is loaded statically by entry ${entry.fileName} but bundles ${hit}; ` +
              "import it dynamically (lazy route / import()) instead",
          );
        }
      }
    }
    if (total > opts.maxEntryBytes) {
      problems.push(
        `entry ${entry.fileName} and its static imports are ${total} bytes, over the ${opts.maxEntryBytes}-byte budget`,
      );
    }
  }
  return problems;
}

/** Vite plugin: fails `vite build` when {@link checkEntryBudget} reports problems. */
export function bundleBudget(opts: BudgetOptions): Plugin {
  return {
    name: "mapi-bundle-budget",
    apply: "build",
    // Workers are built by a separate Rollup pass with their own (worker) entry chunks.
    applyToEnvironment: (env) => env.name === "client",
    generateBundle(_options, bundle) {
      const chunks: ChunkInfo[] = [];
      for (const out of Object.values(bundle)) {
        if (out.type !== "chunk") continue;
        chunks.push({
          fileName: out.fileName,
          isEntry: out.isEntry,
          imports: out.imports,
          moduleIds: Object.keys(out.modules),
          size: Buffer.byteLength(out.code, "utf8"),
        });
      }
      const problems = checkEntryBudget(chunks, opts);
      if (problems.length) this.error(`bundle budget exceeded:\n  - ${problems.join("\n  - ")}`);
    },
  };
}
