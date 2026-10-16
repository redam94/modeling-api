/**
 * Dependencies the Vite dev server pre-bundles at start-up although its import scan does not
 * find them. Vite then discovers them on first use, optimises them in a second pass, changes
 * the hash of every pre-bundled chunk and reloads the page; a page that had started rendering
 * mixed two copies of React ("Invalid hook call" in ReactFlowProvider), and a lazy route
 * loading meanwhile failed ("Failed to fetch dynamically imported module .../yaml-….js").
 * Found by the live-stack browser runs of M3.5. Production builds are not affected (Rollup
 * bundles one copy); the entry-bundle budget still keeps these lazy there.
 *
 * - React Flow loads only with the workflow editor and the run graph.
 * - Imports of `?worker` modules (the monaco-yaml language worker) are never scanned.
 */
export const DEV_PREBUNDLE: readonly string[] = ["@xyflow/react", "monaco-yaml/yaml.worker.js"];
