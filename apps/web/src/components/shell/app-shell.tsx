import { Link, Outlet, useNavigate, useRouterState } from "@tanstack/react-router";
import { Monitor, Moon, PanelLeftClose, PanelLeftOpen, Search, Sun } from "lucide-react";
import type { ReactNode } from "react";

import { useReady } from "@/api/queries";
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
import { useHotkeys } from "@/hooks/use-hotkeys";
import { useApplyTheme } from "@/hooks/use-theme";
import { cn } from "@/lib/utils";
import { useUi, type Persona, type ThemePreference } from "@/stores/ui";

import { CommandPalette, ShortcutsDialog } from "./command-palette";
import { EXPERT_NAV, GUIDED_NAV, SETTINGS_NAV, type NavItem } from "./nav";
import { Toaster } from "./toaster";

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

export function AppShell({ children }: { children?: ReactNode }) {
  useApplyTheme();
  const collapsed = useUi((s) => s.sidebarCollapsed);
  const toggleSidebar = useUi((s) => s.toggleSidebar);
  const persona = useUi((s) => s.persona);
  const setPaletteOpen = useUi((s) => s.setPaletteOpen);
  const setShortcutsOpen = useUi((s) => s.setShortcutsOpen);
  const navigate = useNavigate();

  const nav = persona === "guided" ? [GUIDED_NAV, ...EXPERT_NAV.slice(1)] : EXPERT_NAV;
  const chords = Object.fromEntries(
    [...EXPERT_NAV, SETTINGS_NAV].map((item) => [`g ${item.chord}`, () => void navigate({ to: item.to })]),
  );
  useHotkeys({
    "mod+k": () => setPaletteOpen(true),
    "/": () => setPaletteOpen(true),
    "?": () => setShortcutsOpen(true),
    "mod+b": () => toggleSidebar(),
    ...chords,
  });

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
            <Button variant="ghost" size="icon-sm" onClick={() => setPaletteOpen(true)} aria-label="Jump to…" className="md:hidden">
              <Search />
            </Button>
            <button
              type="button"
              onClick={() => setPaletteOpen(true)}
              className="hidden h-8 w-72 items-center gap-2 rounded-md border bg-card px-2.5 text-[13px] text-muted-foreground hover:text-foreground md:flex"
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
              <PersonaToggle />
              <ThemeMenu />
            </div>
          </div>
          <nav aria-label="Primary (compact)" className="flex flex-wrap gap-1 border-t px-3 py-1.5 md:hidden">
            {[...nav, SETTINGS_NAV].map((item) => (
              <CompactNavLink key={item.to} item={item} />
            ))}
          </nav>
        </header>
        <main id="main" className="min-w-0 flex-1 px-4 py-6 md:px-8 md:py-8">
          {children ?? <Outlet />}
        </main>
      </div>
      <CommandPalette />
      <ShortcutsDialog />
      <Toaster />
    </div>
  );
}

/** Mark: three stacked bands narrowing to a point estimate (a posterior fan, abstracted). */
function Logo() {
  return (
    <svg viewBox="0 0 24 24" className="size-6 shrink-0" aria-hidden>
      <rect x="2" y="4" width="20" height="4" rx="2" className="fill-primary/25" />
      <rect x="5" y="10" width="14" height="4" rx="2" className="fill-primary/55" />
      <rect x="9" y="16" width="6" height="4" rx="2" className="fill-primary" />
    </svg>
  );
}
