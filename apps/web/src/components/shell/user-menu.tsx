import { useNavigate } from "@tanstack/react-router";
import { KeyRound, LogOut, Settings, ShieldCheck } from "lucide-react";

import { useSignOut } from "@/features/auth/gate";
import { useWorkspace } from "@/features/auth/workspace";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/overlays";
import { relativeTime } from "@/lib/format";

export function initials(name: string): string {
  const parts = name.replace(/@.*$/, "").split(/[\s._-]+/).filter(Boolean);
  const first = parts[0];
  const last = parts.at(-1);
  if (!first || !last) return "?";
  return (parts.length === 1 ? first.slice(0, 2) : `${first[0] ?? ""}${last[0] ?? ""}`).toUpperCase();
}

/** The signed-in user: identity, operator badge, shortcuts and sign-out. */
export function UserMenu() {
  const ws = useWorkspace();
  const signOut = useSignOut();
  const navigate = useNavigate();
  if (ws.mode === "none") return null;
  const { user } = ws;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label={`Account: ${user.name}`}
          className="relative grid size-8 shrink-0 place-items-center rounded-full bg-primary/14 text-[11px] font-semibold text-primary ring-offset-2 hover:bg-primary/20 focus-visible:outline-2 focus-visible:outline-ring"
        >
          {initials(user.name)}
          {ws.operator ? (
            <span aria-hidden className="absolute -right-0.5 -bottom-0.5 grid size-3.5 place-items-center rounded-full bg-background">
              <ShieldCheck className="size-3 text-primary" />
            </span>
          ) : null}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        <DropdownMenuLabel className="grid gap-0.5">
          <span className="truncate text-[13px] font-medium text-foreground">{user.name}</span>
          {user.email ? <span className="truncate">{user.email}</span> : null}
          {ws.operator ? <span className="text-primary">Platform operator</span> : null}
          {ws.expiresAt ? <span>Session ends {relativeTime(ws.expiresAt)}</span> : null}
        </DropdownMenuLabel>
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
