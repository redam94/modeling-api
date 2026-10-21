import { cva, type VariantProps } from "class-variance-authority";
import { Slot } from "radix-ui";
import type * as React from "react";

import { cn } from "@/lib/utils";

export const buttonVariants = cva(
  // focus-ring (app.css): the ODS focus treatment, a surface-coloured gap and then the ring.
  "focus-ring inline-flex shrink-0 items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-semibold transition-colors disabled:pointer-events-none [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default:
          "bg-primary text-primary-foreground hover:bg-primary-hover active:bg-primary-active disabled:bg-brand-300 dark:disabled:opacity-50",
        destructive: "bg-destructive text-destructive-foreground hover:bg-destructive/90 disabled:opacity-50",
        // Secondary and outline are one style: a white button with a brand ring.
        secondary:
          "bg-card text-primary ring-1 ring-primary ring-inset hover:bg-accent disabled:text-hint disabled:ring-border-strong",
        outline:
          "bg-card text-primary ring-1 ring-primary ring-inset hover:bg-accent disabled:text-hint disabled:ring-border-strong",
        ghost: "hover:bg-surface-hover hover:text-foreground disabled:opacity-50",
        link: "text-primary underline-offset-4 hover:text-primary-hover hover:underline disabled:opacity-50",
        // A text-only primary action ("Edit", "Show all").
        action: "text-primary hover:bg-accent hover:text-primary-hover disabled:text-hint",
      },
      size: {
        default: "h-10 px-5",
        sm: "h-8 px-4",
        lg: "h-12 px-6",
        icon: "size-10",
        "icon-sm": "size-8",
      },
    },
    compoundVariants: [{ variant: "action", class: "px-2" }],
    defaultVariants: { variant: "default", size: "default" },
  },
);

export interface ButtonProps extends React.ComponentProps<"button">, VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

export function Button({ className, variant, size, asChild = false, type, ...props }: ButtonProps) {
  const Comp = asChild ? Slot.Root : "button";
  return (
    <Comp
      data-slot="button"
      className={cn(buttonVariants({ variant, size, className }))}
      {...(asChild ? {} : { type: type ?? "button" })}
      {...props}
    />
  );
}
