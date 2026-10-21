/**
 * The client portal's frame: the app mark and the client's name, a project switcher when the
 * user is in more than one project, the theme menu and the account menu (sign out). There is no
 * navigation, command palette or keyboard shortcut: a client user sees only their dashboards.
 */
import { useQueryClient } from "@tanstack/react-query";
import { Check, ChevronsUpDown, LogOut, Monitor, Moon, Sun } from "lucide-react";
import type * as React from "react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/overlays";
import { Logo, useSignOut } from "@/features/auth/gate";
import { useWorkspace, type ProjectChoice } from "@/features/auth/workspace";
import { useScope } from "@/stores/scope";
import { useUi, type ThemePreference } from "@/stores/ui";

function initials(name: string): string {
  const parts = name.replace(/@.*$/, "").split(/[\s._-]+/).filter(Boolean);
  const first = parts[0];
  const last = parts.at(-1);
  if (!first || !last) return "?";
  return (parts.length === 1 ? first.slice(0, 2) : `${first[0] ?? ""}${last[0] ?? ""}`).toUpperCase();
}

function ThemeMenu() {
  const theme = useUi((s) => s.theme);
  const setTheme = useUi((s) => s.setTheme);
  const Icon = theme === "dark" ? Moon : theme === "light" ? Sun : Monitor;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon-sm" aria-label="Theme">
          <Icon />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel>Theme</DropdownMenuLabel>
        <DropdownMenuRadioGroup value={theme} onValueChange={(v) => setTheme(v as ThemePreference)}>
          <DropdownMenuRadioItem value="system">System</DropdownMenuRadioItem>
          <DropdownMenuRadioItem value="light">Light</DropdownMenuRadioItem>
          <DropdownMenuRadioItem value="dark">Dark</DropdownMenuRadioItem>
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** Switch project; a project where the user is not a client opens the full app at Home. */
function useSwitch(onClientProject: () => void): (c: ProjectChoice) => void {
  const qc = useQueryClient();
  return (c) => {
    const cur = useScope.getState().active;
    if (cur && cur.tenantId === c.project.tenant_id && cur.projectId === c.project.project_id) return;
    void qc.cancelQueries({ queryKey: ["p"] });
    if (c.role !== "client") window.history.replaceState(window.history.state, "", "/");
    useScope.getState().select({ tenantId: c.project.tenant_id, projectId: c.project.project_id });
    if (c.role === "client") onClientProject();
  };
}

function ProjectMenu({ onClientProject }: { onClientProject: () => void }) {
  const ws = useWorkspace();
  const choose = useSwitch(onClientProject);
  if (ws.choices.length < 2 || !ws.current) return null;
  const cur = ws.current;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="sm" className="min-w-0 gap-1.5 px-2.5" aria-label={`Project: ${cur.tenant.name} / ${cur.project.name}`}>
          <span className="truncate">{cur.project.name}</span>
          <ChevronsUpDown className="text-hint" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-64">
        <DropdownMenuLabel>Projects</DropdownMenuLabel>
        {ws.choices.map((c) => (
          <DropdownMenuItem key={c.key} onSelect={() => choose(c)}>
            <span className="grid min-w-0 flex-1">
              <span className="truncate text-foreground">{c.project.name}</span>
              <span className="truncate text-xs text-hint">{c.tenant.name}</span>
            </span>
            {c.key === cur.key ? <Check aria-label="Current project" /> : null}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function AccountMenu() {
  const ws = useWorkspace();
  const signOut = useSignOut();
  if (ws.mode === "none") return null;
  const { user } = ws;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label={`Account: ${user.name}`}
          className="focus-ring grid size-8 shrink-0 place-items-center rounded-full bg-accent text-2xs font-semibold text-accent-foreground hover:bg-brand-200 dark:hover:bg-secondary"
        >
          {initials(user.name)}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        <DropdownMenuLabel className="grid gap-0.5">
          <span className="truncate text-sm font-semibold text-foreground">{user.name}</span>
          {user.email ? <span className="truncate">{user.email}</span> : null}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => void signOut()}>
          <LogOut /> Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export function ClientShell({ clientName, onHome, children }: { clientName: string | null; onHome: () => void; children: React.ReactNode }) {
  return (
    <div className="flex min-h-svh flex-col bg-background">
      <a href="#main" className="focus-ring sr-only rounded-md bg-card px-3 py-2 focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-50">
        Skip to the content
      </a>
      <header className="sticky top-0 z-20 border-b bg-card/90 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-[1440px] items-center gap-3 px-4 md:px-8">
          <a
            href="/c"
            onClick={(e) => {
              e.preventDefault();
              onHome();
            }}
            className="focus-ring flex min-w-0 items-center gap-2.5 rounded-md"
          >
            <Logo className="size-7" />
            <span className="truncate text-base font-semibold text-foreground">{clientName ?? "Your dashboards"}</span>
          </a>
          <ProjectMenu onClientProject={onHome} />
          <div className="ml-auto flex items-center gap-2">
            <ThemeMenu />
            <AccountMenu />
          </div>
        </div>
      </header>
      <main id="main" className="mx-auto w-full max-w-[1440px] min-w-0 flex-1 px-4 py-6 md:px-8 md:py-8">
        {children}
      </main>
    </div>
  );
}
