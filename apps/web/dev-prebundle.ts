/**
 * Dependencies the Vite dev server pre-bundles at start-up although no entry module imports
 * them. React Flow loads only with the workflow editor and run graph (a lazy chunk), so Vite
 * found it on first use, optimised it in a second pass and reloaded the page; a page that had
 * already started rendering then mixed two copies of React ("Invalid hook call" in
 * ReactFlowProvider). Found by the M3.5 live-stack browser run. Production builds are not
 * affected (Rollup bundles one React); the entry-bundle budget still keeps these lazy there.
 */
export const DEV_PREBUNDLE: readonly string[] = ["@xyflow/react"];
