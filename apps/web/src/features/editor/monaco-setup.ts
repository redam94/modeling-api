/**
 * Lazy Monaco + monaco-yaml setup (code-split: nothing here loads until an editor mounts).
 *
 * - Workers are bundled by Vite (`?worker`), never fetched from a CDN.
 * - The ModelSpec JSON Schema is supplied inline from `GET /v1/schemas/modelspec.json`;
 *   `enableSchemaRequest` stays off so the editor never fetches remote `$ref`s.
 * - Hover markdown from schemas is rendered untrusted (Monaco's default `isTrusted: false`:
 *   no command links), satisfying plan §9.
 */
import type * as Monaco from "monaco-editor";
import type { MonacoYaml } from "monaco-yaml";

export type MonacoApi = typeof Monaco;

export const SPEC_FILE_GLOB = "**/*.mapi.yaml";

interface Loaded {
  monaco: MonacoApi;
  yaml: MonacoYaml;
}

let loading: Promise<Loaded> | null = null;

function defineThemes(monaco: MonacoApi): void {
  monaco.editor.defineTheme("mapi-light", {
    base: "vs",
    inherit: true,
    rules: [
      { token: "type", foreground: "4b3cc4" },
      { token: "string", foreground: "1f7a63" },
      { token: "number", foreground: "b8641c" },
      { token: "keyword", foreground: "a8366a" },
      { token: "comment", foreground: "8a93a3", fontStyle: "italic" },
    ],
    colors: {
      "editor.background": "#ffffff",
      "editor.lineHighlightBackground": "#f3f4f8",
      "editorLineNumber.foreground": "#b3bac6",
      "editorLineNumber.activeForeground": "#5b6577",
      "editor.selectionBackground": "#dcd8ff",
      "editorIndentGuide.background1": "#eceef3",
    },
  });
  monaco.editor.defineTheme("mapi-dark", {
    base: "vs-dark",
    inherit: true,
    rules: [
      { token: "type", foreground: "b3a9ff" },
      { token: "string", foreground: "6fd3b5" },
      { token: "number", foreground: "f0b27a" },
      { token: "keyword", foreground: "ef8fbb" },
      { token: "comment", foreground: "6f7a8c", fontStyle: "italic" },
    ],
    colors: {
      "editor.background": "#1b212d",
      "editor.lineHighlightBackground": "#212837",
      "editorLineNumber.foreground": "#4a5468",
      "editorLineNumber.activeForeground": "#98a1b1",
      "editor.selectionBackground": "#3a3470",
      "editorIndentGuide.background1": "#2a3140",
    },
  });
}

export function loadMonaco(): Promise<Loaded> {
  loading ??= (async () => {
    const [{ default: monaco }, monacoYaml, editorWorker, yamlWorker] = await Promise.all([
      import("./monaco-lite"),
      import("monaco-yaml"),
      import("monaco-editor/esm/vs/editor/editor.worker.js?worker"),
      import("./yaml.worker.ts?worker"),
    ]);
    self.MonacoEnvironment = {
      getWorker(_moduleId: string, label: string) {
        return label === "yaml" ? new yamlWorker.default() : new editorWorker.default();
      },
    };
    defineThemes(monaco);
    const yaml = monacoYaml.configureMonacoYaml(monaco, {
      enableSchemaRequest: false,
      hover: true,
      completion: true,
      validate: true,
      format: { enable: true },
      yamlVersion: "1.2",
      schemas: [],
    });
    return { monaco, yaml };
  })();
  return loading;
}

/** Bind the ModelSpec schema to every `*.mapi.yaml` model. */
export async function setSpecSchema(uri: string, schema: unknown): Promise<void> {
  const { yaml } = await loadMonaco();
  await yaml.update({
    schemas: [{ uri, fileMatch: [SPEC_FILE_GLOB], schema: schema as never }],
  });
}
