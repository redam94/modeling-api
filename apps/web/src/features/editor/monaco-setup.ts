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

// Colours follow the app.css tokens (brand ramp for types/keywords, status inks, hint grey).
function defineThemes(monaco: MonacoApi): void {
  monaco.editor.defineTheme("mapi-light", {
    base: "vs",
    inherit: true,
    rules: [
      { token: "type", foreground: "3a2bd6" },
      { token: "string", foreground: "0b6b3a" },
      { token: "number", foreground: "9a4200" },
      { token: "keyword", foreground: "2a1ea8" },
      { token: "comment", foreground: "625f79", fontStyle: "italic" },
    ],
    colors: {
      "editor.background": "#ffffff",
      "editor.lineHighlightBackground": "#f6f5fb",
      "editorLineNumber.foreground": "#84819b",
      "editorLineNumber.activeForeground": "#48455f",
      "editor.selectionBackground": "#dcd8ff",
      "editorIndentGuide.background1": "#eeedf6",
    },
  });
  monaco.editor.defineTheme("mapi-dark", {
    base: "vs-dark",
    inherit: true,
    rules: [
      { token: "type", foreground: "aaa3ff" },
      { token: "string", foreground: "5fdc8f" },
      { token: "number", foreground: "ffb36b" },
      { token: "keyword", foreground: "dcd8ff" },
      { token: "comment", foreground: "9d9abf", fontStyle: "italic" },
    ],
    colors: {
      "editor.background": "#1a1642",
      "editor.lineHighlightBackground": "#1f1a4c",
      "editorLineNumber.foreground": "#8a86b3",
      "editorLineNumber.activeForeground": "#b6b3d2",
      "editor.selectionBackground": "#2f2878",
      "editorIndentGuide.background1": "#2e2866",
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
