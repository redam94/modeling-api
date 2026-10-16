/**
 * Guided or Expert (plan §11 "One shell, two modes"): the header toggle is remembered per user,
 * and a project admin can lock a project to Guided mode (`Project.guided_locked`). In a locked
 * project, members below admin see Guided screens only and no Expert toggle; the API still
 * enforces what they may store.
 */
import { useCallback, useEffect } from "react";

import { atLeast } from "@/api/access";
import { useWorkspace } from "@/features/auth/workspace";
import { personaOf, useUi, type Persona } from "@/stores/ui";

import { useProjectInfo } from "./api";

export interface GuidedLock {
  /** The project is locked to Guided mode. */
  projectLocked: boolean;
  /** …and the lock applies to this user (below project admin). */
  locked: boolean;
}

export function useGuidedLock(): GuidedLock {
  const ws = useWorkspace();
  const current = ws.current;
  const info = useProjectInfo(current?.tenant.id, current?.project.project_id, ws.mode === "session");
  const projectLocked = Boolean(info.data?.guided_locked);
  return { projectLocked, locked: projectLocked && !atLeast(current?.role, "admin") };
}

/** The persona the shell shows: the user's choice, or Guided when the lock applies. */
export function useEffectivePersona(): Persona {
  const ws = useWorkspace();
  const persona = useUi((s) => personaOf(s, ws.user.id));
  const lock = useGuidedLock();
  return lock.locked ? "guided" : persona;
}

export function useSetPersona(): (p: Persona) => void {
  const ws = useWorkspace();
  const set = useUi((s) => s.setPersona);
  return useCallback((p: Persona) => set(p, ws.user.id), [set, ws.user.id]);
}

/** Opening a Guided screen chooses Guided mode (so the header toggle tells the truth). */
export function useEnterGuided(): void {
  const persona = useEffectivePersona();
  const setPersona = useSetPersona();
  useEffect(() => {
    if (persona !== "guided") setPersona("guided");
    // Only on arrival: switching to Expert from here navigates away.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}
