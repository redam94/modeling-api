/**
 * Which spec revision the editor text belongs to, and what a save may claim as its parent.
 *
 * The server's optimistic-concurrency check compares `parent_revision` with the spec's latest
 * revision, so the client must send the revision the text was *loaded from*, not whatever
 * latest revision its cache happens to hold.
 */

/**
 * Whether the editor should replace its text with the revision `incomingKey` (`spec@rev`).
 *
 * `loadedKey` is the revision already in the editor, or `null` for the first load and for an
 * explicit load the user asked for (after agreeing to discard edits). A revision change from
 * anywhere else, such as a refetch or history navigation, never overwrites unsaved text.
 */
export function shouldLoadRevision(incomingKey: string, loadedKey: string | null, dirty: boolean): boolean {
  if (loadedKey === null) return true;
  if (incomingKey === loadedKey) return false;
  return !dirty;
}

/**
 * The `parent_revision` to send when saving text based on revision `baseRev`.
 *
 * Normally that is `baseRev` itself, so a concurrent save elsewhere makes the server answer
 * CONFLICT. When the text is based on an older revision than the known latest, saving would
 * replace the latest; `overwrites` names that revision so the caller can ask first, and the
 * parent becomes the latest the user agreed to replace.
 */
export function saveParent(baseRev: number, latest: number | undefined): { parent: number; overwrites: number | null } {
  if (latest !== undefined && baseRev < latest) return { parent: latest, overwrites: latest };
  return { parent: baseRev, overwrites: null };
}
