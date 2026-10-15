/** RFC 6901 JSON pointers. */

export function parsePointer(pointer: string): string[] {
  if (pointer === "" || pointer === "/") return pointer === "/" ? [""] : [];
  const raw = pointer.startsWith("#") ? decodeURIComponent(pointer.slice(1)) : pointer;
  if (!raw.startsWith("/")) throw new Error(`invalid JSON pointer ${JSON.stringify(pointer)}`);
  return raw
    .slice(1)
    .split("/")
    .map((s) => s.replace(/~1/g, "/").replace(/~0/g, "~"));
}

export function formatPointer(segments: readonly (string | number)[]): string {
  return segments.map((s) => `/${String(s).replace(/~/g, "~0").replace(/\//g, "~1")}`).join("");
}

/** True if `pointer` equals `prefix` or lies below it. */
export function pointerWithin(pointer: string, prefix: string): boolean {
  return pointer === prefix || pointer.startsWith(`${prefix}/`);
}
