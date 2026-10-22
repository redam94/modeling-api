/**
 * A text field for a short list ("a, b, c"). The field keeps the text as typed, so a comma or a
 * space typed before the next item stays on screen; the list it reports is the trimmed, non-empty
 * items. When the list changes from outside (a reset, another control), the text follows it.
 */
import { useEffect, useState } from "react";
import type * as React from "react";

import { Input } from "@/components/ui/primitives";

/** The items of `text`: split on commas, trimmed, empty ones left out, at most `max`. */
export function parseList(text: string, max = Infinity): string[] {
  return text
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean)
    .slice(0, max);
}

function same(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((x, i) => x === b[i]);
}

export function ListInput({
  value,
  onChange,
  max,
  ...props
}: { value: readonly string[]; onChange: (next: string[]) => void; max?: number } & Omit<React.ComponentProps<typeof Input>, "value" | "onChange">) {
  const [text, setText] = useState(() => value.join(", "));
  useEffect(() => {
    setText((t) => (same(parseList(t, max), value) ? t : value.join(", ")));
  }, [value, max]);
  return (
    <Input
      {...props}
      value={text}
      onChange={(e) => {
        setText(e.target.value);
        onChange(parseList(e.target.value, max));
      }}
      onBlur={(e) => {
        setText(parseList(e.target.value, max).join(", "));
        props.onBlur?.(e);
      }}
    />
  );
}
