/**
 * Builder › Setup › Panels (layout 2): the panels of the readout in the order clients see them.
 * Add a panel of any kind the layout allows (bound to the model's main output; a kind that
 * cannot be added says why), open one to edit it, move it up or down, or remove it. Every
 * control is a native button, so the list works from the keyboard; after a move the focus
 * stays on the moved panel's button and a polite live region says where it went.
 */
import { ChevronDown, ChevronRight, ChevronUp, Plus, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { TagWord } from "@/components/ods";
import { Button } from "@/components/ui/button";

import { Section } from "../readout/parts";
import { MAX_KPI_PANELS, MAX_PANELS, PANEL_KINDS, type DashboardSpecV2, type PanelKind, type SourceCatalog } from "../types";
import { PanelEditor } from "./panel-editor";
import { issueAt, type FormIssue } from "./spec-form";
import { addPanel, addPanelReason, defaultPanel, INTENT_WORD, movePanel, PANEL_KIND_WORD, PRESET_RULES, removePanel, setPanel } from "./spec-form-v2";

export interface PanelsEditorProps {
  spec: DashboardSpecV2;
  onChange: (f: (s: DashboardSpecV2) => DashboardSpecV2) => void;
  issues: readonly FormIssue[];
  catalog: SourceCatalog | null | undefined;
  readOnly?: boolean;
}

const ADD_WORD: Record<PanelKind, string> = { kpi: "Key number", chart: "Chart", table: "Table", note: "Note", recipe: "Model plot", mmm: "Planner part" };

/**
 * Stable row keys of the panel list. A panel's `id` is editable (typing in it must not close the
 * editor or remount the row), so rows are keyed and opened by a client-side key that follows the
 * panel through moves, adds and removes.
 */
function useRowKeys(n: number) {
  const keys = useRef<string[]>([]);
  const next = useRef(0);
  while (keys.current.length < n) keys.current.push(`row-${next.current++}`);
  if (keys.current.length > n) keys.current.length = n;
  return keys.current;
}

export function PanelsEditor({ spec, onChange, issues, catalog, readOnly = false }: PanelsEditorProps) {
  const keys = useRowKeys(spec.panels.length);
  const [openKey, setOpen] = useState<string | null>(null);
  const [said, setSaid] = useState("");
  const focusNext = useRef<{ id: string; dir: "up" | "down" } | null>(null);
  const buttons = useRef(new Map<string, HTMLButtonElement | null>());
  const kinds = PANEL_KINDS.filter((k) => PRESET_RULES[spec.preset].kinds.includes(k));
  const kpis = spec.panels.filter((p) => p.kind === "kpi").length;

  useEffect(() => {
    const f = focusNext.current;
    if (!f) return;
    focusNext.current = null;
    const el = buttons.current.get(`${f.id}:${f.dir}`);
    const other = buttons.current.get(`${f.id}:${f.dir === "up" ? "down" : "up"}`);
    (el && !el.disabled ? el : other)?.focus();
  });

  const move = (i: number, delta: -1 | 1) => {
    const p = spec.panels[i];
    if (!p) return;
    const to = Math.max(0, Math.min(spec.panels.length - 1, i + delta));
    const key = keys[i];
    focusNext.current = key ? { id: key, dir: delta < 0 ? "up" : "down" } : null;
    if (to !== i && key !== undefined && keys[to] !== undefined) [keys[i], keys[to]] = [keys[to], key];
    onChange((s) => movePanel(s, i, delta));
    setSaid(`${p.title || p.id} moved to place ${to + 1} of ${spec.panels.length}.`);
  };
  const add = (kind: PanelKind) => {
    const p = defaultPanel(spec, kind, catalog);
    if (!p) return;
    onChange((s) => addPanel(s, p));
    keys.push(`row-new-${keys.length}-${Date.now()}`);
    setOpen(keys[keys.length - 1] ?? null);
    setSaid(`${ADD_WORD[kind]} added at place ${spec.panels.length + 1}.`);
  };
  const remove = (i: number) => {
    const p = spec.panels[i];
    if (!p) return;
    onChange((s) => removePanel(s, i));
    keys.splice(i, 1);
    setSaid(`${p.title || p.id} removed.`);
  };

  const reasons = kinds.map((k) => [k, addPanelReason(spec, k, catalog)] as const);
  return (
    <Section
      className="xl:col-span-2"
      title="Panels"
      description={`What the readout shows, from the top. At most ${MAX_PANELS} panels and ${MAX_KPI_PANELS} key numbers (${spec.panels.length} panels and ${kpis} key numbers now).`}
    >
      {issueAt(issues, "/panels") ? (
        <p role="alert" className="text-xs font-medium text-destructive">
          {issueAt(issues, "/panels")}
        </p>
      ) : null}
      <ol aria-label="Panels" className="grid gap-2">
        {spec.panels.map((p, i) => {
          const who = p.title.trim() || p.id || `panel ${i + 1}`;
          const rowKey = keys[i] ?? `row-${i}`;
          const isOpen = openKey === rowKey;
          const problem = issueAt(issues, `/panels/${i}`, true);
          const editorId = `panel-editor-${i}`;
          const what = p.kind === "chart" ? INTENT_WORD[p.intent] : PANEL_KIND_WORD[p.kind];
          return (
            <li key={rowKey} data-panel={p.id} className="rounded-lg border">
              <div className="flex flex-wrap items-center gap-2 p-2">
                <Button size="sm" variant="ghost" aria-expanded={isOpen} aria-controls={editorId} onClick={() => setOpen(isOpen ? null : rowKey)} className="min-w-0 flex-1 justify-start">
                  {isOpen ? <ChevronDown /> : <ChevronRight />}
                  <span className="text-xs text-muted-foreground tabular-nums">{i + 1}.</span>
                  <span className="truncate font-medium">{who}</span>
                  <span className="sr-only">, {what}. {isOpen ? "Close the editor." : "Edit."}</span>
                </Button>
                <TagWord tone="neutral">{what}</TagWord>
                {problem ? <TagWord tone="negative">Needs a fix</TagWord> : null}
                <span className="flex gap-1">
                  <Button
                    size="icon-sm"
                    variant="ghost"
                    ref={(el) => void buttons.current.set(`${rowKey}:up`, el)}
                    aria-label={`Move ${who} up`}
                    disabled={readOnly || i === 0}
                    onClick={() => move(i, -1)}
                  >
                    <ChevronUp />
                  </Button>
                  <Button
                    size="icon-sm"
                    variant="ghost"
                    ref={(el) => void buttons.current.set(`${rowKey}:down`, el)}
                    aria-label={`Move ${who} down`}
                    disabled={readOnly || i === spec.panels.length - 1}
                    onClick={() => move(i, 1)}
                  >
                    <ChevronDown />
                  </Button>
                  <Button
                    size="icon-sm"
                    variant="ghost"
                    aria-label={`Remove ${who}`}
                    disabled={readOnly || spec.panels.length <= 1}
                    title={spec.panels.length <= 1 ? "A dashboard needs at least one panel." : undefined}
                    onClick={() => remove(i)}
                  >
                    <Trash2 />
                  </Button>
                </span>
              </div>
              {problem && !isOpen ? (
                <p className="px-3 pb-2 text-xs font-medium text-destructive">{problem}</p>
              ) : null}
              {isOpen ? (
                <div id={editorId} className="border-t p-3">
                  <PanelEditor spec={spec} index={i} panel={p} issues={issues} catalog={catalog} onChange={(next) => onChange((s) => setPanel(s, i, next))} />
                </div>
              ) : null}
            </li>
          );
        })}
      </ol>
      <p aria-live="polite" className="sr-only" data-testid="panels-live">
        {said}
      </p>
      <div className="grid gap-2">
        <p className="text-sm font-semibold">Add a panel</p>
        <div className="flex flex-wrap gap-2">
          {reasons.map(([k, why]) => (
            <Button key={k} size="sm" variant="secondary" disabled={readOnly || Boolean(why)} aria-describedby={why ? `panel-add-${k}-why` : undefined} onClick={() => add(k)}>
              <Plus /> {ADD_WORD[k]}
            </Button>
          ))}
        </div>
        {reasons.some(([, why]) => why) ? (
          <ul className="grid gap-0.5 text-xs text-muted-foreground">
            {reasons.map(([k, why]) =>
              why ? (
                <li key={k} id={`panel-add-${k}-why`}>
                  {ADD_WORD[k]}: {why}
                </li>
              ) : null,
            )}
          </ul>
        ) : null}
      </div>
    </Section>
  );
}
