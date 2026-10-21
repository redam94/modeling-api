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
      { token: "type", foreground: "0014cc" },
      { token: "string", foreground: "036e1b" },
      { token: "number", foreground: "a63d00" },
      { token: "keyword", foreground: "00078f" },
      { token: "comment", foreground: "697077", fontStyle: "italic" },
    ],
    colors: {
      "editor.background": "#ffffff",
      "editor.lineHighlightBackground": "#f4f5f7",
      "editorLineNumber.foreground": "#a2a9b0",
      "editorLineNumber.activeForeground": "#4d5358",
      "editor.selectionBackground": "#c2d4ff",
      "editorIndentGuide.background1": "#e7eaee",
    },
  });
  monaco.editor.defineTheme("mapi-dark", {
    base: "vs-dark",
    inherit: true,
    rules: [
      { token: "type", foreground: "85a3ff" },
      { token: "string", foreground: "4fd26b" },
      { token: "number", foreground: "ff9e66" },
      { token: "keyword", foreground: "c2d4ff" },
      { token: "comment", foreground: "959ba3", fontStyle: "italic" },
    ],
    colors: {
      "editor.background": "#1b212d",
      "editor.lineHighlightBackground": "#222936",
      "editorLineNumber.foreground": "#697077",
      "editorLineNumber.activeForeground": "#a2a9b0",
      "editor.selectionBackground": "#24346b",
      "editorIndentGuide.background1": "#2c3440",
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
