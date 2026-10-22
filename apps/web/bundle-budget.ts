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
  /** File names of chunks this chunk loads with `import()` (not part of its static closure). */
  dynamicImports?: string[];
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
  /**
   * Budgets for lazily loaded app chunks (the share link app, the client portal). For the first
   * chunk whose file name matches `match`:
   * - `forbidden` is checked over the chunk and its whole static import closure (the entry
   *   included when the chunk imports from it), so nothing the closure pulls in can hide a module;
   * - `maxBytes` bounds what loading the chunk adds: its static closure minus what is already
   *   loaded when it can be requested (the entries' closure, which `maxEntryBytes` bounds, and
   *   the closure every chunk that loads it with `import()` has in common).
   * A budget whose chunk is missing is a problem too, so a renamed chunk cannot skip its check.
   */
  chunkBudgets?: ChunkBudget[];
}

export interface ChunkBudget {
  match: RegExp;
  maxBytes: number;
  forbidden: RegExp[];
  /**
   * The budget applies once a chunk bundles a module matching this (the lazy page or panel
   * the budget is for). Until then (a feature not mounted yet) a missing chunk is fine; once
   * the module is in the build, a missing chunk is a problem, because the module was merged
   * into another chunk (often the entry) instead of loading lazily.
   */
  requiredWhen?: RegExp;
}

/** A module id inside an npm package (a flat or a pnpm `.pnpm/<pkg>@<v>/node_modules/` layout). */
export function pkgPattern(names: string): RegExp {
  return new RegExp(`node_modules\\/(?:\\.pnpm\\/[^/]+\\/node_modules\\/)?(?:${names})\\/`);
}

/**
 * v0.18 (plan D27, I-8): the animation library (`motion` re-exports framer-motion, motion-dom
 * and motion-utils) and the app's one wrapper around it. Lazy signed-in chunks only.
 */
export const MOTION_PATTERNS: RegExp[] = [pkgPattern("motion|framer-motion|motion-dom|motion-utils"), /\/src\/lib\/motion\.tsx$/];

/**
 * v0.18: what a share link and the client portal must never load: the animation library, the
 * onboarding, library and extension features, the interactive controls and the illustrations.
 */
export const SIGNED_IN_ONLY: RegExp[] = [
  ...MOTION_PATTERNS,
  /\/src\/features\/onboarding\//,
  /\/src\/features\/library\//,
  /\/src\/features\/extend\//,
  /\/src\/components\/interactive\//,
  /\/src\/components\/illustrations\//,
];

/** The chunk and every chunk it reaches through static imports. */
export function staticClosure(chunks: ChunkInfo[], fileName: string): Set<string> {
  const byName = new Map(chunks.map((c) => [c.fileName, c]));
  const seen = new Set<string>();
  const stack = [fileName];
  while (stack.length) {
    const name = stack.pop()!;
    if (seen.has(name)) continue;
    seen.add(name);
    for (const dep of byName.get(name)?.imports ?? []) stack.push(dep);
  }
  return seen;
}

/** What is loaded before `fileName` can be requested: the entries' closure and its dynamic importers' common closure. */
export function loadedBefore(chunks: ChunkInfo[], fileName: string): Set<string> {
  const loaded = new Set<string>();
  for (const entry of chunks.filter((c) => c.isEntry)) for (const name of staticClosure(chunks, entry.fileName)) loaded.add(name);
  const closures = chunks.filter((c) => c.dynamicImports?.includes(fileName)).map((c) => staticClosure(chunks, c.fileName));
  const [first, ...rest] = closures;
  for (const name of first ?? []) if (rest.every((closure) => closure.has(name))) loaded.add(name);
  return loaded;
}

/** Problems with the {@link BudgetOptions.chunkBudgets}; an empty list means every chunk is within budget. */
export function checkChunkBudgets(chunks: ChunkInfo[], budgets: ChunkBudget[]): string[] {
  const byName = new Map(chunks.map((c) => [c.fileName, c]));
  const problems: string[] = [];
  for (const budget of budgets) {
    const root = chunks.find((c) => budget.match.test(c.fileName));
    if (!root) {
      if (budget.requiredWhen) {
        const pattern = budget.requiredWhen;
        const holder = chunks.find((c) => c.moduleIds.some((id) => pattern.test(id)));
        if (holder) {
          const id = holder.moduleIds.find((m) => pattern.test(m));
          problems.push(`${id} is bundled in ${holder.fileName}, but no chunk matches ${budget.match}; load it with import() in its own chunk`);
        }
        continue;
      }
      problems.push(`no chunk matches ${budget.match}; rename the budget with the chunk`);
      continue;
    }
    const before = loadedBefore(chunks, root.fileName);
    let added = 0;
    for (const name of staticClosure(chunks, root.fileName)) {
      const c = byName.get(name);
      if (!c) continue;
      if (!before.has(name) || name === root.fileName) added += c.size;
      for (const pattern of budget.forbidden) {
        const hit = c.moduleIds.find((id) => pattern.test(id));
        if (hit) {
          problems.push(
            `${c.fileName} is loaded statically by ${root.fileName} but bundles ${hit}; ` +
              "keep it out of that chunk's imports (or load it with import())",
          );
        }
      }
    }
    if (added > budget.maxBytes) {
      problems.push(`${root.fileName} and the static imports it adds are ${added} bytes, over the ${budget.maxBytes}-byte budget`);
    }
  }
  return problems;
}

/** Returns human-readable problems; an empty list means the build is within budget. */
export function checkEntryBudget(chunks: ChunkInfo[], opts: BudgetOptions): string[] {
  const byName = new Map(chunks.map((c) => [c.fileName, c]));
  const entries = chunks.filter((c) => c.isEntry);
  if (entries.length === 0) return ["no entry chunk was produced"];
  const problems: string[] = [];
  for (const entry of entries) {
    const seen = staticClosure(chunks, entry.fileName);
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
  return [...problems, ...checkChunkBudgets(chunks, opts.chunkBudgets ?? [])];
}

/** Vite plugin: fails `vite build` when {@link checkEntryBudget} (entry and chunk budgets) reports problems. */
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
          dynamicImports: out.dynamicImports,
          moduleIds: Object.keys(out.modules),
          size: Buffer.byteLength(out.code, "utf8"),
        });
      }
      const problems = checkEntryBudget(chunks, opts);
      if (problems.length) this.error(`bundle budget exceeded:\n  - ${problems.join("\n  - ")}`);
    },
  };
}
