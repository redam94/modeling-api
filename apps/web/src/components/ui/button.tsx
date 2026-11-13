import { cva, type VariantProps } from "class-variance-authority";
import { Slot } from "radix-ui";
import type * as React from "react";

import { cn } from "@/lib/utils";

export const buttonVariants = cva(
  // A pill. focus-ring (app.css): a surface-coloured gap and then the ink ring.
  "focus-ring inline-flex shrink-0 items-center justify-center gap-2 whitespace-nowrap rounded-full text-sm font-semibold transition-colors disabled:pointer-events-none [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default:
          "bg-primary text-primary-foreground hover:bg-primary-hover active:bg-primary-active disabled:bg-grey-400 dark:disabled:opacity-50",
        destructive: "bg-destructive text-destructive-foreground hover:bg-destructive/90 disabled:opacity-50",
        // Primary = ink fill (above), secondary = ink tint, outline = hairline.
        secondary: "bg-secondary text-foreground hover:bg-border-strong disabled:text-hint",
        outline: "text-foreground ring-1 ring-border-strong ring-inset hover:bg-accent disabled:text-hint",
        ghost: "hover:bg-surface-hover hover:text-foreground disabled:opacity-50",
        link: "link rounded-sm disabled:opacity-50",
        // A text-only primary action ("Edit", "Show all").
        action: "text-foreground hover:bg-accent disabled:text-hint",
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
