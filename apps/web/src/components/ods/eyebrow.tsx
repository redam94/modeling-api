/** The ODS eyebrow: a 10px bold uppercase label in the hint ink, above a value or a section. */
import type * as React from "react";

import { cn } from "@/lib/utils";

export function Eyebrow({
  as: Tag = "p",
  className,
  ...props
}: React.HTMLAttributes<HTMLElement> & { as?: "p" | "span" | "div" | "h2" | "h3" | "dt" }) {
  return <Tag data-slot="eyebrow" className={cn("eyebrow", className)} {...props} />;
}
