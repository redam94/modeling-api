/**
 * People by name: user ids (`usr_…`) that the API returns (who promoted an endpoint, who rolled
 * it back) resolved through the selected project's members, which include the organisation's
 * tenant-wide members. An id nobody here can resolve (a former member, a service account
 * without a display name) stays an id.
 */
import { useCallback } from "react";

import { can } from "@/api/access";
import { useMembers } from "@/api/queries";
import { Mono } from "@/components/common";

import { useWorkspace } from "./workspace";

/** A resolver from principal id to a display name (`null` when unknown). */
export function usePeopleNames(): (id: string | null | undefined) => string | null {
  const ws = useWorkspace();
  const current = ws.current;
  const enabled = ws.mode === "session" && current !== null && can(ws.grants, "read", "member");
  const members = useMembers(enabled && current ? current.tenant.id : "", enabled && current ? current.project.project_id : null);
  const items = members.data?.items;
  return useCallback(
    (id: string | null | undefined) => {
      if (!id) return null;
      if (id === ws.user.id) return ws.user.name;
      const m = items?.find((x) => x.principal_id === id);
      return m ? m.display_name || m.email || null : null;
    },
    [items, ws.user.id, ws.user.name],
  );
}

/** A person's name (the id in the tooltip), or the id when the name is unknown. */
export function PersonName({ id, className }: { id: string; className?: string }) {
  const name = usePeopleNames()(id);
  return name ? (
    <span title={id} className={className}>
      {name}
    </span>
  ) : (
    <Mono className={className}>{id}</Mono>
  );
}
