/**
 * The YAML view of a library document (every builder has one): the document as YAML in the
 * lazy Monaco editor, with the `:validate` issues as markers. Edits that parse into a document
 * of the same kind replace the builder's document (builder ↔ YAML round trip); edits that do
 * not parse stay in the editor with the reason under it, and the builder keeps its last good
 * document.
 */
import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";

import type { Issue } from "@/api/types";
import { LoadingRows } from "@/components/common";
import { InlineMessage } from "@/components/ods";
import { locateIssues } from "@/lib/yaml-diagnostics";

import { docToYaml, yamlToDoc } from "./doc-yaml";
import { docKey } from "./api";
import { docOfKind, type LibraryDoc, type LibraryKind } from "./types";

const YamlEditor = lazy(() => import("@/features/editor/yaml-editor").then((m) => ({ default: m.YamlEditor })));

export function YamlTab<K extends LibraryKind>({ kind, doc, onDoc, issues, height = "28rem" }: { kind: K; doc: LibraryDoc; onDoc: (doc: LibraryDoc) => void; issues?: readonly Issue[]; height?: string }) {
  const [text, setText] = useState(() => docToYaml(doc));
  const [error, setError] = useState<string | null>(null);
  /** The document the text last came from or produced: builder changes regenerate the text. */
  const synced = useRef(docKey(doc));
  useEffect(() => {
    const key = docKey(doc);
    if (key === synced.current) return;
    synced.current = key;
    setText(docToYaml(doc));
    setError(null);
  }, [doc]);
  const located = useMemo(() => (issues?.length ? locateIssues(text, issues) : []), [text, issues]);
  return (
    <div className="grid gap-2">
      <div className="overflow-hidden rounded-xl border">
        <Suspense fallback={<LoadingRows rows={6} />}>
          <YamlEditor
            path={`library-${kind}.yaml`}
            value={text}
            height={height}
            issues={located}
            ariaLabel="The item as YAML"
            onChange={(next) => {
              setText(next);
              const parsed = yamlToDoc(next, kind, (v) => docOfKind(kind, v) !== null);
              if (parsed.ok) {
                setError(null);
                synced.current = docKey(parsed.doc);
                onDoc(parsed.doc);
              } else setError(parsed.error);
            }}
          />
        </Suspense>
      </div>
      {error ? (
        <InlineMessage tone="warning">
          The YAML is not valid, so the builder keeps your last good version. {error}
        </InlineMessage>
      ) : (
        <p className="text-xs text-muted-foreground">Edit here or in the builder: the two stay in step.</p>
      )}
    </div>
  );
}

export default YamlTab;
