/** "Back to Guided mode" for a spec Guided mode drafted (the Expert half of the round trip). */
import { Link } from "@tanstack/react-router";
import { Sparkles } from "lucide-react";

import { useSetPersona } from "./persona";
import { guidedTemplateOf } from "./session";

export function GuidedReturnLink({ specId, document }: { specId: string | null; document: unknown }) {
  const setPersona = useSetPersona();
  if (!specId || !guidedTemplateOf(document as Record<string, unknown> | null)) return null;
  return (
    <Link to="/guided/$specId" params={{ specId }} onClick={() => setPersona("guided")} className="inline-flex items-center gap-1 font-medium text-primary hover:underline">
      <Sparkles className="size-3.5" aria-hidden /> Back to Guided mode
    </Link>
  );
}
