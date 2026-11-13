/**
 * Small shared helpers of the "from your team" lists (Catalog › Yours, the workflow gallery,
 * the Build palette). Pure; no React.
 */
import type { LibraryItem } from "@/features/library/types";
import { relativeTime } from "@/lib/format";

/** "Made by you · updated 2 hours ago". */
export function itemByline(item: Pick<LibraryItem, "created_by" | "updated_at">, me: string, now = Date.now()): string {
  const who = item.created_by === "system" ? "Added with the sample project" : item.created_by === me ? "Made by you" : "Made by your team";
  return `${who} · updated ${relativeTime(item.updated_at, now)}`;
}

/** Items worth listing outside Make your own (archived ones stay there). */
export function teamItems(items: readonly LibraryItem[]): LibraryItem[] {
  return items.filter((i) => !i.archived);
}
