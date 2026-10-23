/**
 * Names of the people on uploaded code (uploader, reviewers): "you", a member's display name
 * from the tenant member list when the caller may read it, else a short id. Names are user
 * data: text only.
 */
import { useMemo } from "react";

import { useMembers } from "@/api/queries";
import { useWorkspace } from "@/features/auth/workspace";
import { shortId } from "@/lib/format";

export type NameOf = (userId: string | null | undefined) => string;

export function useNameOf(tenantId: string): NameOf {
  const ws = useWorkspace();
  const members = useMembers(ws.mode === "session" ? tenantId : "", null);
  const me = ws.user.id;
  const names = useMemo(() => {
    const m = new Map<string, string>();
    for (const x of members.data?.items ?? []) {
      const name = x.display_name || x.email;
      if (name) m.set(x.principal_id, name);
    }
    return m;
  }, [members.data]);
  return useMemo<NameOf>(() => (id) => (!id ? "someone" : id === me ? "you" : (names.get(id) ?? `user ${shortId(id)}`)), [names, me]);
}
