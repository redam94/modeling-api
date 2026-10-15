/**
 * Monaco with only what the spec editor needs: the editor core + standard contributions and
 * the YAML tokenizer. The full `monaco-editor` entry would also bundle the TypeScript, CSS,
 * HTML and JSON language services and their (large) workers.
 */
import "monaco-editor/esm/vs/editor/editor.all.js";
import "monaco-editor/esm/vs/basic-languages/yaml/yaml.contribution.js";

import * as api from "monaco-editor/esm/vs/editor/editor.api.js";

export default api as unknown as typeof import("monaco-editor");
