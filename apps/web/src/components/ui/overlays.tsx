/**
 * Radix-based overlays and toggles in the shadcn/ui new-york style: Dialog, DropdownMenu,
 * Tooltip, Tabs, Switch, Checkbox, ToggleGroup.
 */
import { Check, X } from "lucide-react";
import {
  Checkbox as CheckboxPrimitive,
  Dialog as DialogPrimitive,
  DropdownMenu as MenuPrimitive,
  Switch as SwitchPrimitive,
  Tabs as TabsPrimitive,
  ToggleGroup as ToggleGroupPrimitive,
  Tooltip as TooltipPrimitive,
} from "radix-ui";
import type * as React from "react";

import { cn } from "@/lib/utils";

// ----------------------------------------------------------------------- Dialog

export const Dialog = DialogPrimitive.Root;
export const DialogTrigger = DialogPrimitive.Trigger;
export const DialogClose = DialogPrimitive.Close;

export function DialogContent({
  className,
  children,
  wide,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Content> & { wide?: boolean }) {
  return (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-overlay/60" />
      <DialogPrimitive.Content
        className={cn(
          "fixed top-[12vh] left-1/2 z-50 grid max-h-[80vh] w-[calc(100%-2rem)] -translate-x-1/2 gap-4 overflow-y-auto rounded-xl border bg-popover p-6 text-popover-foreground shadow-lg max-sm:p-4",
          wide ? "max-w-3xl" : "max-w-lg",
          className,
        )}
        {...props}
      >
        {children}
        <DialogPrimitive.Close data-dialog-close="" className="focus-ring absolute top-4 right-4 rounded-sm p-1 text-muted-foreground hover:text-foreground">
          <X className="size-4" />
          <span className="sr-only">Close</span>
        </DialogPrimitive.Close>
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
}

export function DialogTitle({ className, ...props }: React.ComponentProps<typeof DialogPrimitive.Title>) {
  return <DialogPrimitive.Title className={cn("text-lg font-semibold", className)} {...props} />;
}

export function DialogDescription({ className, ...props }: React.ComponentProps<typeof DialogPrimitive.Description>) {
  return <DialogPrimitive.Description className={cn("text-sm text-muted-foreground", className)} {...props} />;
}

export function DialogFooter({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("flex justify-end gap-2 pt-2", className)} {...props} />;
}

// ----------------------------------------------------------------------- DropdownMenu

export const DropdownMenu = MenuPrimitive.Root;
export const DropdownMenuTrigger = MenuPrimitive.Trigger;

export function DropdownMenuContent({ className, sideOffset = 6, ...props }: React.ComponentProps<typeof MenuPrimitive.Content>) {
  return (
    <MenuPrimitive.Portal>
      <MenuPrimitive.Content
        sideOffset={sideOffset}
        className={cn("z-50 min-w-40 rounded-lg border bg-popover p-1 text-popover-foreground shadow-md", className)}
        {...props}
      />
    </MenuPrimitive.Portal>
  );
}

export function DropdownMenuItem({ className, ...props }: React.ComponentProps<typeof MenuPrimitive.Item>) {
  return (
    <MenuPrimitive.Item
      className={cn(
        "flex min-h-9 cursor-default items-center gap-2 rounded-md px-2 py-1.5 text-sm outline-none select-none data-[disabled]:opacity-50 data-[highlighted]:bg-accent data-[highlighted]:text-accent-foreground data-[highlighted]:shadow-[inset_3px_0_0_var(--ring)] [&_svg]:size-4",
        className,
      )}
      {...props}
    />
  );
}

export function DropdownMenuLabel({ className, ...props }: React.ComponentProps<typeof MenuPrimitive.Label>) {
  return <MenuPrimitive.Label className={cn("px-2 py-1.5 text-xs text-hint", className)} {...props} />;
}

export function DropdownMenuSeparator({ className, ...props }: React.ComponentProps<typeof MenuPrimitive.Separator>) {
  return <MenuPrimitive.Separator className={cn("-mx-1 my-1 h-px bg-border", className)} {...props} />;
}

export const DropdownMenuRadioGroup = MenuPrimitive.RadioGroup;

export function DropdownMenuRadioItem({ className, children, ...props }: React.ComponentProps<typeof MenuPrimitive.RadioItem>) {
  return (
    <MenuPrimitive.RadioItem
      className={cn(
        "relative flex min-h-9 cursor-default items-center gap-2 rounded-md py-1.5 pr-2 pl-7 text-sm outline-none select-none data-[highlighted]:bg-accent data-[highlighted]:text-accent-foreground data-[highlighted]:shadow-[inset_3px_0_0_var(--ring)]",
        className,
      )}
      {...props}
    >
      <span className="absolute left-2 flex size-3.5 items-center justify-center">
        <MenuPrimitive.ItemIndicator>
          <span className="size-1.5 rounded-full bg-primary" />
        </MenuPrimitive.ItemIndicator>
      </span>
      {children}
    </MenuPrimitive.RadioItem>
  );
}

// ----------------------------------------------------------------------- Tooltip

export const TooltipProvider = TooltipPrimitive.Provider;

export function Tooltip({ content, children, side = "top" }: { content: React.ReactNode; children: React.ReactNode; side?: "top" | "right" | "bottom" | "left" }) {
  return (
    <TooltipPrimitive.Root>
      <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
      <TooltipPrimitive.Portal>
        <TooltipPrimitive.Content
          side={side}
          sideOffset={6}
          className="z-50 max-w-xs rounded-md bg-tooltip px-2 py-1.5 text-xs text-tooltip-foreground shadow-sm"
        >
          {content}
        </TooltipPrimitive.Content>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  );
}

// ----------------------------------------------------------------------- Tabs

export const Tabs = TabsPrimitive.Root;

export function TabsList({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.List>) {
  return (
    <TabsPrimitive.List
      // Scrolls sideways instead of widening the page on narrow screens. The baseline is an inset
      // shadow (not a border) so the active tab's underline is not clipped by the overflow box.
      className={cn(
        "inline-flex h-10 max-w-full items-center gap-8 overflow-x-auto overflow-y-hidden text-muted-foreground shadow-[inset_0_-1px_0_var(--color-border)] [scrollbar-width:thin]",
        className,
      )}
      {...props}
    />
  );
}

export function TabsTrigger({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.Trigger>) {
  return (
    <TabsPrimitive.Trigger
      className={cn(
        // An inset ring: the list scrolls (overflow), which would clip an outer focus ring.
        "inline-flex h-10 shrink-0 items-center gap-1.5 rounded-t-sm border-b-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset border-transparent text-sm font-semibold whitespace-nowrap text-muted-foreground transition-colors hover:text-primary-hover data-[state=active]:border-primary data-[state=active]:text-primary [&_svg]:size-4",
        className,
      )}
      {...props}
    />
  );
}

export function TabsContent({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.Content>) {
  return <TabsPrimitive.Content className={cn("pt-5 outline-none", className)} {...props} />;
}

// ----------------------------------------------------------------------- Switch

export function Switch({ className, ...props }: React.ComponentProps<typeof SwitchPrimitive.Root>) {
  return (
    <SwitchPrimitive.Root
      className={cn(
        "focus-ring peer inline-flex h-6 w-10 shrink-0 items-center rounded-full border border-transparent bg-input transition-colors disabled:cursor-not-allowed disabled:opacity-50 data-[state=checked]:bg-primary",
        className,
      )}
      {...props}
    >
      <SwitchPrimitive.Thumb className="pointer-events-none block size-5 translate-x-0.5 rounded-full bg-card shadow-xs transition-transform data-[state=checked]:translate-x-[18px]" />
    </SwitchPrimitive.Root>
  );
}

// ----------------------------------------------------------------------- Checkbox

export function Checkbox({ className, ...props }: React.ComponentProps<typeof CheckboxPrimitive.Root>) {
  return (
    <CheckboxPrimitive.Root
      className={cn(
        "peer grid size-4 shrink-0 place-items-center rounded-sm border border-input bg-card transition-colors focus-ring disabled:cursor-not-allowed disabled:opacity-50 data-[state=checked]:border-primary data-[state=checked]:bg-primary data-[state=checked]:text-primary-foreground",
        className,
      )}
      {...props}
    >
      <CheckboxPrimitive.Indicator>
        <Check className="size-3" strokeWidth={3} />
      </CheckboxPrimitive.Indicator>
    </CheckboxPrimitive.Root>
  );
}

// ----------------------------------------------------------------------- ToggleGroup

export function SegmentedControl<T extends string>({
  value,
  onValueChange,
  options,
  label,
  className,
}: {
  value: T;
  onValueChange: (v: T) => void;
  options: { value: T; label: React.ReactNode; title?: string }[];
  label: string;
  className?: string;
}) {
  return (
    <ToggleGroupPrimitive.Root
      type="single"
      aria-label={label}
      value={value}
      onValueChange={(v) => {
        if (v) onValueChange(v as T);
      }}
      // Too many options for a narrow screen scroll inside the strip, never the page.
      className={cn("inline-flex h-8 max-w-full items-center gap-0.5 overflow-x-auto rounded-lg bg-border p-0.5 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden", className)}
    >
      {options.map((o) => (
        <ToggleGroupPrimitive.Item
          key={o.value}
          value={o.value}
          title={o.title}
          className="focus-ring inline-flex h-7 shrink-0 items-center gap-1.5 rounded-md px-2.5 text-xs whitespace-nowrap font-medium text-muted-foreground transition-colors hover:text-foreground data-[state=on]:bg-card data-[state=on]:text-foreground data-[state=on]:shadow-xs [&_svg]:size-3.5"
        >
          {o.label}
        </ToggleGroupPrimitive.Item>
      ))}
    </ToggleGroupPrimitive.Root>
  );
}
