/**
 * The welcome dialog (v0.18 D24): a friendly card on a first signed-in visit that offers
 * the two-minute tour, the sample project, or "Not now".
 *
 * It is **non-modal** (Radix Dialog with `modal={false}` and no scrim): the page behind it
 * stays clickable and readable by screen readers, and clicking elsewhere does not lose it.
 * Esc and "Not now" close it. Every choice records `welcomed` on the server, so it shows
 * once per user per tenant. It sits in the lower corner (a bottom sheet on a phone) on the
 * dark block, with the one inverted ink action.
 */
import { Dialog as DialogPrimitive } from "radix-ui";

import { Logo } from "@/components/brand/logo";
import { cn } from "@/lib/utils";

export type WelcomeChoice = "tour" | "sample" | "later";

export interface WelcomeDialogProps {
  /** Offer the sample project (samples are on for this tenant). */
  sampleOffered: boolean;
  /** The sample is being made or is ready (changes the button's words). */
  sampleStarted: boolean;
  onChoose: (choice: WelcomeChoice) => void;
}

const pill = "inline-flex min-h-11 items-center justify-center rounded-full px-5 text-sm font-semibold outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-block-foreground";

export function WelcomeDialog({ sampleOffered, sampleStarted, onChoose }: WelcomeDialogProps) {
  return (
    <DialogPrimitive.Root open modal={false} onOpenChange={(open) => (open ? undefined : onChoose("later"))}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Content
          data-testid="welcome-dialog"
          aria-describedby="welcome-dialog-body"
          // clicking the page leaves the card where it is (it never blocks that click)
          onInteractOutside={(e) => e.preventDefault()}
          onCloseAutoFocus={(e) => e.preventDefault()}
          className={cn(
            "animate-coach-in fixed z-[65] grid gap-4 overflow-hidden bg-block p-6 text-block-foreground shadow-lg",
            "inset-x-0 bottom-0 max-h-[85svh] overflow-y-auto rounded-t-3xl pb-[max(1.5rem,env(safe-area-inset-bottom))]",
            "sm:inset-x-auto sm:right-6 sm:bottom-6 sm:w-[420px] sm:rounded-3xl sm:pb-6",
          )}
        >
          <div className="flex items-start gap-4">
            <div className="grid min-w-0 flex-1 gap-2">
              <p className="text-[0.8125rem] font-semibold text-block-muted">Welcome</p>
              <DialogPrimitive.Title className="display-title text-2xl leading-tight">Hi there! Fancy a quick look around?</DialogPrimitive.Title>
            </div>
            <Logo className="size-9 max-sm:hidden" />
          </div>
          <DialogPrimitive.Description id="welcome-dialog-body" className="text-sm text-block-foreground">
            {sampleOffered
              ? "Take a two-minute tour, or open a sample project: a real marketing-mix model on example data, fitted for you in a couple of minutes. You can find both again in your menu."
              : "Take a two-minute tour of where things live. You can find it again in your menu any time."}
          </DialogPrimitive.Description>
          <div className="flex flex-wrap items-center gap-2">
            <button type="button" className={cn(pill, "bg-block-foreground text-block hover:bg-block-foreground/85")} onClick={() => onChoose("tour")}>
              Take the 2-minute tour
            </button>
            {sampleOffered ? (
              <button type="button" className={cn(pill, "shadow-[inset_0_0_0_1.5px_currentColor] hover:bg-block-raised")} onClick={() => onChoose("sample")}>
                {sampleStarted ? "Open the sample project" : "Explore the sample project"}
              </button>
            ) : null}
            <button
              type="button"
              className="min-h-11 rounded-md px-2 text-sm text-block-muted underline underline-offset-4 outline-none hover:text-block-foreground focus-visible:outline-2 focus-visible:outline-block-foreground"
              onClick={() => onChoose("later")}
            >
              Not now
            </button>
          </div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
