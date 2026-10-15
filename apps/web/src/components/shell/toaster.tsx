import { CheckCircle2, Info, X, XCircle } from "lucide-react";

import { cn } from "@/lib/utils";
import { useToasts } from "@/stores/toast";

export function Toaster() {
  const toasts = useToasts((s) => s.toasts);
  const dismiss = useToasts((s) => s.dismiss);
  return (
    <div aria-live="polite" className="pointer-events-none fixed right-4 bottom-4 z-[60] grid w-80 gap-2">
      {toasts.map((t) => {
        const Icon = t.tone === "success" ? CheckCircle2 : t.tone === "error" ? XCircle : Info;
        return (
          <div
            key={t.id}
            role={t.tone === "error" ? "alert" : "status"}
            className="pointer-events-auto flex items-start gap-3 rounded-lg border bg-popover p-3 text-[13px] shadow-lg"
          >
            <Icon
              className={cn(
                "mt-0.5 size-4 shrink-0",
                t.tone === "success" && "text-success",
                t.tone === "error" && "text-destructive",
                t.tone === "info" && "text-info",
              )}
            />
            <div className="min-w-0 flex-1">
              <p className="font-medium">{t.title}</p>
              {t.description ? <p className="text-muted-foreground">{t.description}</p> : null}
            </div>
            <button type="button" aria-label="Dismiss" onClick={() => dismiss(t.id)} className="text-muted-foreground hover:text-foreground">
              <X className="size-4" />
            </button>
          </div>
        );
      })}
    </div>
  );
}
