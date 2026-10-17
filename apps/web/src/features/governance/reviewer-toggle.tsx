/**
 * The Members page's reviewer column: grant or revoke the `reviewer` capability (approves
 * validations). Project admins hold it implicitly; viewers cannot hold it (approval needs the
 * editor role or higher).
 */
import { atLeast } from "@/api/access";
import { errorMessage } from "@/api/client";
import type { Membership } from "@/api/types";
import { Switch } from "@/components/ui/overlays";
import { Badge } from "@/components/ui/primitives";
import { toast } from "@/stores/toast";

export function ReviewerToggle({
  member,
  name,
  editable,
  pending,
  onChange,
}: {
  member: Membership;
  name: string;
  editable: boolean;
  pending: boolean;
  onChange: (capabilities: NonNullable<Membership["capabilities"]>, done: { onSuccess: () => void; onError: (e: unknown) => void }) => void;
}) {
  const caps = member.capabilities ?? [];
  const has = caps.includes("reviewer");
  if (member.role === "admin") {
    return (
      <Badge variant="outline" title="Admins approve validations without a separate grant">
        implicit
      </Badge>
    );
  }
  if (!atLeast(member.role, "editor")) {
    return <span className="text-xs text-muted-foreground" title="Reviewers need the editor role or higher">—</span>;
  }
  if (!editable) return has ? <Badge variant="info">reviewer</Badge> : <span className="text-xs text-muted-foreground">no</span>;
  return (
    <Switch
      checked={has}
      disabled={pending}
      aria-label={`${name} can approve validations`}
      onCheckedChange={(on) =>
        onChange(on ? [...new Set([...caps, "reviewer" as const])] : caps.filter((c) => c !== "reviewer"), {
          onSuccess: () => toast.success(on ? "Reviewer added" : "Reviewer removed", on ? `${name} can now approve validations.` : `${name} can no longer approve validations.`),
          onError: (e) => toast.error("The reviewer capability was not changed", errorMessage(e)),
        })
      }
    />
  );
}
