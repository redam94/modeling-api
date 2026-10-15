/**
 * Small shadcn/ui (new-york) equivalents: Badge, Card, Input, Textarea, Label, NativeSelect,
 * Separator, Skeleton, Kbd, Progress, Table. Hand-written to match the generated components.
 */
import { cva, type VariantProps } from "class-variance-authority";
import { Label as LabelPrimitive, Separator as SeparatorPrimitive } from "radix-ui";
import type * as React from "react";
import { createContext, useContext } from "react";

import { cn } from "@/lib/utils";

// ----------------------------------------------------------------------- Badge

export const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-xs font-medium whitespace-nowrap [&_svg]:size-3",
  {
    variants: {
      variant: {
        default: "border-transparent bg-primary text-primary-foreground",
        secondary: "border-transparent bg-secondary text-secondary-foreground",
        outline: "border-border text-foreground",
        success: "border-success/30 bg-success/12 text-success",
        warning: "border-warning/35 bg-warning/14 text-warning",
        danger: "border-destructive/30 bg-destructive/12 text-destructive",
        info: "border-info/30 bg-info/12 text-info",
      },
    },
    defaultVariants: { variant: "secondary" },
  },
);

export function Badge({
  className,
  variant,
  ...props
}: React.ComponentProps<"span"> & VariantProps<typeof badgeVariants>) {
  return <span data-slot="badge" className={cn(badgeVariants({ variant }), className)} {...props} />;
}

// ----------------------------------------------------------------------- Card

export function Card({ className, ...props }: React.ComponentProps<"section">) {
  return (
    <section
      data-slot="card"
      className={cn("rounded-lg border bg-card text-card-foreground shadow-[0_1px_0_0_rgb(0_0_0/0.03)]", className)}
      {...props}
    />
  );
}

export function CardHeader({ className, ...props }: React.ComponentProps<"header">) {
  return <header className={cn("flex items-start justify-between gap-4 px-5 pt-4 pb-3", className)} {...props} />;
}

export function CardTitle({ className, ...props }: React.ComponentProps<"h2">) {
  return <h2 className={cn("text-[15px] leading-6 font-semibold tracking-tight", className)} {...props} />;
}

export function CardDescription({ className, ...props }: React.ComponentProps<"p">) {
  return <p className={cn("text-muted-foreground text-[13px]", className)} {...props} />;
}

export function CardContent({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("px-5 pb-5", className)} {...props} />;
}

// ----------------------------------------------------------------------- Inputs

const fieldBase =
  "w-full min-w-0 rounded-md border border-input bg-card px-3 text-sm shadow-xs transition-colors placeholder:text-muted-foreground disabled:cursor-not-allowed disabled:opacity-50 aria-invalid:border-destructive focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ring";

/** Set by {@link Field} for the control whose id is `controlId`. */
interface FieldState {
  controlId: string;
  invalid: boolean;
  describedBy: string | undefined;
}

const FieldContext = createContext<FieldState | null>(null);

/**
 * `aria-invalid` / `aria-describedby` for a control inside a {@link Field}, applied only to
 * the control the label points at; explicit props win.
 */
function useFieldAria(
  id: string | undefined,
  props: Pick<React.AriaAttributes, "aria-invalid" | "aria-describedby">,
): Pick<React.AriaAttributes, "aria-invalid" | "aria-describedby"> {
  const field = useContext(FieldContext);
  if (!field || !id || field.controlId !== id) return {};
  return {
    "aria-invalid": props["aria-invalid"] ?? (field.invalid ? true : undefined),
    "aria-describedby": props["aria-describedby"] ?? field.describedBy,
  };
}

export function Input({ className, type = "text", ...props }: React.ComponentProps<"input">) {
  const aria = useFieldAria(props.id, props);
  return <input type={type} data-slot="input" className={cn(fieldBase, "h-9 py-1", className)} {...props} {...aria} />;
}

export function Textarea({ className, ...props }: React.ComponentProps<"textarea">) {
  const aria = useFieldAria(props.id, props);
  return <textarea data-slot="textarea" className={cn(fieldBase, "min-h-20 py-2", className)} {...props} {...aria} />;
}

export function NativeSelect({ className, children, ...props }: React.ComponentProps<"select">) {
  const aria = useFieldAria(props.id, props);
  return (
    <select data-slot="select" className={cn(fieldBase, "h-9 py-1 pr-8", className)} {...props} {...aria}>
      {children}
    </select>
  );
}

export function Label({ className, ...props }: React.ComponentProps<typeof LabelPrimitive.Root>) {
  return (
    <LabelPrimitive.Root
      data-slot="label"
      className={cn("text-[13px] leading-none font-medium select-none peer-disabled:opacity-50", className)}
      {...props}
    />
  );
}

/**
 * A labelled control with optional help text and a validation error. The error is shown in
 * the destructive colour above the help text (which stays), announced (`role="alert"`), and
 * tied to the control with `htmlFor`: the Input/Textarea/NativeSelect with that id gets
 * `aria-invalid` and an `aria-describedby` naming the error and the hint.
 */
export function Field({
  label,
  hint,
  error,
  htmlFor,
  children,
  className,
}: {
  label: string;
  hint?: React.ReactNode;
  error?: React.ReactNode;
  htmlFor?: string;
  children: React.ReactNode;
  className?: string;
}) {
  const errorId = htmlFor && error ? `${htmlFor}-error` : undefined;
  const hintId = htmlFor && hint ? `${htmlFor}-hint` : undefined;
  const describedBy = [errorId, hintId].filter(Boolean).join(" ") || undefined;
  return (
    <div className={cn("grid gap-1.5", className)}>
      <Label htmlFor={htmlFor}>{label}</Label>
      <FieldContext.Provider value={htmlFor ? { controlId: htmlFor, invalid: Boolean(error), describedBy } : null}>
        {children}
      </FieldContext.Provider>
      {error ? (
        <p id={errorId} role="alert" className="text-xs font-medium text-destructive">
          {error}
        </p>
      ) : null}
      {hint ? (
        <p id={hintId} className="text-muted-foreground text-xs">
          {hint}
        </p>
      ) : null}
    </div>
  );
}

// ----------------------------------------------------------------------- Misc

export function Separator({
  className,
  orientation = "horizontal",
  ...props
}: React.ComponentProps<typeof SeparatorPrimitive.Root>) {
  return (
    <SeparatorPrimitive.Root
      decorative
      orientation={orientation}
      className={cn("shrink-0 bg-border", orientation === "horizontal" ? "h-px w-full" : "h-full w-px", className)}
      {...props}
    />
  );
}

export function Skeleton({ className, ...props }: React.ComponentProps<"div">) {
  return <div aria-hidden className={cn("animate-pulse rounded-md bg-muted", className)} {...props} />;
}

export function Kbd({ className, ...props }: React.ComponentProps<"kbd">) {
  return (
    <kbd
      className={cn(
        "inline-flex h-5 min-w-5 items-center justify-center rounded border border-border bg-muted px-1 font-mono text-[11px] text-muted-foreground",
        className,
      )}
      {...props}
    />
  );
}

export function Progress({
  value,
  className,
  barClassName,
  label,
}: {
  value: number | null;
  className?: string;
  barClassName?: string;
  label: string;
}) {
  const pct = value == null ? null : Math.round(Math.min(1, Math.max(0, value)) * 100);
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      {...(pct == null ? {} : { "aria-valuenow": pct })}
      className={cn("relative h-1.5 w-full overflow-hidden rounded-full bg-muted", className)}
    >
      <div
        className={cn("h-full rounded-full bg-primary transition-[width]", pct == null && "w-1/3 animate-pulse", barClassName)}
        style={pct == null ? undefined : { width: `${pct}%` }}
      />
    </div>
  );
}

// ----------------------------------------------------------------------- Table

export function Table({ className, ...props }: React.ComponentProps<"table">) {
  return (
    // `contain: inline-size` keeps a wide table from widening its (grid) ancestors on narrow
    // screens: it scrolls inside its own box instead of making the page scroll sideways.
    <div className="relative w-full overflow-x-auto [contain:inline-size]">
      <table className={cn("w-full caption-bottom text-[13px]", className)} {...props} />
    </div>
  );
}

export function THead({ className, ...props }: React.ComponentProps<"thead">) {
  return <thead className={cn("[&_tr]:border-b", className)} {...props} />;
}

export function TBody({ className, ...props }: React.ComponentProps<"tbody">) {
  return <tbody className={cn("[&_tr:last-child]:border-0", className)} {...props} />;
}

export function TR({ className, ...props }: React.ComponentProps<"tr">) {
  return <tr className={cn("border-b transition-colors data-[state=selected]:bg-accent", className)} {...props} />;
}

export function TH({ className, ...props }: React.ComponentProps<"th">) {
  return (
    <th
      className={cn("h-9 px-3 text-left align-middle text-xs font-medium whitespace-nowrap text-muted-foreground", className)}
      {...props}
    />
  );
}

export function TD({ className, ...props }: React.ComponentProps<"td">) {
  return <td className={cn("px-3 py-2 align-middle", className)} {...props} />;
}
