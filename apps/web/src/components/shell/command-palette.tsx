/**
 * The global search (v0.21): one overlay (Cmd/Ctrl+K or "/") that finds pages, actions and the
 * user's own things by name, in groups, with recent items, jumps by id and documentation pages.
 *
 * - Pages and actions come from `nav.ts`, by the rules of the sidebar (nav mode, session,
 *   operator) plus `can()`: the search never offers a page the sidebar hides.
 * - The lists of the API load in a lazy chunk (`features/search/remote-sources`), one source for
 *   each group, so the local groups show at once and a slow or failed list holds nothing.
 * - Clients and share links never get this: the portal and the share frame do not mount it.
 */
import { useNavigate } from "@tanstack/react-router";
import {
  Activity,
  ArrowRight,
  BadgeCheck,
  BookOpen,
  Boxes,
  Database,
  Eraser,
  FileCode2,
  FlaskConical,
  Hash,
  LayoutDashboard,
  Library,
  Moon,
  Plug,
  Search,
  Sun,
  WandSparkles,
  Workflow,
  X,
  type LucideIcon,
} from "lucide-react";
import { Dialog as DialogPrimitive } from "radix-ui";
import { lazy, Suspense, useCallback, useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from "react";

import { can } from "@/api/access";
import { useProjectScope } from "@/api/queries";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/overlays";
import { Kbd, Skeleton } from "@/components/ui/primitives";
import { env } from "@/env";
import { useWorkspace } from "@/features/auth/workspace";
import { useEffectivePersona, useGuidedLock } from "@/features/guided/persona";
import { idJump, kindAvailable, remoteGroups, visiblePages } from "@/features/search/access";
import { docsHref, docsSearchHref, loadDocsIndex, searchDocs, type DocsPage } from "@/features/search/docs";
import { KINDS, SERVER_FILTERED, type KindId, type RemoteGroupId, type SourceState } from "@/features/search/kinds";
import { Marked } from "@/features/search/marked";
import { normaliseQuery, rankRows, type Range } from "@/features/search/rank";
import { useRecents, type RecentItem } from "@/features/search/recents";
import { isApplePlatform } from "@/hooks/platform";
import { useDebounced } from "@/hooks/use-debounced";
import { cn } from "@/lib/utils";
import { useUi } from "@/stores/ui";

import { ADMIN_NAV, EXPERT_NAV, navForMode, PROJECT_NAV, SETTINGS_NAV, type NavMode } from "./nav";
import { useSwitchProject } from "./project-switcher";

export { fuzzyScore } from "@/features/search/rank";

/** The lists of the API: a lazy chunk, loaded on the first query. */
const RemoteSources = lazy(() => import("@/features/search/remote-sources"));

/** Rows of one group, before its "See all" row. */
const GROUP_ROWS = 5;
/** The pause before the search asks the server. */
const DEBOUNCE_MS = 250;
/** The shortest text the search asks the API about. */
const MIN_REMOTE_CHARS = 2;

const KIND_ICON: Readonly<Record<KindId, LucideIcon>> = {
  datasets: Database,
  models: Boxes,
  specs: FileCode2,
  runs: Activity,
  experiments: FlaskConical,
  dashboards: LayoutDashboard,
  workflows: Workflow,
  endpoints: Plug,
  catalog: Library,
  library: WandSparkles,
  validations: BadgeCheck,
  policies: BadgeCheck,
  webhooks: Plug,
  plugins: FileCode2,
};

type RowAction =
  /** An in-app page: Enter opens it, Cmd/Ctrl+Enter opens it in a new tab. */
  | { type: "link"; href: string; recent?: RecentItem }
  /** A page of the docs site: a new tab. */
  | { type: "external"; url: string }
  | { type: "run"; run: () => void; keepOpen?: boolean }
  /** An example: it goes into the box. */
  | { type: "fill"; text: string };

interface Row {
  key: string;
  title: string;
  ranges: readonly Range[];
  /** One muted line: a description, an id, a status, a section. */
  secondary?: string | undefined;
  /** The hint at the right: what the row is. */
  kind: string;
  icon: LucideIcon;
  action: RowAction;
}

interface Group {
  id: string;
  /** `null`: the rows of the no-results state. */
  heading: string | null;
  rows: Row[];
  /** A remote group that waits for its answer: one skeleton line when it has no row yet. */
  loading?: boolean;
}

const NO_RANGES: readonly Range[] = [];

function resultsText(count: number): string {
  return count === 0 ? "No results" : count === 1 ? "1 result" : `${count} results`;
}

export function CommandPalette() {
  const open = useUi((s) => s.paletteOpen);
  const setOpen = useUi((s) => s.setPaletteOpen);
  const setTheme = useUi((s) => s.setTheme);
  const navigate = useNavigate();
  const ws = useWorkspace();
  const switchProject = useSwitchProject();
  const persona = useEffectivePersona();
  const lock = useGuidedLock();
  const scope = useProjectScope().key;
  const recents = useRecents((s) => s.byScope[scope]);
  const addRecent = useRecents((s) => s.add);
  const clearRecents = useRecents((s) => s.clear);
  const [query, setQuery] = useState("");
  /** The row the keyboard is on (an index in the flat list of rows). */
  const [cursor, setActive] = useState(0);
  const opener = useRef<HTMLElement | null>(null);
  const scroller = useRef<HTMLDivElement | null>(null);
  const [remote, setRemote] = useState<Partial<Record<RemoteGroupId, SourceState>>>({});
  const [docs, setDocs] = useState<DocsPage[] | null>(null);
  const listId = useId();
  const docsUrl = env.docsUrl;

  // The same list as the sidebar (app-shell.tsx): the nav of the mode, then Project and Platform.
  const { grants, choices } = ws;
  const currentKey = ws.current?.key;
  const navMode: NavMode = lock.locked ? "locked" : persona === "guided" ? "guided" : "expert";
  const session = ws.mode === "session";
  const pages = useMemo(
    () => visiblePages([...navForMode(navMode), ...(session ? PROJECT_NAV : []), ...(ws.operator ? [ADMIN_NAV] : []), SETTINGS_NAV], grants),
    [navMode, session, ws.operator, grants],
  );
  const paths = useMemo(() => new Set(pages.map((p) => p.to)), [pages]);
  const groupDefs = useMemo(() => remoteGroups(paths, grants), [paths, grants]);
  const groupIds = useMemo(() => groupDefs.map((g) => g.id as RemoteGroupId), [groupDefs]);

  /** The text to match: lower case, single spaces. */
  const typed = normaliseQuery(query);
  /** The text as the user wrote it, for the `?q=` of a list page and of the docs. */
  const text = query.trim().split(/\s+/).join(" ");
  const debounced = useDebounced(typed, DEBOUNCE_MS);
  const searching = typed.length >= MIN_REMOTE_CHARS;
  // What the server is asked for: the text after the pause. A shorter text asks for nothing.
  const remoteQuery = searching && debounced.length >= MIN_REMOTE_CHARS ? debounced : "";

  const report = useCallback((id: RemoteGroupId, state: SourceState | null) => {
    setRemote((prev) => {
      if (state === null) {
        if (!(id in prev)) return prev;
        const { [id]: _gone, ...rest } = prev;
        return rest;
      }
      return { ...prev, [id]: state };
    });
  }, []);

  useEffect(() => {
    if (!open || !docsUrl) return;
    let live = true;
    loadDocsIndex().then(
      (index) => live && setDocs(index),
      () => undefined,
    );
    return () => {
      live = false;
    };
  }, [open, docsUrl]);

  const close = useCallback(() => {
    setOpen(false);
    setQuery("");
    setActive(0);
  }, [setOpen]);

  const groups = useMemo<Group[]>(() => {
    const link = (href: string, recent?: RecentItem): RowAction => (recent ? { type: "link", href, recent } : { type: "link", href });
    const pageRows: Row[] = pages.map((n) => ({ key: `page:${n.to}`, title: n.label, ranges: NO_RANGES, secondary: n.description, kind: "Page", icon: n.icon, action: link(n.to) }));
    const actionRows: Row[] = [];
    if (paths.has("/build") && can(grants, "create", "spec")) {
      actionRows.push({ key: "action:new-spec", title: "New spec", ranges: NO_RANGES, secondary: "Open the editor with a starter spec", kind: "Action", icon: ArrowRight, action: link("/build/new") });
    }
    actionRows.push(
      { key: "action:theme-dark", title: "Use dark theme", ranges: NO_RANGES, secondary: "Theme", kind: "Action", icon: Moon, action: { type: "run", run: () => setTheme("dark") } },
      { key: "action:theme-light", title: "Use light theme", ranges: NO_RANGES, secondary: "Theme", kind: "Action", icon: Sun, action: { type: "run", run: () => setTheme("light") } },
    );
    for (const c of session ? choices : []) {
      if (c.key === currentKey) continue;
      actionRows.push({
        key: `action:project:${c.key}`,
        title: `Switch to ${c.tenant.name} / ${c.project.name}`,
        ranges: NO_RANGES,
        secondary: `Project · ${c.role}`,
        kind: "Action",
        icon: ArrowRight,
        action: { type: "run", run: () => switchProject(c) },
      });
    }

    if (!typed) {
      const shown = (recents ?? []).filter((r) => kindAvailable(r.group, paths, grants));
      const first: Group = shown.length
        ? {
            id: "recent",
            heading: "Recent",
            rows: [
              ...shown.map((r): Row => ({ key: `recent:${r.href}`, title: r.title, ranges: NO_RANGES, secondary: r.secondary, kind: KINDS[r.group].kind, icon: KIND_ICON[r.group], action: link(r.href, r) })),
              { key: "recent:clear", title: "Clear recent items", ranges: NO_RANGES, kind: "Action", icon: Eraser, action: { type: "run", run: () => clearRecents(scope), keepOpen: true } },
            ],
          }
        : {
            id: "try",
            heading: "Try",
            rows: [
              // a page, a part of a name, an action: each example finds something in an empty project too
              { text: pages.find((p) => p.to === "/datasets")?.label ?? pages[0]?.label ?? "Settings", kind: "A page" },
              { text: "fit", kind: "A part of a name" },
              { text: "dark theme", kind: "An action" },
            ].map((t): Row => ({ key: `try:${t.text}`, title: t.text, ranges: NO_RANGES, kind: t.kind, icon: Search, action: { type: "fill", text: t.text } })),
          };
      return [first, { id: "pages", heading: "Go to", rows: pageRows }, { id: "actions", heading: "Actions", rows: actionRows }];
    }

    const ranked = (rows: Row[]) => rankRows(typed, rows, { limit: GROUP_ROWS }).map((r): Row => ({ ...r.row, ranges: r.ranges }));
    const out: Group[] = [];
    const jump = idJump(typed, paths, grants);
    const jumpRow: Row[] = jump
      ? [{ key: `id:${jump.id}`, title: jump.label, ranges: NO_RANGES, secondary: "By id", kind: KINDS[jump.kind].kind, icon: Hash, action: link(jump.href, { group: jump.kind, title: jump.id, href: jump.href }) }]
      : [];
    out.push({ id: "pages", heading: "Go to", rows: [...jumpRow, ...ranked(pageRows)].slice(0, GROUP_ROWS) });
    out.push({ id: "actions", heading: "Actions", rows: ranked(actionRows) });

    for (const def of searching ? groupDefs : []) {
      const id = def.id as RemoteGroupId;
      const state = remote[id];
      if (!state) {
        out.push({ id, heading: def.heading, rows: [], loading: true });
        continue;
      }
      // The answer of the server is for the text after the pause: until then the group waits.
      const waiting = state.status === "loading" || (SERVER_FILTERED.has(id) && remoteQuery !== typed);
      // The row of a pasted id is in "Go to" already.
      const found = rankRows(typed, state.items, { limit: GROUP_ROWS }).filter((r) => r.row.href !== jump?.href);
      const rows = found.map(({ row, ranges }): Row => ({
        key: `${id}:${row.id}`,
        title: row.title,
        ranges,
        secondary: row.secondary,
        kind: def.kind,
        icon: KIND_ICON[id],
        action: link(row.href, { group: id, title: row.title, secondary: row.secondary, href: row.href }),
      }));
      if (rows.length) {
        const list = def.list ?? def.nav;
        const page = pages.find((p) => p.to === def.nav);
        rows.push({ key: `${id}:all`, title: `See all in ${page?.label ?? def.heading}`, ranges: NO_RANGES, kind: "Page", icon: ArrowRight, action: link(`${list}?q=${encodeURIComponent(text)}`) });
      }
      out.push({ id, heading: def.heading, rows, loading: waiting });
    }

    if (docsUrl && docs) {
      out.push({
        id: "docs",
        heading: "Docs",
        rows: searchDocs(docs, typed, GROUP_ROWS).map((d): Row => ({ key: `docs:${d.path}`, title: d.title, ranges: d.ranges, secondary: d.secondary, kind: "Docs", icon: BookOpen, action: { type: "external", url: docsHref(docsUrl, d.path) } })),
      });
    }

    const live = out.filter((g) => g.rows.length || g.loading);
    if (live.length) return live;
    // Nothing matched: the two ways on.
    const ways: Row[] = [];
    if (docsUrl) ways.push({ key: "empty:docs", title: "Search the docs", ranges: NO_RANGES, secondary: "Opens the documentation in a new tab", kind: "Docs", icon: BookOpen, action: { type: "external", url: docsSearchHref(docsUrl, text) } });
    if (paths.has("/choose")) ways.push({ key: "empty:choose", title: "Open Help me choose", ranges: NO_RANGES, secondary: "Answer some questions to find the applicable analyses", kind: "Page", icon: ArrowRight, action: link("/choose") });
    return [{ id: "empty", heading: null, rows: ways }];
  }, [pages, paths, grants, choices, currentKey, session, typed, text, searching, remoteQuery, recents, groupDefs, remote, docs, docsUrl, scope, setTheme, switchProject, clearRecents]);

  const rows = useMemo(() => groups.flatMap((g) => g.rows), [groups]);
  const loading = groups.some((g) => g.loading);
  const noResults = Boolean(typed) && !loading && groups.every((g) => g.heading === null);
  const active = Math.min(cursor, Math.max(0, rows.length - 1));
  const optionId = (i: number) => `${listId}-option-${i}`;

  useEffect(() => {
    if (!open || !rows.length) return;
    // The first row goes with the heading above it; jsdom has no layout (no scrollIntoView).
    if (active === 0 && scroller.current) scroller.current.scrollTop = 0;
    else document.getElementById(`${listId}-option-${active}`)?.scrollIntoView?.({ block: "nearest" });
  }, [open, active, rows.length, listId]);

  const activate = (row: Row | undefined, newTab: boolean) => {
    if (!row) return;
    const a = row.action;
    if (a.type === "fill") {
      setQuery(a.text);
      setActive(0);
      return;
    }
    if (a.type === "run") {
      if (a.keepOpen) setActive(0);
      else close();
      a.run();
      return;
    }
    if (a.type === "external") {
      close();
      window.open(a.url, "_blank", "noopener,noreferrer");
      return;
    }
    if (a.recent) addRecent(scope, a.recent);
    close();
    if (newTab) window.open(a.href, "_blank", "noopener");
    else void navigate({ href: a.href });
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.nativeEvent.isComposing) return;
    const last = rows.length - 1;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive(Math.min(last, active + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive(Math.max(0, active - 1));
    } else if (e.key === "Home") {
      e.preventDefault();
      setActive(0);
    } else if (e.key === "End") {
      e.preventDefault();
      setActive(Math.max(0, last));
    } else if (e.key === "Enter") {
      e.preventDefault();
      activate(rows[active], e.metaKey || e.ctrlKey);
    }
  };

  // The index of each row in the flat list (the keyboard moves across the groups).
  let next = 0;
  const numbered = groups.map((g) => ({ ...g, rows: g.rows.map((row) => ({ row, i: next++ })) }));

  return (
    <DialogPrimitive.Root open={open} onOpenChange={(o) => (o ? setOpen(true) : close())}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-overlay/50 backdrop-blur-md" />
        <DialogPrimitive.Content
          aria-describedby={undefined}
          // Without a trigger element Radix gives the focus to nothing on close: keep the opener.
          onOpenAutoFocus={() => {
            opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
          }}
          onCloseAutoFocus={(e) => {
            e.preventDefault();
            opener.current?.focus();
          }}
          className="fixed top-[12vh] left-1/2 z-50 flex max-h-[76vh] w-[calc(100%-2rem)] max-w-[680px] -translate-x-1/2 flex-col overflow-hidden rounded-xl border bg-popover text-popover-foreground shadow-lg outline-none"
        >
          <DialogPrimitive.Title className="sr-only">Search</DialogPrimitive.Title>
          <div className="flex h-14 shrink-0 items-center gap-3 border-b px-4">
            <Search className="size-5 shrink-0 text-muted-foreground" aria-hidden />
            <input
              role="combobox"
              aria-label="Search"
              aria-expanded={rows.length > 0}
              aria-controls={listId}
              aria-autocomplete="list"
              aria-activedescendant={rows.length ? optionId(active) : undefined}
              autoComplete="off"
              spellCheck={false}
              placeholder="Find a page, a name or an id"
              className="h-full min-w-0 flex-1 bg-transparent text-base outline-none placeholder:text-hint"
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setActive(0);
              }}
              onKeyDown={onKeyDown}
            />
            {query ? (
              <button
                type="button"
                aria-label="Clear the text"
                className="focus-ring rounded-sm p-1 text-muted-foreground hover:text-foreground"
                onClick={() => {
                  setQuery("");
                  setActive(0);
                }}
              >
                <X className="size-4" />
              </button>
            ) : null}
            <Kbd>Esc</Kbd>
          </div>
          <div ref={scroller} className="min-h-0 flex-1 overflow-y-auto p-2">
            {noResults ? <p className="px-3 pt-3 pb-2 text-sm text-muted-foreground">No page, action or item matches this text.</p> : null}
            <div id={listId} role="listbox" aria-label="Search results">
              {numbered.map((g) => (
                <div key={g.id} role="group" aria-labelledby={g.heading ? `${listId}-group-${g.id}` : undefined} aria-label={g.heading ? undefined : "Other ways"} aria-busy={g.loading ? true : undefined}>
                  {g.heading ? (
                    <div id={`${listId}-group-${g.id}`} className="kicker px-3 pt-3 pb-1">
                      {g.heading}
                    </div>
                  ) : null}
                  {g.loading && !g.rows.length ? (
                    <div className="px-3 py-2.5">
                      <Skeleton className="h-4 w-2/5" />
                    </div>
                  ) : null}
                  {g.rows.map(({ row, i }) => (
                    <div
                      key={row.key}
                      id={optionId(i)}
                      role="option"
                      aria-selected={i === active}
                      onMouseMove={() => (i === active ? undefined : setActive(i))}
                      onClick={(e) => activate(row, e.metaKey || e.ctrlKey)}
                      className={cn(
                        "flex min-h-10 cursor-pointer items-center gap-3 rounded-md px-3 py-2 text-sm",
                        // the keyboard's place: the accent tint plus a ring-coloured bar (3:1 on the
                        // popover), since the tint alone is about 1.1:1 against it
                        i === active && "bg-accent text-accent-foreground shadow-[inset_3px_0_0_var(--ring)]",
                      )}
                    >
                      <row.icon className={cn("size-4 shrink-0", i === active ? "text-accent-foreground" : "text-muted-foreground")} aria-hidden />
                      <span data-title="" className="shrink-0 font-medium max-sm:max-w-[60%] max-sm:truncate sm:max-w-[70%] sm:truncate">
                        <Marked text={row.title} ranges={row.ranges} />
                      </span>
                      {/* muted grey is under 4.5:1 on the accent tint: the active row's lines take its text colour */}
                      {row.secondary ? <span className={cn("min-w-0 flex-1 truncate text-xs", i === active ? "text-accent-foreground" : "text-muted-foreground")}>{row.secondary}</span> : null}
                      <span className={cn("ml-auto shrink-0 pl-2 text-xs", i === active ? "text-accent-foreground" : "text-hint")}>{row.kind}</span>
                    </div>
                  ))}
                </div>
              ))}
            </div>
          </div>
          <div role="status" className="sr-only">
            {typed && !(loading && rows.length === 0) ? resultsText(noResults ? 0 : rows.length) : ""}
          </div>
          <div data-testid="search-hints" className="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-1 border-t px-4 py-2 text-xs text-muted-foreground">
            <span className="flex items-center gap-1">
              <Kbd>↑</Kbd>
              <Kbd>↓</Kbd>
              Move
            </span>
            <span className="flex items-center gap-1">
              <Kbd>↵</Kbd>
              Open
            </span>
            <span className="flex items-center gap-1">
              <Kbd>Esc</Kbd>
              Close
            </span>
          </div>
          {open && remoteQuery && groupIds.length ? (
            <Suspense fallback={null}>
              <RemoteSources query={remoteQuery} groups={groupIds} report={report} />
            </Suspense>
          ) : null}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

const SHORTCUTS: [string[], string][] = [
  [["⌘", "K"], "Search"],
  [["/"], "Search"],
  [["?"], "This help"],
  [["⌘", "B"], "Collapse the sidebar"],
  ...[...EXPERT_NAV, SETTINGS_NAV].map((n): [string[], string] => [["g", n.chord], `Go to ${n.label}`]),
  [["⌘", "S"], "Editor: save revision"],
  [["⌘", "Enter"], "Editor: validate the spec now"],
];

export function ShortcutsDialog() {
  const open = useUi((s) => s.shortcutsOpen);
  const setOpen = useUi((s) => s.setShortcutsOpen);
  // The list is written with the Apple key; other platforms use Ctrl for the same shortcuts.
  const apple = isApplePlatform();
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent>
        <DialogTitle>Keyboard shortcuts</DialogTitle>
        <DialogDescription>Single-key shortcuts work only when no text field has the focus.</DialogDescription>
        <dl className="grid grid-cols-[auto_1fr] items-center gap-x-6 gap-y-2 text-sm">
          {SHORTCUTS.map(([keys, label]) => (
            <div key={keys.join(" ")} className="contents">
              <dt className="flex gap-1">
                {keys.map((k) => (
                  <Kbd key={k}>{k === "⌘" && !apple ? "Ctrl" : k}</Kbd>
                ))}
              </dt>
              <dd className="text-muted-foreground">{label}</dd>
            </div>
          ))}
        </dl>
      </DialogContent>
    </Dialog>
  );
}
