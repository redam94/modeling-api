/**
 * M4: the nodes of a tree dataset version. A tree of nodes on the left (from `tree_schema`),
 * and the selected node on the right: its schema, the summary of its coordinates
 * (`summary.json` `nodes`) and, for the node the ingest previewed, its sample rows.
 *
 * The tree follows the WAI-ARIA tree pattern: one tab stop (the selected node), Up/Down move
 * to the previous/next node, Home/End to the first/last, Right to the first child and Left to
 * the parent; selection follows focus. Every node is always expanded (`aria-expanded`).
 */
import { Link } from "@tanstack/react-router";
import { FolderTree, Telescope } from "lucide-react";
import { useRef, useState, type KeyboardEvent } from "react";

import type { DataSchema, DatasetVersion } from "@/api/types";
import { ErrorNotice, LoadingRows, Mono } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

import { useNodeSummaries } from "./api";
import { defaultNode, nodeName, nodeParent, ROOT, treeRows, type TreeRow } from "./tree";
import { CoordList, PreviewPanel, SchemaTable } from "./version-views";

function varCount(schema: DataSchema): number {
  return Object.keys(schema.vars ?? {}).length;
}

/** The row a tree key moves to from row `i` (`null`: the key is not a tree key; `-1`: nowhere). */
export function treeKeyTarget(rows: TreeRow[], i: number, key: string): number | null {
  const row = rows[i];
  if (!row) return null;
  switch (key) {
    case "ArrowDown":
      return Math.min(i + 1, rows.length - 1);
    case "ArrowUp":
      return Math.max(i - 1, 0);
    case "Home":
      return 0;
    case "End":
      return rows.length - 1;
    case "ArrowRight":
      return rows.findIndex((r) => nodeParent(r.path) === row.path);
    case "ArrowLeft": {
      const parent = nodeParent(row.path);
      return parent === null ? -1 : rows.findIndex((r) => r.path === parent);
    }
    default:
      return null;
  }
}

export function TreeBrowser({ version, nodes }: { version: DatasetVersion; nodes: Record<string, DataSchema> }) {
  const rows = treeRows(nodes);
  const fallback = defaultNode(nodes);
  const [selected, setSelected] = useState(fallback);
  const node = nodes[selected] ? selected : fallback;
  const schema = nodes[node] ?? {};
  const summaries = useNodeSummaries(version, true);
  const summary = summaries.data?.[node];
  const items = useRef<(HTMLButtonElement | null)[]>([]);
  const onKey = (e: KeyboardEvent, i: number) => {
    const next = treeKeyTarget(rows, i, e.key);
    if (next === null) return;
    e.preventDefault();
    const row = rows[next];
    if (!row) return;
    setSelected(row.path);
    items.current[next]?.focus();
  };
  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-5 md:grid-cols-[14rem_minmax(0,1fr)]">
      <nav aria-label="Nodes" className="grid content-start gap-0.5">
        <p className="flex items-center gap-1.5 px-2 pb-1 text-xs font-medium text-muted-foreground">
          <FolderTree className="size-3.5" /> {rows.length} {rows.length === 1 ? "node" : "nodes"}
        </p>
        <ul role="tree" aria-label="Tree of nodes" className="grid gap-0.5">
          {rows.map((r, i) => (
            <li key={r.path} role="none">
              <button
                ref={(el) => {
                  items.current[i] = el;
                }}
                type="button"
                role="treeitem"
                aria-selected={r.path === node}
                aria-level={r.depth + 1}
                aria-setsize={rows.filter((x) => nodeParent(x.path) === nodeParent(r.path)).length}
                aria-posinset={rows.filter((x) => nodeParent(x.path) === nodeParent(r.path)).findIndex((x) => x.path === r.path) + 1}
                aria-expanded={rows.some((x) => nodeParent(x.path) === r.path) ? true : undefined}
                tabIndex={r.path === node ? 0 : -1}
                onClick={() => setSelected(r.path)}
                onKeyDown={(e) => onKey(e, i)}
                className={cn("flex w-full items-center justify-between gap-2 rounded-md py-1.5 pr-2 text-left text-[13px] hover:bg-accent", r.path === node && "bg-accent font-medium")}
                style={{ paddingLeft: `${0.5 + r.depth * 0.9}rem` }}
                title={r.path}
              >
                <Mono className="truncate">{r.path === ROOT ? "/" : nodeName(r.path)}</Mono>
                <span className="shrink-0 text-xs text-muted-foreground tabular">{varCount(r.schema) ? `${varCount(r.schema)} var${varCount(r.schema) === 1 ? "" : "s"}` : "group"}</span>
              </button>
            </li>
          ))}
        </ul>
      </nav>
      <section aria-label={`Node ${node}`} className="grid min-w-0 content-start gap-5">
        <div className="flex flex-wrap items-center gap-2">
          <Mono className="text-[15px] font-semibold break-all">{node}</Mono>
          {node === fallback ? <Badge variant="secondary">default node</Badge> : null}
          {!varCount(schema) ? <Badge variant="outline">group without variables</Badge> : null}
          {varCount(schema) && version.status === "ready" ? (
            <Button asChild size="sm" variant="outline" className="ml-auto">
              <Link to="/datasets/$datasetId/versions/$version/explore" params={{ datasetId: version.dataset_id, version: String(version.version) }} search={{ node }}>
                <Telescope /> Explore this node
              </Link>
            </Button>
          ) : null}
        </div>
        {varCount(schema) || Object.keys(schema.dims ?? {}).length ? (
          <SchemaTable schema={schema} />
        ) : (
          <p className="text-muted-foreground">This node only groups the nodes below it. It has no dims or variables of its own.</p>
        )}
        <div className="grid gap-2">
          <h3 className="text-[13px] font-semibold">Coordinates</h3>
          {summaries.isPending ? (
            <LoadingRows rows={2} />
          ) : summaries.isError ? (
            <ErrorNotice error={summaries.error} title="The app could not load the node summaries" />
          ) : summary && Object.keys(summary.coords ?? {}).length ? (
            <CoordList coords={summary.coords ?? {}} />
          ) : (
            <p className="text-muted-foreground">This node has no coordinates of its own.</p>
          )}
        </div>
        <div className="grid gap-2">
          <h3 className="text-[13px] font-semibold">Preview</h3>
          {node === fallback ? (
            <PreviewPanel version={version} />
          ) : (
            <p className="text-muted-foreground">
              The ingest previews the default node (<Mono>{fallback}</Mono>). To see the values of this node, explore it.
            </p>
          )}
        </div>
      </section>
    </div>
  );
}
