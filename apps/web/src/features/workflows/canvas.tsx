/**
 * The visual DAG (React Flow, lazy-loaded with this module). Every edit goes through the
 * document functions: dragging an output onto an input binds it (typed ports; an incompatible
 * drop is refused with the reason), dragging a branch arm onto a node's top edge gates it,
 * Delete removes the selected nodes or edges, and nodes dropped from the palette are added
 * where they land. Node positions are a per-browser view preference, never part of the
 * document. In run mode the boxes show each node's status and open its details.
 */
import "@xyflow/react/dist/style.css";

import {
  applyNodeChanges,
  Background,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Connection,
  type Edge,
  type Node,
  type NodeChange,
  type NodeProps,
  type OnConnectEnd,
} from "@xyflow/react";
import { AlertTriangle, GitBranch, Lock } from "lucide-react";
import { memo, useCallback, useEffect, useMemo, useState, type DragEvent } from "react";

import type { Issue } from "@/api/types";
import { useResolvedTheme } from "@/hooks/use-theme";
import { cn } from "@/lib/utils";

import { buildCanvasEdges, buildCanvasNodes, checkCanvasConnection, GATE_HANDLE, REF_IN, REF_OUT, type CanvasEdgeModel, type CanvasNodeData } from "./canvas-model";
import { PALETTE_MIME } from "./canvas-constants";
import type { Point } from "./layout";
import { PORT_TYPE_META, type Catalog, type SubworkflowIndex } from "./ports";
import { KIND_COLOR, KIND_LABEL, NODE_STATUS_META, TONE_NODE } from "./status";
import type { NodeRunStatus, WorkflowDoc } from "./types";
import { INPUTS_NODE } from "./types";


type FlowNode = Node<CanvasNodeData, "workflow">;

export type CanvasEdit =
  | { type: "connect"; binding: string; target: string; port: string; mode: "replace" | "append" }
  | { type: "gate"; target: string; when: string | null }
  | { type: "disconnect"; target: string; port: string; binding: string }
  | { type: "remove-nodes"; ids: string[] }
  | { type: "add-node"; impl: string; position: Point };

export interface WorkflowCanvasProps {
  doc: WorkflowDoc;
  catalog: Catalog;
  subs?: SubworkflowIndex;
  positions: Record<string, Point>;
  issues?: Map<string, Issue[]>;
  statuses?: Map<string, NodeRunStatus>;
  selected: string | null;
  readOnly?: boolean;
  onSelect: (id: string | null) => void;
  onEdit?: (edit: CanvasEdit) => void;
  onMove?: (id: string, position: Point) => void;
  onRefused?: (reason: string) => void;
  height?: string;
  label: string;
}

const handleClass = "!size-2.5 !border-2 !border-card";

function PortRow({ side, id, name, type, required, multiple }: { side: "in" | "out"; id: string; name: string; type: string; required?: boolean; multiple?: boolean }) {
  const meta = PORT_TYPE_META[type as keyof typeof PORT_TYPE_META];
  const color = meta?.color ?? "var(--muted-foreground)";
  const desc = `${side === "in" ? "Input" : "Output"} ${name}: ${meta?.label ?? type}${multiple ? ", takes several" : ""}${required === false ? ", optional" : ""}`;
  return (
    <div className={cn("relative flex h-5 items-center text-[11px] text-muted-foreground", side === "out" ? "justify-end pr-1" : "pl-1")} title={desc}>
      <Handle
        type={side === "in" ? "target" : "source"}
        position={side === "in" ? Position.Left : Position.Right}
        id={id}
        aria-label={desc}
        className={handleClass}
        style={{ background: color, ...(multiple ? { borderRadius: 2 } : {}) }}
      />
      <span className={cn("truncate font-mono", required === false && "italic")}>{name}</span>
    </div>
  );
}

const WorkflowNodeBox = memo(function WorkflowNodeBox({ data, selected }: NodeProps<FlowNode>) {
  const status = data.status ? NODE_STATUS_META[data.status] : null;
  const errors = data.issues.filter((i) => (i.severity ?? "error") === "error").length;
  const warnings = data.issues.length - errors;
  if (data.role === "inputs") {
    return (
      <div className="w-[190px] rounded-lg border-2 border-dashed bg-muted/40 px-2 py-2 text-xs" data-testid="canvas-node-inputs">
        <p className="mb-1 font-semibold">Workflow inputs</p>
        {data.outputs.length ? data.outputs.map(([name, type]) => <PortRow key={name} side="out" id={`out:${name}`} name={name} type={type} />) : <p className="text-muted-foreground">None declared</p>}
      </div>
    );
  }
  const kind = data.kind;
  return (
    <div
      data-testid={`canvas-node-${data.nodeId}`}
      data-status={data.status ?? undefined}
      className={cn(
        "relative w-[230px] rounded-lg border bg-card text-card-foreground shadow-sm transition-shadow",
        status ? TONE_NODE[status.tone] : errors ? "border-destructive" : "border-border",
        selected && "ring-2 ring-primary ring-offset-1 ring-offset-background",
        data.status === "running" && "animate-pulse",
      )}
    >
      {data.isBranch ? null : (
        <Handle type="target" position={Position.Top} id={GATE_HANDLE} aria-label={`Gate ${data.nodeId} on a branch arm`} className={cn(handleClass, "!bg-muted-foreground")} title="Drop a branch arm here to run this node only on that arm" />
      )}
      <Handle type="source" position={Position.Right} id={REF_OUT} isConnectable={false} className="!pointer-events-none !opacity-0" />
      <Handle type="target" position={Position.Left} id={REF_IN} isConnectable={false} className="!pointer-events-none !opacity-0" />
      <div className="flex items-center justify-between gap-2 border-b px-2 py-1" style={{ boxShadow: kind ? `inset 3px 0 0 ${KIND_COLOR[kind]}` : undefined }}>
        <span className="truncate text-[11px] font-medium text-muted-foreground">{kind ? KIND_LABEL[kind] : "Unknown"}</span>
        <span className="flex items-center gap-1">
          {errors ? (
            <span className="inline-flex items-center gap-0.5 rounded bg-destructive/12 px-1 text-[11px] font-semibold text-destructive" aria-label={`${errors} problem${errors === 1 ? "" : "s"}`}>
              <AlertTriangle className="size-3" aria-hidden /> {errors}
            </span>
          ) : warnings ? (
            <span className="inline-flex items-center gap-0.5 rounded bg-warning/14 px-1 text-[11px] font-semibold text-warning" aria-label={`${warnings} warning${warnings === 1 ? "" : "s"}`}>
              <AlertTriangle className="size-3" aria-hidden /> {warnings}
            </span>
          ) : null}
          {status ? <span className={cn("rounded px-1 text-[11px] font-semibold", status.tone === "danger" ? "text-destructive" : status.tone === "success" ? "text-success" : status.tone === "active" ? "text-info" : "text-muted-foreground")}>{status.label}</span> : null}
        </span>
      </div>
      <div className="px-2 pt-1.5">
        <p className="truncate text-[13px] leading-5 font-semibold" title={data.label ?? data.nodeId}>
          {data.label ?? data.title}
        </p>
        <p className="truncate font-mono text-[11px] text-muted-foreground">
          {data.nodeId} · {data.implId}
        </p>
        {data.gatedBy ? (
          <p className="mt-0.5 flex items-center gap-1 text-[11px] text-muted-foreground">
            <GitBranch className="size-3" aria-hidden /> only if {data.gatedBy.branch} → {data.gatedBy.arm}
          </p>
        ) : null}
      </div>
      <div className="grid grid-cols-2 gap-x-2 px-0 pt-1 pb-1.5">
        <div className="min-w-0">
          {data.inputs.map(([port, spec]) => (
            <PortRow key={port} side="in" id={`in:${port}`} name={port} type={spec.type} required={spec.required ?? true} multiple={spec.multiple ?? false} />
          ))}
        </div>
        <div className="min-w-0">
          {data.isBranch ? (
            <>
              <div className="relative flex h-5 items-center justify-end pr-1 text-[11px] font-medium text-success">
                then
                <Handle type="source" position={Position.Right} id="arm:then" aria-label={`${data.nodeId} then arm`} className={cn(handleClass, "!bg-success")} />
              </div>
              <div className="relative flex h-5 items-center justify-end pr-1 text-[11px] font-medium text-muted-foreground">
                else
                <Handle type="source" position={Position.Right} id="arm:else" aria-label={`${data.nodeId} else arm`} className={cn(handleClass, "!bg-muted-foreground")} />
              </div>
            </>
          ) : (
            data.outputs.map(([port, type]) => <PortRow key={port} side="out" id={`out:${port}`} name={port} type={type} />)
          )}
        </div>
      </div>
      {data.readOnly && !status ? <Lock className="absolute right-1 bottom-1 size-3 text-muted-foreground" aria-hidden /> : null}
    </div>
  );
});

const nodeTypes = { workflow: WorkflowNodeBox };

function toEdge(e: CanvasEdgeModel): Edge {
  const color = e.kind === "data" && e.portType ? PORT_TYPE_META[e.portType].color : e.kind === "control" ? (e.label === "then" ? "var(--success)" : "var(--muted-foreground)") : "var(--border)";
  return {
    id: e.id,
    source: e.source,
    target: e.target,
    sourceHandle: e.sourceHandle,
    targetHandle: e.targetHandle,
    ...(e.label ? { label: e.label } : {}),
    deletable: e.kind !== "reads",
    selectable: e.kind !== "reads",
    focusable: e.kind !== "reads",
    ariaLabel:
      e.kind === "data" ? `${e.binding} feeds ${e.target}.${e.port}${e.label ? ` (${e.label})` : ""}` : e.kind === "control" ? `${e.target} runs only on the ${e.label} arm of ${e.source}` : `${e.target} reads results of ${e.source}`,
    type: e.kind === "control" ? "smoothstep" : "default",
    animated: false,
    style: { stroke: color, strokeWidth: e.kind === "data" ? 1.75 : 1.25, ...(e.kind === "control" ? { strokeDasharray: "6 4" } : e.kind === "reads" ? { strokeDasharray: "2 4" } : {}) },
    markerEnd: { type: MarkerType.ArrowClosed, color, width: 14, height: 14 },
    labelStyle: { fontSize: 10, fill: "var(--muted-foreground)" },
    labelBgStyle: { fill: "var(--card)" },
    data: { model: e },
  };
}

function CanvasInner({ doc, catalog, subs, positions, issues, statuses, selected, readOnly = false, onSelect, onEdit, onMove, onRefused, height = "70vh", label }: WorkflowCanvasProps) {
  const theme = useResolvedTheme();
  const flow = useReactFlow<FlowNode, Edge>();
  const models = useMemo(
    () => buildCanvasNodes(doc, catalog, { ...(subs ? { subs } : {}), ...(issues ? { issues } : {}), ...(statuses ? { statuses } : {}), readOnly }),
    [doc, catalog, subs, issues, statuses, readOnly],
  );
  const [nodes, setNodes] = useState<FlowNode[]>([]);
  useEffect(() => {
    setNodes((prev) => {
      const byId = new Map(prev.map((n) => [n.id, n]));
      return models.map((m) => {
        const old = byId.get(m.nodeId);
        return {
          ...(old ?? {}),
          id: m.nodeId,
          type: "workflow" as const,
          data: m,
          position: positions[m.nodeId] ?? old?.position ?? { x: 0, y: 0 },
          selected: m.nodeId === selected,
          deletable: !readOnly && m.role === "node",
          connectable: !readOnly,
          ariaLabel: m.role === "inputs" ? "Workflow inputs" : `${m.label ?? m.title} (${m.nodeId})${m.status ? `, ${NODE_STATUS_META[m.status].label}` : ""}${m.issues.length ? `, ${m.issues.length} problem(s)` : ""}`,
        };
      });
    });
  }, [models, positions, selected, readOnly]);

  const edges = useMemo(() => buildCanvasEdges(doc, catalog, subs).map(toEdge), [doc, catalog, subs]);

  const onNodesChange = useCallback((changes: NodeChange<FlowNode>[]) => {
    setNodes((nds) => applyNodeChanges(changes.filter((c) => c.type !== "remove"), nds));
  }, []);

  const check = useCallback((c: Connection | Edge) => checkCanvasConnection(doc, catalog, { source: c.source, sourceHandle: c.sourceHandle ?? null, target: c.target, targetHandle: c.targetHandle ?? null }, subs), [doc, catalog, subs]);

  const onConnect = useCallback(
    (c: Connection) => {
      if (!onEdit) return;
      const r = check(c);
      if (!r.ok) {
        onRefused?.(r.reason);
        return;
      }
      if (r.kind === "control") onEdit({ type: "gate", target: c.target, when: `${c.source}.${r.arm}` });
      else onEdit({ type: "connect", binding: c.source === INPUTS_NODE ? `inputs.${r.sourcePort}` : `${c.source}.${r.sourcePort}`, target: c.target, port: r.port, mode: r.mode });
    },
    [check, onEdit, onRefused],
  );

  const onConnectEnd: OnConnectEnd = useCallback(
    (_event, state) => {
      if (!state.toHandle || !state.fromHandle || state.isValid !== false) return;
      const from = state.fromHandle;
      const to = state.toHandle;
      const conn = from.type === "source" ? { source: from.nodeId, sourceHandle: from.id ?? null, target: to.nodeId, targetHandle: to.id ?? null } : { source: to.nodeId, sourceHandle: to.id ?? null, target: from.nodeId, targetHandle: from.id ?? null };
      const r = checkCanvasConnection(doc, catalog, conn, subs);
      if (!r.ok) onRefused?.(r.reason);
    },
    [doc, catalog, subs, onRefused],
  );

  const onDrop = useCallback(
    (e: DragEvent<HTMLDivElement>) => {
      const impl = e.dataTransfer.getData(PALETTE_MIME);
      if (!impl || !onEdit || readOnly) return;
      e.preventDefault();
      onEdit({ type: "add-node", impl, position: flow.screenToFlowPosition({ x: e.clientX, y: e.clientY }) });
    },
    [flow, onEdit, readOnly],
  );

  return (
    <div className="relative overflow-hidden rounded-lg border bg-background" style={{ height }} data-testid="workflow-canvas">
      <ReactFlow<FlowNode, Edge>
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onConnect={onConnect}
        onConnectEnd={onConnectEnd}
        isValidConnection={(c) => check(c).ok}
        onNodesDelete={(ns) => onEdit?.({ type: "remove-nodes", ids: ns.map((n) => n.id).filter((id) => id !== INPUTS_NODE) })}
        onEdgesDelete={(es) => {
          for (const e of es) {
            const m = (e.data as { model?: CanvasEdgeModel } | undefined)?.model;
            if (!m) continue;
            if (m.kind === "data" && m.binding && m.port) onEdit?.({ type: "disconnect", target: m.target, port: m.port, binding: m.binding });
            if (m.kind === "control") onEdit?.({ type: "gate", target: m.target, when: null });
          }
        }}
        onSelectionChange={({ nodes: sel }) => {
          const id = sel[0]?.id ?? null;
          if (id !== selected && (id !== null || sel.length === 0)) onSelect(id === INPUTS_NODE ? null : id);
        }}
        onNodeDragStop={(_e, n) => onMove?.(n.id, n.position)}
        onDragOver={(e) => {
          if (readOnly) return;
          e.preventDefault();
          e.dataTransfer.dropEffect = "copy";
        }}
        onDrop={onDrop}
        nodesConnectable={!readOnly}
        elementsSelectable
        deleteKeyCode={readOnly ? null : ["Backspace", "Delete"]}
        colorMode={theme}
        fitView
        fitViewOptions={{ padding: 0.15 }}
        minZoom={0.2}
        proOptions={{ hideAttribution: true }}
        aria-label={label}
        ariaLabelConfig={{
          "node.a11yDescription.default": "Press Enter or Space to select the node, Delete to remove it. Arrow keys move a selected node.",
          "edge.a11yDescription.default": "Press Enter or Space to select the connection, Delete to remove it.",
        }}
      >
        <Background gap={20} size={1} />
        <Controls showInteractive={false} />
        <MiniMap pannable zoomable ariaLabel="Overview of the workflow" className="!hidden md:!block" />
      </ReactFlow>
    </div>
  );
}

/** The canvas with its own React Flow provider (one per editor or run view). */
export function WorkflowCanvas(props: WorkflowCanvasProps) {
  return (
    <ReactFlowProvider>
      <CanvasInner {...props} />
    </ReactFlowProvider>
  );
}

export default WorkflowCanvas;
