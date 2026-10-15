/** Which spec revision the editor text belongs to. */

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
