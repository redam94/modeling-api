/**
 * Names of the people on uploaded code (uploader, reviewers): "you", a member's display name
 * from the tenant member list when the caller may read it, else a short id. Names are user
 * data: text only.
 *
 * v0.19 live: an attested editor is usually a project member, not a tenant member, so admins
 * saw "user usr_…" for them; the verified-uploader list (admins may read it) names them too.
 */
import { useMemo } from "react";

import { useMembers } from "@/api/queries";
import { useWorkspace } from "@/features/auth/workspace";
import { shortId } from "@/lib/format";

import { usePluginUploaders } from "./api";

export type NameOf = (userId: string | null | undefined) => string;

export function useNameOf(tenantId: string): NameOf {
  const ws = useWorkspace();
  const members = useMembers(ws.mode === "session" ? tenantId : "", null);
  const admin = ws.grants.tenantRole === "admin";
  const uploaders = usePluginUploaders(tenantId, ws.mode === "session" && admin);
  const me = ws.user.id;
  const names = useMemo(() => {
    const m = new Map<string, string>();
    for (const x of uploaders.data ?? []) {
      const name = x.display_name || x.email;
      if (name) m.set(x.user_id, name);
    }
    for (const x of members.data?.items ?? []) {
      const name = x.display_name || x.email;
      if (name) m.set(x.principal_id, name);
    }
    return m;
  }, [members.data, uploaders.data]);
  return useMemo<NameOf>(() => (id) => (!id ? "someone" : id === me ? "you" : (names.get(id) ?? `user ${shortId(id)}`)), [names, me]);
}
