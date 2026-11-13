import { useEffect, useRef, useState } from "react";

/**
 * `value` after it has stopped changing for `ms`. An object or an array is compared by its
 * JSON, so a new object with the same content (a builder makes one on each render) does not
 * start the wait again.
 */
export function useDebounced<T>(value: T, ms: number): T {
  const [out, setOut] = useState(value);
  const key: unknown = typeof value === "object" && value !== null ? JSON.stringify(value) : value;
  const latest = useRef(value);
  latest.current = value;
  useEffect(() => {
    const t = setTimeout(() => setOut(latest.current), ms);
    return () => clearTimeout(t);
  }, [key, ms]);
  return out;
}
