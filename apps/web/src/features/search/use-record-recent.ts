/**
 * A visit to a detail page is a recent item of the global search (v0.21), like a thing opened
 * from the search: the same store, the same list of eight for each project, no duplicates (a
 * page that is visited again moves to the top).
 *
 * Signed-in pages only. The client portal and the share link have no search, and the bundle
 * budget keeps `features/search` out of their chunks.
 */
import { useEffect } from "react";

import { useProjectScope } from "@/api/queries";
import { useWorkspace } from "@/features/auth/workspace";

import { useRecents, type RecentItem } from "./recents";

/**
 * Call it on a detail page with what the search shows for the thing (`group`, `title`,
 * `secondary`, `href`), or with `null` until the page has its data. `href` is the page without
 * its tab or other search.
 */
export function useRecordRecent(item: RecentItem | null | undefined): void {
  const scope = useProjectScope().key;
  const add = useRecents((s) => s.add);
  const claim = useRecents((s) => s.claim);
  const me = useWorkspace().user.id;
  const group = item?.group;
  const title = item?.title;
  const secondary = item?.secondary;
  const href = item?.href;
  useEffect(() => {
    if (!group || !title || !href) return;
    // The lists of a different person (a session that ended without a sign-out) go first.
    claim(me);
    add(scope, { group, title, ...(secondary ? { secondary } : {}), href });
  }, [add, claim, me, scope, group, title, secondary, href]);
}
