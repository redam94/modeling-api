import { useNavigate, useRouter, useRouterState } from "@tanstack/react-router";
import { BookOpen, Compass, Keyboard, KeyRound, LogOut, Settings, ShieldCheck } from "lucide-react";

import { useSignOut } from "@/features/auth/gate";
import { useWorkspace } from "@/features/auth/workspace";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/overlays";
import { env } from "@/env";
import { relativeTime } from "@/lib/format";
import { useUi } from "@/stores/ui";

export function initials(name: string): string {
  const parts = name.replace(/@.*$/, "").split(/[\s._-]+/).filter(Boolean);
  const first = parts[0];
  const last = parts.at(-1);
  if (!first || !last) return "?";
  return (parts.length === 1 ? first.slice(0, 2) : `${first[0] ?? ""}${last[0] ?? ""}`).toUpperCase();
}

/**
 * The tours a user can replay (v0.18 D18, `features/onboarding/tours.ts` titles, shortened).
 * The menu only adds `?tour=<id>` to the page's URL: the shell's lazy tour host starts the tour
 * (and explains when one needs the sample project), so the entry never loads the tour engine.
 */
export const MENU_TOURS: readonly { id: string; label: string }[] = [
  { id: "welcome", label: "Take the 2-minute tour" },
  { id: "data", label: "Tour: bring in data" },
  { id: "fit", label: "Tour: fit a model" },
  { id: "results", label: "Tour: read the results" },
  { id: "whatif", label: "Tour: try a what-if" },
  { id: "dashboard", label: "Tour: make a dashboard" },
  { id: "make_your_own", label: "Tour: make your own" },
];

/** The current URL with `?tour=<id>` added (other search params kept). */
export function withTourParam(pathname: string, search: string, id: string): string {
  const params = new URLSearchParams(search);
  params.set("tour", id);
  return `${pathname}?${params.toString()}`;
}

/** The signed-in user: identity, operator badge, the Help group (docs, shortcuts, tours) and sign-out. */
export function UserMenu() {
  const ws = useWorkspace();
  const signOut = useSignOut();
  const navigate = useNavigate();
  const router = useRouter();
  const location = useRouterState({ select: (s) => s.location });
  const setShortcutsOpen = useUi((s) => s.setShortcutsOpen);
  if (ws.mode === "none") return null;
  const { user } = ws;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label={`Account: ${user.name}`}
          data-tour="shell.help-menu"
          className="focus-ring relative grid size-8 shrink-0 place-items-center rounded-full bg-accent text-2xs font-semibold text-accent-foreground transition-colors hover:bg-border-strong"
        >
          {initials(user.name)}
          {ws.operator ? (
            <span aria-hidden className="absolute -right-0.5 -bottom-0.5 grid size-3.5 place-items-center rounded-full bg-background">
              <ShieldCheck className="size-3 text-foreground" />
            </span>
          ) : null}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        <DropdownMenuLabel className="grid gap-0.5">
          <span className="truncate text-sm font-semibold text-foreground">{user.name}</span>
          {user.email ? <span className="truncate">{user.email}</span> : null}
          {ws.operator ? <span className="font-semibold text-foreground">Platform operator</span> : null}
          {ws.expiresAt ? <span>Session ends {relativeTime(ws.expiresAt)}</span> : null}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuGroup aria-label="Help">
          <DropdownMenuLabel>Help</DropdownMenuLabel>
          {env.docsUrl ? (
            <DropdownMenuItem asChild>
              <a href={env.docsUrl} target="_blank" rel="noreferrer">
                <BookOpen /> Read the docs
              </a>
            </DropdownMenuItem>
          ) : null}
          <DropdownMenuItem onSelect={() => setShortcutsOpen(true)}>
            <Keyboard /> Keyboard shortcuts
          </DropdownMenuItem>
          {MENU_TOURS.map((t) => (
            <DropdownMenuItem key={t.id} onSelect={() => router.history.push(withTourParam(location.pathname, location.searchStr, t.id))}>
              {t.id === "welcome" ? <Compass /> : <span aria-hidden className="size-4" />} {t.label}
            </DropdownMenuItem>
          ))}
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => void navigate({ to: "/tokens" })}>
          <KeyRound /> API tokens
        </DropdownMenuItem>
        {ws.operator ? (
          <DropdownMenuItem onSelect={() => void navigate({ to: "/admin" })}>
            <ShieldCheck /> Admin console
          </DropdownMenuItem>
        ) : null}
        <DropdownMenuItem onSelect={() => void navigate({ to: "/settings" })}>
          <Settings /> Settings
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => void signOut()}>
          <LogOut /> Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
