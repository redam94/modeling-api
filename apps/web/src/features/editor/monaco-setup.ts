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

// Surfaces and line numbers follow the app.css tokens (card, page, hint, border). Syntax colours are
// the five of the docs site: keyword, string, number, comment, and function or type.
function defineThemes(monaco: MonacoApi): void {
  monaco.editor.defineTheme("mapi-light", {
    base: "vs",
    inherit: true,
    rules: [
      { token: "type", foreground: "0f68a0" },
      { token: "string", foreground: "c41a16" },
      { token: "number", foreground: "1c00cf" },
      { token: "keyword", foreground: "9b2393" },
      { token: "comment", foreground: "6e6e73", fontStyle: "italic" },
    ],
    colors: {
      "editor.background": "#ffffff",
      "editor.lineHighlightBackground": "#f5f5f7",
      "editorLineNumber.foreground": "#86868b",
      "editorLineNumber.activeForeground": "#424245",
      "editor.selectionBackground": "#bfdcec",
      "editorIndentGuide.background1": "#e8e8ed",
    },
  });
  monaco.editor.defineTheme("mapi-dark", {
    base: "vs-dark",
    inherit: true,
    rules: [
      { token: "type", foreground: "6bdfff" },
      { token: "string", foreground: "ff8170" },
      { token: "number", foreground: "d9c97c" },
      { token: "keyword", foreground: "ff7ab2" },
      { token: "comment", foreground: "86868b", fontStyle: "italic" },
    ],
    colors: {
      "editor.background": "#161617",
      "editor.lineHighlightBackground": "#1d1d1f",
      "editorLineNumber.foreground": "#86868b",
      "editorLineNumber.activeForeground": "#d2d2d7",
      "editor.selectionBackground": "#31586f",
      "editorIndentGuide.background1": "#2a2a2c",
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
