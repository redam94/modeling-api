import { useVirtualizer } from "@tanstack/react-virtual";
import { ArrowDownToLine } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { useRunLogs } from "@/api/queries";
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
        ) : (
          <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
            {virtualizer.getVirtualItems().map((v) => {
              const l = lines[v.index];
              if (!l) return null;
              return (
                <div
                  key={l.key}
                  data-index={v.index}
                  ref={virtualizer.measureElement}
                  className="absolute left-0 grid w-full grid-cols-[5.5rem_3.5rem_1fr] gap-2 px-3"
                  style={{ transform: `translateY(${v.start}px)` }}
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
            })}
          </div>
        )}
      </div>
      {logs.data?.next_cursor ? (
        <p className="text-xs text-muted-foreground">Showing the first {lines.length} lines; older pages are not loaded.</p>
      ) : null}
    </div>
  );
}
