import type * as Monaco from "monaco-editor";
import { useEffect, useRef, useState } from "react";

import { Spinner } from "@/components/common";
import { useResolvedTheme } from "@/hooks/use-theme";
import type { LocatedIssue, TextRange } from "@/lib/yaml-diagnostics";
import { markerMessage, severityOf } from "@/lib/yaml-diagnostics";

import { loadMonaco, type MonacoApi } from "./monaco-setup";

export interface YamlEditorHandle {
  reveal(range: TextRange): void;
  focus(): void;
}

export interface YamlEditorProps {
  /** Unique per document; must end in `.mapi.yaml` for the ModelSpec schema to apply. */
  path: string;
  value: string;
  onChange?: (value: string) => void;
  /** Server-side validation issues, already located in the text. */
  issues?: LocatedIssue[];
  readOnly?: boolean;
  onSave?: () => void;
  onValidate?: () => void;
  onReady?: (handle: YamlEditorHandle) => void;
  height?: string;
  ariaLabel: string;
}

const MARKER_OWNER = "mapi-validate";

export function toMarkers(monaco: MonacoApi, issues: LocatedIssue[]): Monaco.editor.IMarkerData[] {
  const sev = {
    error: monaco.MarkerSeverity.Error,
    warning: monaco.MarkerSeverity.Warning,
    info: monaco.MarkerSeverity.Info,
  } as const;
  return issues.map(({ issue, range, exact }) => ({
    ...range,
    severity: sev[severityOf(issue)],
    message: exact ? markerMessage(issue) : `${markerMessage(issue)}\n(at ${issue.pointer || "/"})`,
    source: "mapi",
    code: issue.code,
  }));
}

/** Monaco + monaco-yaml editor with live server diagnostics as markers. */
export function YamlEditor({
  path,
  value,
  onChange,
  issues = [],
  readOnly = false,
  onSave,
  onValidate,
  onReady,
  height = "60vh",
  ariaLabel,
}: YamlEditorProps) {
  const host = useRef<HTMLDivElement>(null);
  const editorRef = useRef<Monaco.editor.IStandaloneCodeEditor | null>(null);
  const monacoRef = useRef<MonacoApi | null>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const theme = useResolvedTheme();
  const callbacks = useRef({ onChange, onSave, onValidate, onReady });
  callbacks.current = { onChange, onSave, onValidate, onReady };
  // Monaco loads asynchronously: create the model from the latest value, not the first one.
  const valueRef = useRef(value);
  valueRef.current = value;

  useEffect(() => {
    let disposed = false;
    let model: Monaco.editor.ITextModel | null = null;
    void loadMonaco()
      .then(({ monaco }) => {
        if (disposed || !host.current) return;
        monacoRef.current = monaco;
        const uri = monaco.Uri.parse(`file:///${path}`);
        const initial = valueRef.current;
        model = monaco.editor.getModel(uri) ?? monaco.editor.createModel(initial, "yaml", uri);
        if (model.getValue() !== initial) model.setValue(initial);
        const editor = monaco.editor.create(host.current, {
          model,
          readOnly,
          automaticLayout: true,
          minimap: { enabled: false },
          fontFamily: "'JetBrains Mono Variable', ui-monospace, monospace",
          fontSize: 13,
          lineHeight: 20,
          tabSize: 2,
          insertSpaces: true,
          scrollBeyondLastLine: false,
          renderWhitespace: "boundary",
          ariaLabel,
          quickSuggestions: { other: true, strings: true, comments: false },
          padding: { top: 12, bottom: 12 },
          theme: document.documentElement.classList.contains("dark") ? "mapi-dark" : "mapi-light",
          links: false,
        });
        editorRef.current = editor;
        editor.onDidChangeModelContent(() => callbacks.current.onChange?.(editor.getValue()));
        editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => callbacks.current.onSave?.());
        editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter, () => callbacks.current.onValidate?.());
        callbacks.current.onReady?.({
          reveal: (r) => {
            editor.revealRangeInCenter(r);
            editor.setSelection(r);
            editor.focus();
          },
          focus: () => editor.focus(),
        });
        setReady(true);
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
    return () => {
      disposed = true;
      editorRef.current?.dispose();
      editorRef.current = null;
      model?.dispose();
    };
    // The model is created once per path; later `value` changes are applied below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path]);

  useEffect(() => {
    const editor = editorRef.current;
    if (editor && editor.getValue() !== value) {
      editor.getModel()?.pushEditOperations([], [{ range: editor.getModel()!.getFullModelRange(), text: value }], () => null);
    }
  }, [value]);

  useEffect(() => {
    editorRef.current?.updateOptions({ readOnly });
  }, [readOnly]);

  useEffect(() => {
    monacoRef.current?.editor.setTheme(theme === "dark" ? "mapi-dark" : "mapi-light");
  }, [theme, ready]);

  useEffect(() => {
    const monaco = monacoRef.current;
    const model = editorRef.current?.getModel();
    if (monaco && model) monaco.editor.setModelMarkers(model, MARKER_OWNER, toMarkers(monaco, issues));
  }, [issues, ready]);

  return (
    <div className="relative overflow-hidden rounded-lg border bg-card" style={{ height }}>
      <div ref={host} className="absolute inset-0" data-testid="yaml-editor" />
      {!ready && !error ? (
        <div className="absolute inset-0 flex items-center justify-center gap-2 text-muted-foreground">
          <Spinner /> Loading editor…
        </div>
      ) : null}
      {error ? <p className="p-4 text-destructive">The editor failed to load: {error}</p> : null}
    </div>
  );
}
