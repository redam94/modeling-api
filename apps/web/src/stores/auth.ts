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
 * again without throwing away what is on screen. `dismissed` means the user chose to stay on
 * the page: the dialog stays closed and a banner offers sign-in instead.
 */
import { create } from "zustand";

export type AuthMode = "unknown" | "session" | "none";

export interface AuthState {
  mode: AuthMode;
  expired: boolean;
  dismissed: boolean;
  setMode: (mode: AuthMode) => void;
  markExpired: () => void;
  dismiss: () => void;
  clearExpired: () => void;
}

export const useAuth = create<AuthState>()((set, get) => ({
  mode: "unknown",
  expired: false,
  dismissed: false,
  setMode: (mode) => {
    if (get().mode !== mode) set({ mode });
  },
  markExpired: () => {
    if (get().mode === "session" && !get().expired) set({ expired: true });
  },
  dismiss: () => set({ dismissed: true }),
  clearExpired: () => {
    if (get().expired || get().dismissed) set({ expired: false, dismissed: false });
  },
}));
