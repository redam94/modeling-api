import { useNavigate } from "@tanstack/react-router";
import { ArrowRight, Hash, Moon, Search, Sun } from "lucide-react";
import { useMemo, useState, type KeyboardEvent } from "react";

import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/overlays";
import { Kbd } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import { useUi } from "@/stores/ui";

import { EXPERT_NAV, SETTINGS_NAV } from "./nav";

interface Command {
  id: string;
  label: string;
  hint: string;
  icon: React.ReactNode;
  run(): void;
}

const ID_PATTERNS: [RegExp, (id: string) => string, string][] = [
  [/^run_[0-9a-z]{6,}$/, (id) => `/runs/${id}`, "Open run"],
  [/^ds_[0-9a-z]{6,}$/, (id) => `/datasets/${id}`, "Open dataset"],
  [/^spc_[0-9a-z]{6,}$/, (id) => `/build/${id}`, "Open spec"],
  [/^mdl_[0-9a-z]{6,}$/, (id) => `/models/${id}`, "Open model"],
];

/** Score a label against a query: substring first, then in-order subsequence. */
export function fuzzyScore(label: string, query: string): number {
  const l = label.toLowerCase();
  const q = query.toLowerCase().trim();
  if (!q) return 1;
  const idx = l.indexOf(q);
  if (idx >= 0) return 100 - idx;
  let pos = 0;
  for (const ch of q) {
    pos = l.indexOf(ch, pos);
    if (pos < 0) return 0;
    pos += 1;
  }
  return 10;
}

export function CommandPalette() {
  const open = useUi((s) => s.paletteOpen);
  const setOpen = useUi((s) => s.setPaletteOpen);
  const setTheme = useUi((s) => s.setTheme);
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);

  const commands = useMemo<Command[]>(() => {
    const go = (to: string) => () => void navigate({ to });
    const list: Command[] = [...EXPERT_NAV, SETTINGS_NAV].map((n) => ({
      id: `nav:${n.to}`,
      label: n.label,
      hint: n.description,
      icon: <n.icon className="size-4" />,
      run: go(n.to),
    }));
    list.push(
      { id: "new-spec", label: "New spec", hint: "Open the editor with a starter spec", icon: <ArrowRight className="size-4" />, run: go("/build/new") },
      { id: "theme-dark", label: "Use dark theme", hint: "Theme", icon: <Moon className="size-4" />, run: () => setTheme("dark") },
      { id: "theme-light", label: "Use light theme", hint: "Theme", icon: <Sun className="size-4" />, run: () => setTheme("light") },
    );
    const q = query.trim();
    for (const [re, to, label] of ID_PATTERNS) {
      if (re.test(q)) list.unshift({ id: `id:${q}`, label: `${label} ${q}`, hint: "By id", icon: <Hash className="size-4" />, run: go(to(q)) });
    }
    return list
      .map((c) => ({ c, s: Math.max(fuzzyScore(c.label, q), c.id.startsWith("id:") ? 1000 : 0) }))
      .filter((x) => x.s > 0)
      .sort((a, b) => b.s - a.s)
      .map((x) => x.c);
  }, [query, navigate, setTheme]);

  const close = () => {
    setOpen(false);
    setQuery("");
    setActive(0);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((a) => Math.min(commands.length - 1, a + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => Math.max(0, a - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const cmd = commands[active];
      if (cmd) {
        close();
        cmd.run();
      }
    }
  };

  return (
    <Dialog open={open} onOpenChange={(o) => (o ? setOpen(true) : close())}>
      <DialogContent className="gap-0 p-0" aria-describedby={undefined}>
        <DialogTitle className="sr-only">Command palette</DialogTitle>
        <div className="flex items-center gap-2 border-b px-3">
          <Search className="size-4 text-muted-foreground" />
          <input
            autoFocus
            role="combobox"
            aria-expanded="true"
            aria-controls="palette-list"
            aria-activedescendant={commands[active] ? `cmd-${commands[active].id}` : undefined}
            placeholder="Go to a page, or paste a run / dataset / spec / model id"
            className="h-12 w-full bg-transparent text-sm outline-none placeholder:text-muted-foreground"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setActive(0);
            }}
            onKeyDown={onKeyDown}
          />
        </div>
        <ul id="palette-list" role="listbox" className="max-h-80 overflow-y-auto p-1.5">
          {commands.map((c, i) => (
            <li
              key={c.id}
              id={`cmd-${c.id}`}
              role="option"
              aria-selected={i === active}
              onMouseEnter={() => setActive(i)}
              onClick={() => {
                close();
                c.run();
              }}
              className={cn(
                "flex cursor-pointer items-center gap-3 rounded-md px-3 py-2 text-[13px]",
                i === active && "bg-accent text-accent-foreground",
              )}
            >
              <span className="text-muted-foreground">{c.icon}</span>
              <span className="font-medium">{c.label}</span>
              <span className="ml-auto truncate text-xs text-muted-foreground">{c.hint}</span>
            </li>
          ))}
          {commands.length === 0 ? <li className="px-3 py-6 text-center text-muted-foreground">No matches.</li> : null}
        </ul>
      </DialogContent>
    </Dialog>
  );
}

const SHORTCUTS: [string[], string][] = [
  [["⌘", "K"], "Command palette"],
  [["/"], "Command palette"],
  [["?"], "This help"],
  [["⌘", "B"], "Collapse the sidebar"],
  ...[...EXPERT_NAV, SETTINGS_NAV].map((n): [string[], string] => [["g", n.chord], `Go to ${n.label}`]),
  [["⌘", "S"], "Editor: save revision"],
  [["⌘", "Enter"], "Editor: validate now"],
];

export function ShortcutsDialog() {
  const open = useUi((s) => s.shortcutsOpen);
  const setOpen = useUi((s) => s.setShortcutsOpen);
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent>
        <DialogTitle>Keyboard shortcuts</DialogTitle>
        <DialogDescription>Single keys work when no text field has focus.</DialogDescription>
        <dl className="grid grid-cols-[auto_1fr] items-center gap-x-6 gap-y-2 text-[13px]">
          {SHORTCUTS.map(([keys, label]) => (
            <div key={label} className="contents">
              <dt className="flex gap-1">
                {keys.map((k) => (
                  <Kbd key={k}>{k}</Kbd>
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
