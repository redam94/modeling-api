/**
 * The YAML round trip of a library document (builder <-> YAML tab). A module of its own so the
 * `yaml` package loads with the lazy YAML tab, not with the Make your own page (library-* budget).
 */
import { parseDocument, stringify } from "yaml";

import { tidyDoc } from "./doc";
import { KIND_DOC, type LibraryDoc, type LibraryKind } from "./types";

/** The document as YAML (the builders' YAML tab). */
export function docToYaml(doc: LibraryDoc): string {
  return stringify(tidyDoc(doc), { lineWidth: 0, aliasDuplicateObjects: false });
}

export type YamlParse<K extends LibraryKind> = { ok: true; doc: LibraryDoc & { kind: (typeof KIND_DOC)[K] } } | { ok: false; error: string };

/**
 * YAML text back to a document of `kind` (the YAML tab's edits). Refuses text that does not
 * parse, that holds anchors and aliases (no merge bombs), or that is another kind.
 */
export function yamlToDoc<K extends LibraryKind>(text: string, kind: K, guard: (v: unknown) => boolean): YamlParse<K> {
  if (new TextEncoder().encode(text).length > 262_144) return { ok: false, error: "The YAML is larger than 256 KiB." };
  const parsed = parseDocument(text, { uniqueKeys: true, prettyErrors: false });
  const first = parsed.errors[0];
  if (first) return { ok: false, error: first.message.split("\n")[0] ?? "The YAML is not valid." };
  let value: unknown;
  try {
    value = parsed.toJS({ maxAliasCount: 0 });
  } catch (e) {
    return { ok: false, error: e instanceof Error ? (e.message.split("\n")[0] ?? "The YAML is not valid.") : "The YAML is not valid." };
  }
  if (typeof value !== "object" || value === null || (value as { kind?: unknown }).kind !== KIND_DOC[kind]) {
    return { ok: false, error: `The document must be "kind: ${KIND_DOC[kind]}".` };
  }
  if (!guard(value)) return { ok: false, error: "The document is missing a part the builder needs (metadata with a name and a title, and the parts of its kind)." };
  return { ok: true, doc: value as LibraryDoc & { kind: (typeof KIND_DOC)[K] } };
}

