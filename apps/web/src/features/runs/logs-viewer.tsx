import { useVirtualizer } from "@tanstack/react-virtual";
import { ArrowDownToLine } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";

import { MAX_LOG_LINES, useRunLogs } from "@/api/queries";
import type { LogLine } from "@/api/types";
import { ErrorNotice } from "@/components/common";
import { Button } from "@/components/ui/button";
import { SegmentedControl } from "@/components/ui/overlays";
import { Input } from "@/components/ui/primitives";
import { stripAnsi } from "@/lib/ansi";
import { cn } from "@/lib/utils";

type StreamFilter = "all" | "stdout" | "stderr" | "system";

export interface CleanLine {
  key: string;
  attempt: number;
  ts: string;
  stream: LogLine["stream"];
  text: string;
  truncated: boolean;
}

/** Untrusted log lines → display lines (ANSI/control stripped, ordered by attempt then seq). */
export function cleanLogLines(lines: LogLine[]): CleanLine[] {
  return [...lines]
    .sort((a, b) => a.attempt - b.attempt || a.seq - b.seq)
    .map((l) => ({
      key: `${l.attempt}:${l.seq}`,
      attempt: l.attempt,
      ts: l.ts,
      stream: l.stream,
      text: stripAnsi(l.line),
      truncated: Boolean(l.truncated),
    }));
}

const FALLBACK_ROWS = 500;

function LogRow({
  line: l,
  index,
  measure,
  style,
}: {
  line: CleanLine;
  index?: number;
  measure?: (el: Element | null) => void;
  style?: CSSProperties;
}) {
  return (
    <div
      data-index={index}
      ref={measure}
      className="grid w-full grid-cols-[5.5rem_3.5rem_1fr] gap-2 px-3"
      style={style}
    >
      <span className="text-muted-foreground/70 tabular">{l.ts.slice(11, 19)}</span>
      <span className={cn(l.stream === "stderr" ? "text-warning" : l.stream === "system" ? "text-info" : "text-muted-foreground")}>
        {l.stream}
      </span>
      <span className="break-all whitespace-pre-wrap">
        {l.text}
        {l.truncated ? <span className="text-muted-foreground"> [truncated]</span> : null}
      </span>
    </div>
  );
}

/**
 * Captured run output. Lines are untrusted: rendered as plain text nodes only (no markup,
 * no links), ANSI sequences removed. Virtualised so long logs stay fast.
 */
export function LogsViewer({ runId, live }: { runId: string; live: boolean }) {
  const [stream, setStream] = useState<StreamFilter>("all");
  const [needle, setNeedle] = useState("");
  const [follow, setFollow] = useState(true);
  const logs = useRunLogs(runId, stream === "all" ? null : stream, live);
  const lines = useMemo(() => {
    const all = cleanLogLines(logs.data?.items ?? []);
    const q = needle.trim().toLowerCase();
    return q ? all.filter((l) => l.text.toLowerCase().includes(q)) : all;
  }, [logs.data, needle]);

  const scrollRef = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: lines.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 20,
    overscan: 30,
  });

  const virtualItems = virtualizer.getVirtualItems();

  useEffect(() => {
    if (follow && lines.length) virtualizer.scrollToIndex(lines.length - 1, { align: "end" });
  }, [follow, lines.length, virtualizer]);

  return (
    <div className="grid gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <SegmentedControl<StreamFilter>
          label="Stream"
          value={stream}
          onValueChange={setStream}
          options={[
            { value: "all", label: "All" },
            { value: "stdout", label: "stdout" },
            { value: "stderr", label: "stderr" },
            { value: "system", label: "system" },
          ]}
        />
        <Input
          aria-label="Filter log lines"
          placeholder="Filter lines"
          value={needle}
          onChange={(e) => setNeedle(e.target.value)}
          className="h-8 max-w-56"
        />
        <Button
          variant={follow ? "secondary" : "ghost"}
          size="sm"
          onClick={() => setFollow((f) => !f)}
          aria-pressed={follow}
          className="ml-auto"
        >
          <ArrowDownToLine /> Follow
        </Button>
      </div>
      {logs.isError ? <ErrorNotice error={logs.error} title="Logs could not be loaded" /> : null}
      <div
        ref={scrollRef}
        role="log"
        aria-label="Run logs"
        aria-live="off"
        className="h-80 overflow-auto rounded-lg border bg-[color-mix(in_oklab,var(--foreground)_4%,var(--card))] font-mono text-[12px] leading-5"
        onWheel={() => setFollow(false)}
      >
        {lines.length === 0 ? (
          <p className="p-4 font-sans text-[13px] text-muted-foreground">
            {logs.isPending ? "Loading logs…" : needle ? "No lines match the filter." : "No output captured yet."}
          </p>
        ) : virtualItems.length === 0 ? (
          // Before the first layout pass (and in non-layout environments) render a plain slice.
          <div>
            {lines.slice(-FALLBACK_ROWS).map((l) => (
              <LogRow key={l.key} line={l} />
            ))}
          </div>
        ) : (
          <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
            {virtualItems.map((v) => {
              const l = lines[v.index];
              if (!l) return null;
              return (
                <LogRow
                  key={l.key}
                  line={l}
                  index={v.index}
                  measure={virtualizer.measureElement}
                  style={{ position: "absolute", left: 0, transform: `translateY(${v.start}px)` }}
                />
              );
            })}
          </div>
        )}
      </div>
      {logs.data?.dropped ? (
        <p className="text-xs text-muted-foreground">
          Showing the latest {MAX_LOG_LINES.toLocaleString()} lines; {logs.data.dropped.toLocaleString()} older lines are not kept in view.
        </p>
      ) : null}
      {logs.data?.behind ? (
        <p className="flex items-center gap-2 text-xs text-muted-foreground">
          {live ? "Loading newer lines…" : "Newer lines are not loaded yet."}
          {live ? null : (
            <Button variant="outline" size="sm" onClick={() => void logs.refetch()} disabled={logs.isFetching}>
              Load more
            </Button>
          )}
        </p>
      ) : null}
    </div>
  );
}
