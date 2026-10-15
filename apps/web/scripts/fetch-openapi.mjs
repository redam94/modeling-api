// Replace openapi.json with the running server's document, then regenerate the types:
//   pnpm --filter @mapi/web openapi:fetch && pnpm --filter @mapi/web openapi:types
// Uses VITE_API_BASE (default http://127.0.0.1:8000).
import { writeFile } from "node:fs/promises";

const base = (process.env.VITE_API_BASE ?? "http://127.0.0.1:8000").replace(/\/+$/, "");
const url = `${base}/openapi.json`;
const res = await fetch(url, { headers: { Accept: "application/json" } });
if (!res.ok) {
  console.error(`GET ${url} failed with HTTP ${res.status}`);
  process.exit(1);
}
const doc = await res.json();
if (typeof doc !== "object" || doc === null || typeof doc.openapi !== "string" || !doc.paths) {
  console.error(`${url} is not an OpenAPI document`);
  process.exit(1);
}
const out = new URL("../openapi.json", import.meta.url);
await writeFile(out, `${JSON.stringify(doc, null, 2)}\n`, "utf8");
console.log(`wrote ${out.pathname} (OpenAPI ${doc.openapi}, ${Object.keys(doc.paths).length} paths)`);
