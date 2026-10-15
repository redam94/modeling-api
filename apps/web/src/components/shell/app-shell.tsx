import { Link, Outlet, useNavigate, useRouterState } from "@tanstack/react-router";
import { AlertTriangle, LogIn, Monitor, Moon, PanelLeftClose, PanelLeftOpen, Search, Sun } from "lucide-react";
import type { ReactNode } from "react";

import { useReady } from "@/api/queries";
import { signIn } from "@/api/session";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
  SegmentedControl,
  Tooltip,
} from "@/components/ui/overlays";
import { Kbd } from "@/components/ui/primitives";
import { Logo, NoProjectScreen } from "@/features/auth/gate";
import { useWorkspace } from "@/features/auth/workspace";
import { useHotkeys } from "@/hooks/use-hotkeys";
import { useApplyTheme } from "@/hooks/use-theme";
import { cn } from "@/lib/utils";
import { useUi, type Persona, type ThemePreference } from "@/stores/ui";
import { useAuth } from "@/stores/auth";
import { useScope } from "@/stores/scope";

import { CommandPalette, ShortcutsDialog } from "./command-palette";
import { ADMIN_NAV, EXPERT_NAV, GUIDED_NAV, PROJECT_NAV, SETTINGS_NAV, type NavItem } from "./nav";
import { ProjectSwitcher } from "./project-switcher";
import { Toaster } from "./toaster";
import { UserMenu } from "./user-menu";

function useIsActive(item: NavItem): boolean {
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  return item.to === "/" ? pathname === "/" : pathname === item.to || pathname.startsWith(`${item.to}/`);
}

function NavLink({ item, collapsed }: { item: NavItem; collapsed: boolean }) {
  const active = useIsActive(item);
  const Icon = item.icon;
  const link = (
    <Link
      to={item.to}
      aria-current={active ? "page" : undefined}
      className={cn(
        "group relative flex h-9 items-center gap-3 rounded-md px-2.5 text-[13px] font-medium text-muted-foreground transition-colors hover:bg-background hover:text-foreground",
        active && "bg-background text-foreground shadow-xs",
        collapsed && "justify-center px-0",
      )}
    >
      {active ? <span aria-hidden className="absolute top-2 bottom-2 -left-2 w-0.5 rounded-full bg-primary" /> : null}
      <Icon className={cn("size-4 shrink-0", active && "text-primary")} />
      {collapsed ? <span className="sr-only">{item.label}</span> : <span className="truncate">{item.label}</span>}
    </Link>
  );
  return collapsed ? (
    <Tooltip side="right" content={item.label}>
      {link}
    </Tooltip>
  ) : (
    link
  );
}

/** Phone-width nav: every destination (Settings included) in a wrapping row, never scrolled. */
function CompactNavLink({ item }: { item: NavItem }) {
  const active = useIsActive(item);
  return (
    <Link
      to={item.to}
      aria-current={active ? "page" : undefined}
      className={cn(
        "rounded-md px-2 py-1 text-xs font-medium text-muted-foreground hover:text-foreground",
        active && "bg-accent text-foreground",
      )}
    >
      {item.label}
    </Link>
  );
}

function HealthDot() {
  const ready = useReady();
  const status = ready.isError ? "fail" : (ready.data?.status ?? "pending");
  const label =
    status === "ok" ? "API connected" : status === "degraded" ? "API degraded" : status === "fail" ? "API unreachable" : "Checking API";
  return (
    <Link to="/settings" className="flex items-center gap-2 rounded-md px-2.5 py-1.5 text-xs text-muted-foreground hover:text-foreground" title={label}>
      <span
        aria-hidden
        className={cn(
          "size-2 rounded-full",
          status === "ok" && "bg-success",
          status === "degraded" && "bg-warning",
          status === "fail" && "bg-destructive",
          status === "pending" && "bg-muted-foreground/40",
        )}
      />
      <span className="sr-only sm:not-sr-only">{label}</span>
    </Link>
  );
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

export function PersonaToggle() {
  const persona = useUi((s) => s.persona);
  const setPersona = useUi((s) => s.setPersona);
  const navigate = useNavigate();
  return (
    <SegmentedControl<Persona>
      label="Mode"
      value={persona}
      onValueChange={(p) => {
        setPersona(p);
        void navigate({ to: p === "guided" ? "/guided" : "/" });
      }}
      options={[
        { value: "guided", label: "Guided", title: "Task-based flows (arrives in v0.4)" },
        { value: "expert", label: "Expert", title: "Specs, runs and diagnostics" },
      ]}
    />
  );
}

/** Routes that work without a selected project (operators may belong to none). */
const PROJECTLESS = ["/admin", "/settings"];

/** After "Stay on this page" in the session-expired dialog: a quiet way back in. */
function SignedOutBanner() {
  const show = useAuth((s) => s.expired && s.dismissed);
  if (!show) return null;
  return (
    <div role="status" className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-info/35 bg-info/8 px-4 py-2 text-[13px] md:px-6">
      <LogIn className="size-4 text-info" aria-hidden />
      <span>You are signed out. The page stays as it is, but nothing can be loaded or saved.</span>
      <Button size="sm" variant="outline" className="h-7" onClick={() => signIn()}>
        Sign in again
      </Button>
    </div>
  );
}

function TenantStatusBanner() {
  const ws = useWorkspace();
  const t = ws.current?.tenant;
  if (!t || !t.status || t.status === "active") return null;
  return (
    <div role="status" className="flex items-start gap-2 border-b border-warning/35 bg-warning/10 px-4 py-2 text-[13px] md:px-6">
      <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning" />
      <p>
        <span className="font-medium">{t.name} is {t.status === "suspended" ? "suspended" : "being deleted"}.</span>{" "}
        <span className="text-muted-foreground">
          {t.status === "suspended" ? "Everything is read-only: nothing can be changed or run." : "Nothing can be changed."}
          {t.status_reason ? ` Reason: ${t.status_reason}` : ""}
        </span>
      </p>
    </div>
  );
}

export function AppShell({ children }: { children?: ReactNode }) {
  useApplyTheme();
  const ws = useWorkspace();
  const active = useScope((s) => s.active);
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const collapsed = useUi((s) => s.sidebarCollapsed);
  const toggleSidebar = useUi((s) => s.toggleSidebar);
  const persona = useUi((s) => s.persona);
  const setPaletteOpen = useUi((s) => s.setPaletteOpen);
  const setShortcutsOpen = useUi((s) => s.setShortcutsOpen);
  const navigate = useNavigate();

  const nav = persona === "guided" ? [GUIDED_NAV, ...EXPERT_NAV.slice(1)] : EXPERT_NAV;
  const projectNav = ws.mode === "session" ? PROJECT_NAV : [];
  const adminNav = ws.operator ? [ADMIN_NAV] : [];
  const chords = Object.fromEntries(
    [...EXPERT_NAV, ...projectNav, ...adminNav, SETTINGS_NAV].map((item) => [`g ${item.chord}`, () => void navigate({ to: item.to })]),
  );
  useHotkeys({
    "mod+k": () => setPaletteOpen(true),
    "/": () => setPaletteOpen(true),
    "?": () => setShortcutsOpen(true),
    "mod+b": () => toggleSidebar(),
    ...chords,
  });

  if (ws.mode === "session" && !ws.current && !ws.operator) return <NoProjectScreen email={ws.user.email} />;
  const needsProject = !PROJECTLESS.some((p) => pathname === p || pathname.startsWith(`${p}/`));
  const scopeKey = active ? `${active.tenantId}/${active.projectId}` : "-";

  return (
    <div className="flex min-h-svh">
      <a
        href="#main"
        className="sr-only z-50 rounded-md bg-primary px-3 py-2 text-primary-foreground focus:not-sr-only focus:fixed focus:top-2 focus:left-2"
      >
        Skip to content
      </a>
      <aside
        className={cn(
          "sticky top-0 hidden h-svh shrink-0 flex-col border-r bg-sidebar px-2 py-3 transition-[width] md:flex",
          collapsed ? "w-14" : "w-56",
        )}
      >
        <div className={cn("flex items-center gap-2 px-2 pb-4", collapsed && "justify-center px-0")}>
          <Logo />
          {collapsed ? null : (
            <span className="text-[15px] font-semibold tracking-tight">
              modeling<span className="text-muted-foreground">-api</span>
            </span>
          )}
        </div>
        <nav aria-label="Primary" className="grid gap-0.5">
          {nav.map((item) => (
            <NavLink key={item.to} item={item} collapsed={collapsed} />
          ))}
          {projectNav.length ? (
            <>
              <p className={cn("px-2.5 pt-4 pb-1 text-[11px] font-medium tracking-wide text-muted-foreground uppercase", collapsed && "sr-only")}>Project</p>
              {projectNav.map((item) => (
                <NavLink key={item.to} item={item} collapsed={collapsed} />
              ))}
            </>
          ) : null}
          {adminNav.length ? (
            <>
              <p className={cn("px-2.5 pt-4 pb-1 text-[11px] font-medium tracking-wide text-muted-foreground uppercase", collapsed && "sr-only")}>Platform</p>
              {adminNav.map((item) => (
                <NavLink key={item.to} item={item} collapsed={collapsed} />
              ))}
            </>
          ) : null}
        </nav>
        <div className="mt-auto grid gap-0.5">
          <NavLink item={SETTINGS_NAV} collapsed={collapsed} />
          <Button
            variant="ghost"
            size="sm"
            onClick={toggleSidebar}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            className={cn("justify-start gap-3 px-2.5 text-muted-foreground", collapsed && "justify-center px-0")}
          >
            {collapsed ? <PanelLeftOpen /> : <PanelLeftClose />}
            {collapsed ? null : "Collapse"}
          </Button>
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 border-b bg-background/85 backdrop-blur">
          <div className="flex h-12 items-center gap-3 px-4 md:px-6">
            <ProjectSwitcher />
            <Button variant="ghost" size="icon-sm" onClick={() => setPaletteOpen(true)} aria-label="Jump to…" className="lg:hidden">
              <Search />
            </Button>
            <button
              type="button"
              onClick={() => setPaletteOpen(true)}
              className="hidden h-8 w-56 items-center gap-2 rounded-md border bg-card px-2.5 text-[13px] text-muted-foreground hover:text-foreground lg:flex xl:w-72"
            >
              <Search className="size-4" />
              Jump to…
              <span className="ml-auto flex gap-0.5">
                <Kbd>⌘</Kbd>
                <Kbd>K</Kbd>
              </span>
            </button>
            <div className="ml-auto flex min-w-0 items-center gap-2">
              <HealthDot />
              <div className="hidden sm:block">
                <PersonaToggle />
              </div>
              <ThemeMenu />
              <UserMenu />
            </div>
          </div>
          <nav aria-label="Primary (compact)" className="flex flex-wrap gap-1 border-t px-3 py-1.5 md:hidden">
            {[...nav, ...projectNav, ...adminNav, SETTINGS_NAV].map((item) => (
              <CompactNavLink key={item.to} item={item} />
            ))}
          </nav>
          <SignedOutBanner />
          <TenantStatusBanner />
        </header>
        <main id="main" className="min-w-0 flex-1 px-4 py-6 md:px-8 md:py-8">
          {children ?? (needsProject && !ws.current ? <NoProjectNotice /> : <Outlet key={scopeKey} />)}
        </main>
      </div>
      <CommandPalette />
      <ShortcutsDialog />
      <Toaster />
    </div>
  );
}

/** An operator without a project on a project page. */
function NoProjectNotice() {
  return (
    <div className="grid max-w-xl gap-2 rounded-lg border border-dashed p-8">
      <h1 className="font-semibold">No project selected</h1>
      <p className="text-muted-foreground">
        You are not a member of any project, so there is no project data to show. The admin console works without one.
      </p>
      <Link to="/admin" className="text-primary hover:underline">
        Open the admin console
      </Link>
    </div>
  );
}
