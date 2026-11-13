/**
 * Which modifier key the platform's shortcuts use. Read at render time (not at import), so a
 * test can set `navigator.platform`.
 */
interface UaData {
  platform?: string;
}

/** Is this an Apple platform (macOS, iOS, iPadOS)? `false` when the browser does not say. */
export function isApplePlatform(): boolean {
  if (typeof navigator === "undefined") return false;
  const hinted = (navigator as Navigator & { userAgentData?: UaData }).userAgentData?.platform;
  return /mac|iphone|ipad|ipod/i.test(hinted || navigator.platform || "");
}

/** The key hint of a `mod+<key>` shortcut: "⌘K" on Apple platforms, "Ctrl K" elsewhere. */
export function modKeyHint(key: string): string {
  return isApplePlatform() ? `⌘${key}` : `Ctrl ${key}`;
}
