import { CheckCircle2, Info, X, XCircle } from "lucide-react";

import { useToasts } from "@/stores/toast";

export function Toaster() {
  const toasts = useToasts((s) => s.toasts);
  const dismiss = useToasts((s) => s.dismiss);
  return (
    <div aria-live="polite" className="pointer-events-none fixed bottom-4 left-1/2 z-[60] grid w-[calc(100%-2rem)] max-w-[420px] -translate-x-1/2 gap-2">
      {toasts.map((t) => {
        const Icon = t.tone === "success" ? CheckCircle2 : t.tone === "error" ? XCircle : Info;
        return (
          <div
            key={t.id}
            role={t.tone === "error" ? "alert" : "status"}
            className="pointer-events-auto flex items-start gap-3 rounded-lg bg-tooltip px-4 py-3 text-sm text-tooltip-foreground shadow-lg"
          >
            {/* The status inks are tuned for light surfaces, so on the dark toast the icon shape
                carries the tone (tick, cross, i). */}
            <Icon className="mt-0.5 size-4 shrink-0" />
            <div className="min-w-0 flex-1">
              <p className="font-medium">{t.title}</p>
              {t.description ? <p className="text-tooltip-foreground/80">{t.description}</p> : null}
            </div>
            <button type="button" aria-label="Dismiss" onClick={() => dismiss(t.id)} className="focus-ring rounded-sm text-tooltip-foreground/80 hover:text-tooltip-foreground">
              <X className="size-4" />
            </button>
          </div>
        );
      })}
    </div>
  );
}
