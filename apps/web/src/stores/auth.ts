/**
 * How the browser is authenticated (runtime only, never persisted).
 *
 * - `unknown`: `/auth/session` has not answered yet.
 * - `session`: a v0.3 server with the backend-for-frontend session (cookie). Requests carry
 *   the CSRF header on mutations and `X-Mapi-Project` on project-scoped routes.
 * - `none`: a server without `/auth` (the v0.2 single-tenant profile): no session, no
 *   tenancy headers (that server's CORS policy would reject them).
 *
 * `expired` is raised by the API client on a 401 so the shell can ask the user to sign in
 * again without throwing away what is on screen.
 */
import { create } from "zustand";

export type AuthMode = "unknown" | "session" | "none";

export interface AuthState {
  mode: AuthMode;
  expired: boolean;
  setMode: (mode: AuthMode) => void;
  markExpired: () => void;
  clearExpired: () => void;
}

export const useAuth = create<AuthState>()((set, get) => ({
  mode: "unknown",
  expired: false,
  setMode: (mode) => {
    if (get().mode !== mode) set({ mode });
  },
  markExpired: () => {
    if (get().mode === "session" && !get().expired) set({ expired: true });
  },
  clearExpired: () => set({ expired: false }),
}));
