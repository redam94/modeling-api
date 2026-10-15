import type * as Monaco from "monaco-editor";
import { useEffect, useRef, useState } from "react";

import { Spinner } from "@/components/common";
import { useResolvedTheme } from "@/hooks/use-theme";

import { loadMonaco } from "./monaco-setup";

/** Side-by-side (or inline on narrow screens) diff of two spec revisions. Read-only. */
export function DiffView({ original, modified, height = "60vh" }: { original: string; modified: string; height?: string }) {
  const host = useRef<HTMLDivElement>(null);
  const editorRef = useRef<Monaco.editor.IStandaloneDiffEditor | null>(null);
  const [ready, setReady] = useState(false);
  const theme = useResolvedTheme();

  useEffect(() => {
    let disposed = false;
    const models: Monaco.editor.ITextModel[] = [];
    void loadMonaco().then(({ monaco }) => {
      if (disposed || !host.current) return;
      const a = monaco.editor.createModel(original, "yaml");
      const b = monaco.editor.createModel(modified, "yaml");
      models.push(a, b);
      const editor = monaco.editor.createDiffEditor(host.current, {
        readOnly: true,
        originalEditable: false,
        automaticLayout: true,
        renderSideBySide: host.current.clientWidth > 900,
        minimap: { enabled: false },
        fontFamily: "'JetBrains Mono Variable', ui-monospace, monospace",
        fontSize: 13,
        scrollBeyondLastLine: false,
        links: false,
      });
      editor.setModel({ original: a, modified: b });
      editorRef.current = editor;
      setReady(true);
    });
    return () => {
      disposed = true;
      editorRef.current?.dispose();
      editorRef.current = null;
      models.forEach((m) => m.dispose());
    };
  }, [original, modified]);

  useEffect(() => {
    if (!ready) return;
    void loadMonaco().then(({ monaco }) => monaco.editor.setTheme(theme === "dark" ? "mapi-dark" : "mapi-light"));
  }, [theme, ready]);

  return (
    <div className="relative overflow-hidden rounded-lg border bg-card" style={{ height }}>
      <div ref={host} className="absolute inset-0" />
      {!ready ? (
        <div className="absolute inset-0 flex items-center justify-center gap-2 text-muted-foreground">
          <Spinner /> Loading diff…
        </div>
      ) : null}
    </div>
  );
}
