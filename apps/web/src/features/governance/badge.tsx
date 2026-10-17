/**
 * The validation status of a model version as a badge (shared by the governance screens, the
 * model pages and the Model Explorer). `null` means no validation was ever requested.
 */
import { ShieldCheck, ShieldQuestion } from "lucide-react";

import { Badge } from "@/components/ui/primitives";

import { TONE_BADGE, VALIDATION_META } from "./status";
import type { ValidationStatus } from "./types";

export function ValidationBadge({ status, className }: { status: ValidationStatus | null; className?: string }) {
  if (!status) {
    return (
      <Badge variant="outline" className={className} title="No validation was requested for this version." data-validation-status="none">
        <ShieldQuestion aria-hidden /> Not validated
      </Badge>
    );
  }
  const meta = VALIDATION_META[status];
  return (
    <Badge variant={TONE_BADGE[meta.tone]} className={className} title={meta.plain} data-validation-status={status}>
      {status === "validated" ? <ShieldCheck aria-hidden /> : null}
      {meta.label}
    </Badge>
  );
}
