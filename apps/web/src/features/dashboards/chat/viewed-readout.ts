/**
 * Which readout the viewer is looking at, per dashboard client (v0.20, web+docs-1). The readout
 * history writes the readout it shows (`null`: the latest one, the overview); the chat launcher
 * reads it and sends it as the question's `readout_seq`, so "this readout" is the readout on
 * the page. Keyed by the client's `cacheKey`, so a member, a client user and a share link never
 * see each other's choice. Tiny on purpose: the share app loads it with the launcher.
 */
import { useSyncExternalStore } from "react";

const viewed = new Map<string, number>();
const listeners = new Set<() => void>();

function keyOf(cacheKey: readonly string[]): string {
  return cacheKey.join("\u0000");
}

/** Record the readout on the page (`null`: the latest one). */
export function setViewedReadout(cacheKey: readonly string[], seq: number | null): void {
  const key = keyOf(cacheKey);
  if (seq === null ? !viewed.has(key) : viewed.get(key) === seq) return;
  if (seq === null) viewed.delete(key);
  else viewed.set(key, seq);
  for (const l of listeners) l();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The readout on the page for this client, or `null` for the latest one. */
export function useViewedReadout(cacheKey: readonly string[] | undefined): number | null {
  return useSyncExternalStore(
    subscribe,
    () => (cacheKey ? (viewed.get(keyOf(cacheKey)) ?? null) : null),
    () => null,
  );
}
