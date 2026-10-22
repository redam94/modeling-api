import { Link, Outlet, useNavigate, useRouterState } from "@tanstack/react-router";
import { ChevronDown, LogIn, Monitor, Moon, MoreHorizontal, PanelLeftClose, PanelLeftOpen, Search, Sun } from "lucide-react";
import { lazy, Suspense, useEffect, useId, type ReactNode } from "react";

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
import { useEffectivePersona, useGuidedLock, useSetPersona } from "@/features/guided/persona";
import { TenantStatusBanner } from "@/features/trust/tenant-banner";
import { useHotkeys } from "@/hooks/use-hotkeys";
import { useApplyTheme } from "@/hooks/use-theme";
import { cn } from "@/lib/utils";
import { useUi, type Persona, type ThemePreference } from "@/stores/ui";
import { useAuth } from "@/stores/auth";
import { useScope } from "@/stores/scope";

import { CommandPalette, ShortcutsDialog } from "./command-palette";
import {
  ADMIN_NAV,
  EXPERT_NAV,
  EXPERT_NAV_GROUPS,
  GUIDED_MORE_NAV,
  GUIDED_PRIMARY_NAV,
  inGuidedMore,
  LOCKED_NAV,
  PROJECT_NAV,
  SETTINGS_NAV,
  type NavGroup,
  type NavItem,
} from "./nav";
import { ProjectSwitcher } from "./project-switcher";
import { Toaster } from "./toaster";
import { UserMenu } from "./user-menu";

/**
 * v0.18 (D18): the tour engine, a lazy signed-in chunk (`tour-host-*`). It plays tours, offers
 * the welcome dialog on a first visit and ticks the checklist from onboarding events.
 */
const TourHost = lazy(() => import("@/features/onboarding/tour-host"));

/** The `data-tour` anchors of the nav items the tours point at (v0.18 I-8). */
const NAV_ANCHOR: Readonly<Record<string, string>> = {
  "/guided": "nav.guided",
  "/datasets": "nav.datasets",
  "/dashboards": "nav.dashboards",
  "/catalog": "nav.catalog",
  "/library": "nav.make-your-own",
};

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
      data-tour={NAV_ANCHOR[item.to]}
      className={cn(
        "focus-ring group relative flex min-h-10 items-center gap-3 rounded-lg px-3 text-sm font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground",
        // Studio: the active item is an ink block with its icon in lime.
        active && "bg-block text-block-foreground hover:bg-block hover:text-block-foreground",
        collapsed && "justify-center px-0",
      )}
    >
      <Icon className={cn("size-[18px] shrink-0", active && "stroke-highlight")} />
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
      data-tour={NAV_ANCHOR[item.to]}
      className={cn(
        "focus-ring rounded-full px-2.5 py-1 text-xs font-medium text-muted-foreground hover:bg-muted hover:text-foreground",
        active && "bg-block text-block-foreground hover:bg-block hover:text-block-foreground",
      )}
    >
      {item.label}
    </Link>
  );
}

/** An eyebrow over a group of nav items (screen-reader only while the sidebar is collapsed). */
function GroupLabel({ children, collapsed, first = false, hidden = false }: { children: ReactNode; collapsed: boolean; first?: boolean; hidden?: boolean }) {
  return (
    <p aria-hidden={hidden || undefined} className={cn("eyebrow px-3 pb-1", first ? "pt-0" : "pt-4", collapsed && "sr-only")}>
      {children}
    </p>
  );
}

/**
 * Guided mode's "More" (v0.18 D23): a disclosure over the Expert areas an analyst needs less
 * often. It remembers its state (`useUi.navMoreOpen`) and opens by itself on one of its pages.
 */
function useNavMore() {
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const open = useUi((s) => s.navMoreOpen);
  const setOpen = useUi((s) => s.setNavMoreOpen);
  const onMorePage = inGuidedMore(pathname);
  useEffect(() => {
    if (onMorePage) setOpen(true);
  }, [onMorePage, setOpen]);
  return { open: open || onMorePage, toggle: () => setOpen(!(open || onMorePage)) };
}

function MoreButton({ open, onToggle, controls, collapsed, compact = false }: { open: boolean; onToggle: () => void; controls: string; collapsed?: boolean; compact?: boolean }) {
  const button = (
    <button
      type="button"
      data-testid="nav-more"
      data-tour="nav.more"
      aria-expanded={open}
      aria-controls={controls}
      onClick={onToggle}
      className={cn(
        "focus-ring flex items-center gap-3 font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground",
        compact ? "rounded-full px-2.5 py-1 text-xs" : "min-h-10 w-full rounded-lg px-3 text-sm",
        collapsed && "justify-center px-0",
      )}
    >
      {compact ? null : <MoreHorizontal className="size-[18px] shrink-0" />}
      {collapsed ? <span className="sr-only">More</span> : <span className="truncate">More</span>}
      {collapsed ? null : <ChevronDown aria-hidden className={cn("ml-auto size-4 shrink-0 transition-transform", compact && "ml-0 size-3.5", open && "rotate-180")} />}
    </button>
  );
  return collapsed ? (
    <Tooltip side="right" content={open ? "Fewer" : "More"}>
      {button}
    </Tooltip>
  ) : (
    button
  );
}

/** The sidebar's main items for the current mode. */
function SidebarNav({ mode, collapsed }: { mode: "locked" | "guided" | "expert"; collapsed: boolean }) {
  const more = useNavMore();
  const moreId = useId();
  if (mode === "locked") return LOCKED_NAV.map((item) => <NavLink key={item.to} item={item} collapsed={collapsed} />);
  if (mode === "guided") {
    return (
      <>
        {GUIDED_PRIMARY_NAV.map((item) => (
          <NavLink key={item.to} item={item} collapsed={collapsed} />
        ))}
        <MoreButton open={more.open} onToggle={more.toggle} controls={moreId} collapsed={collapsed} />
        <div id={moreId} hidden={!more.open} className="grid gap-0.5">
          {more.open ? GUIDED_MORE_NAV.map((item) => <NavLink key={item.to} item={item} collapsed={collapsed} />) : null}
        </div>
      </>
    );
  }
  return EXPERT_NAV_GROUPS.map((group: NavGroup, i) => (
    <div key={group.label} role="group" aria-label={group.label} className="grid gap-0.5">
      {/* the group's aria-label names it for screen readers; the eyebrow is for the eye */}
      <GroupLabel collapsed={collapsed} first={i === 0} hidden>
        {group.label}
      </GroupLabel>
      {group.items.map((item) => (
        <NavLink key={item.to} item={item} collapsed={collapsed} />
      ))}
    </div>
  ));
}

/** Phone-width nav: every destination in a wrapping row, never scrolled (Guided: the seven, then More). */
function CompactNav({ mode, extra }: { mode: "locked" | "guided" | "expert"; extra: NavItem[] }) {
  const more = useNavMore();
  const moreId = useId();
  const main = mode === "locked" ? LOCKED_NAV : mode === "guided" ? GUIDED_PRIMARY_NAV : EXPERT_NAV;
  return (
    <nav aria-label="Primary (compact)" data-tour="nav.primary" className="flex flex-wrap gap-1 border-t px-3 py-1.5 md:hidden">
      {main.map((item) => (
        <CompactNavLink key={item.to} item={item} />
      ))}
      {mode === "guided" ? (
        <>
          <MoreButton open={more.open} onToggle={more.toggle} controls={moreId} compact />
          <span id={moreId} hidden={!more.open} className="contents">
            {more.open ? GUIDED_MORE_NAV.map((item) => <CompactNavLink key={item.to} item={item} />) : null}
          </span>
        </>
      ) : null}
      {extra.map((item) => (
        <CompactNavLink key={item.to} item={item} />
      ))}
    </nav>
  );
}

function HealthDot() {
  const ready = useReady();
  const status = ready.isError ? "fail" : (ready.data?.status ?? "pending");
  const label =
    status === "ok" ? "API connected" : status === "degraded" ? "API degraded" : status === "fail" ? "API unreachable" : "API status not known yet";
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
      <span className="sr-only whitespace-nowrap sm:not-sr-only">{label}</span>
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
  const persona = useEffectivePersona();
  const setPersona = useSetPersona();
  const lock = useGuidedLock();
  const navigate = useNavigate();
  // Members below admin of a Guided-locked project see Guided mode only.
  if (lock.locked) return null;
  return (
    <SegmentedControl<Persona>
      label="Mode"
      value={persona}
      onValueChange={(p) => {
        setPersona(p);
        void navigate({ to: p === "guided" ? "/guided" : "/" });
      }}
      options={[
        { value: "guided", label: "Guided", title: "Do tasks in business terms" },
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
    <div role="status" className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-info/35 bg-info/8 px-4 py-2 text-sm md:px-6">
      <LogIn className="size-4 text-info" aria-hidden />
      <span>You are signed out. The page does not change, but the app cannot load or save data.</span>
      <Button size="sm" variant="outline" className="h-7" onClick={() => signIn()}>
        Sign in again
      </Button>
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
  const persona = useEffectivePersona();
  const lock = useGuidedLock();
  const setPaletteOpen = useUi((s) => s.setPaletteOpen);
  const setShortcutsOpen = useUi((s) => s.setShortcutsOpen);
  const navigate = useNavigate();

  // A Guided-locked project shows members below admin the Guided screens only, and the
  // Integrations hub that Guided mode's "Share" step leads to.
  const navMode = lock.locked ? "locked" : persona === "guided" ? "guided" : "expert";
  // Guided mode has no Expert Home: "/" opens the Guided start page (a new self-service
  // workspace lands there, as does a Guided-locked project).
  useEffect(() => {
    if (persona === "guided" && pathname === "/") void navigate({ to: "/guided", replace: true });
  }, [persona, pathname, navigate]);
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
          "sticky top-0 hidden h-svh shrink-0 flex-col overflow-y-auto border-r bg-sidebar px-3 py-4 transition-[width] md:flex",
          collapsed ? "w-16" : "w-60",
        )}
      >
        <div className={cn("flex items-center gap-2.5 px-2 pb-5", collapsed && "justify-center px-0")}>
          <Logo />
          {collapsed ? null : (
            <span className="font-display text-lg leading-none font-bold tracking-[-0.02em]">
              modeling<span className="text-muted-foreground">-api</span>
            </span>
          )}
        </div>
        <nav aria-label="Primary" data-tour="nav.primary" className="grid gap-0.5">
          <SidebarNav mode={navMode} collapsed={collapsed} />
          {projectNav.length ? (
            <>
              <GroupLabel collapsed={collapsed}>Project</GroupLabel>
              {projectNav.map((item) => (
                <NavLink key={item.to} item={item} collapsed={collapsed} />
              ))}
            </>
          ) : null}
          {adminNav.length ? (
            <>
              <GroupLabel collapsed={collapsed}>Platform</GroupLabel>
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
            className={cn("justify-start gap-3 rounded-lg px-3 text-muted-foreground", collapsed && "justify-center px-0")}
          >
            {collapsed ? <PanelLeftOpen /> : <PanelLeftClose />}
            {collapsed ? null : "Collapse"}
          </Button>
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 border-b bg-card/90 backdrop-blur">
          <div className="flex h-12 items-center gap-3 px-4 md:px-6">
            <ProjectSwitcher />
            <Button variant="ghost" size="icon-sm" onClick={() => setPaletteOpen(true)} aria-label="Jump to…" className="lg:hidden">
              <Search />
            </Button>
            <button
              type="button"
              onClick={() => setPaletteOpen(true)}
              className="focus-ring hidden h-8 w-56 items-center gap-2 rounded-lg border border-border-strong bg-card px-2.5 text-sm text-hint hover:border-hint hover:text-foreground lg:flex xl:w-72"
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
          <CompactNav mode={navMode} extra={[...projectNav, ...adminNav, SETTINGS_NAV]} />
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
      {ws.mode === "session" ? (
        <Suspense fallback={null}>
          <TourHost />
        </Suspense>
      ) : null}
    </div>
  );
}

/** An operator without a project on a project page. */
function NoProjectNotice() {
  return (
    <div className="grid max-w-xl gap-2 rounded-xl border border-dashed border-border-strong bg-card p-8">
      <h1 className="font-semibold">No project selected</h1>
      <p className="text-muted-foreground">
        You are not a member of a project. Thus, there is no project data to show. You can use the admin console without a project.
      </p>
      <Link to="/admin" className="text-primary hover:underline">
        Open the admin console
      </Link>
    </div>
  );
}
