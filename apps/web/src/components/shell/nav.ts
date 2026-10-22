import {
  Activity,
  BadgeCheck,
  BarChart3,
  Boxes,
  Compass,
  Database,
  FileCode2,
  FlaskConical,
  Gauge,
  Home,
  KeyRound,
  LayoutDashboard,
  Library,
  Plug,
  Settings,
  ShieldCheck,
  Sparkles,
  Users,
  WandSparkles,
  Workflow,
  type LucideIcon,
} from "lucide-react";

export interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  /** Two-key shortcut after "g". */
  chord: string;
  description: string;
}

/** The selected project's people, credentials and spend (v0.3 servers only). */
export const PROJECT_NAV: NavItem[] = [
  { to: "/usage", label: "Usage", icon: BarChart3, chord: "u", description: "Compute, cost and quotas" },
  { to: "/members", label: "Members", icon: Users, chord: "p", description: "The persons in this project and their roles" },
  { to: "/tokens", label: "Tokens", icon: KeyRound, chord: "t", description: "API tokens for programs and integrations" },
];

/** Platform operators only. */
export const ADMIN_NAV: NavItem = {
  to: "/admin",
  label: "Admin",
  icon: ShieldCheck,
  chord: "a",
  description: "Tenants, quotas, kill switches, executors and audit",
};

/** The Integrations hub: in Expert navigation, and beside Guided in a Guided-locked project. */
export const INTEGRATIONS_NAV: NavItem = {
  to: "/integrations",
  label: "Integrations",
  icon: Plug,
  chord: "i",
  description: "Endpoints, snippets, webhooks and share links",
};

export const GUIDED_NAV: NavItem = {
  to: "/guided",
  label: "Guided",
  icon: Sparkles,
  chord: "g",
  description: "Tasks for analysts in business terms",
};

/** "Help me choose" (M3.5): a few plain questions, then the models, analyses and recipes that fit. */
export const CHOOSE_NAV: NavItem = {
  to: "/choose",
  label: "Help me choose",
  icon: Compass,
  chord: "o",
  description: "Answer some questions to find the applicable analyses",
};

/** Workflows as Expert users see them (the editor, runs and automation)… */
export const WORKFLOWS_NAV: NavItem = {
  to: "/workflows",
  label: "Workflows",
  icon: Workflow,
  chord: "w",
  description: "Tests, models and checks in one automated graph",
};

/** …and as Guided users see them: ready-made recipes run on their data (the same route). */
export const RECIPES_NAV: NavItem = {
  ...WORKFLOWS_NAV,
  label: "Recipes",
  description: "Ready-made analyses that check your data, fit a model and explain each step",
};

/** v0.18 (D1, D23): "Make your own", the team's no-code models, prep presets, chart styles and workflow templates. */
export const LIBRARY_NAV: NavItem = {
  to: "/library",
  label: "Make your own",
  icon: WandSparkles,
  chord: "y",
  description: "Your team's own model recipes, prep presets, chart templates and workflow templates",
};

const HOME_NAV: NavItem = { to: "/", label: "Home", icon: Home, chord: "h", description: "Start here, recent activity and the health of the project" };
const DATASETS_NAV: NavItem = { to: "/datasets", label: "Datasets", icon: Database, chord: "d", description: "Upload, map and examine data" };
const BUILD_NAV: NavItem = { to: "/build", label: "Build", icon: FileCode2, chord: "b", description: "Write and validate model specs" };
const RUNS_NAV: NavItem = { to: "/runs", label: "Runs", icon: Activity, chord: "r", description: "Fits, dry-runs, ingests and scores" };
// v0.14: named groups of runs to compare.
const EXPERIMENTS_NAV: NavItem = { to: "/experiments", label: "Experiments", icon: FlaskConical, chord: "e", description: "Named groups of runs to compare" };
const MODELS_NAV: NavItem = { to: "/models", label: "Models", icon: Boxes, chord: "m", description: "Examine fitted model versions" };
const SCORE_NAV: NavItem = { to: "/score", label: "Score", icon: Gauge, chord: "s", description: "Forecasts, scenarios and counterfactuals" };
// M3.5: the governance that validates models.
const GOVERNANCE_NAV: NavItem = { to: "/governance", label: "Governance", icon: BadgeCheck, chord: "v", description: "Policies, validations and approvals" };
// v0.16: client dashboards.
const DASHBOARDS_NAV: NavItem = { to: "/dashboards", label: "Dashboards", icon: LayoutDashboard, chord: "k", description: "Readouts for clients, with access and share links" };
const CATALOG_NAV: NavItem = { to: "/catalog", label: "Catalog", icon: Library, chord: "c", description: "About cards for all models, steps, checks and templates" };

export interface NavGroup {
  label: string;
  items: NavItem[];
}

/**
 * Expert navigation in three labelled groups (v0.18 D23); Guided stays one click away
 * (opening it switches the mode). Labels and chords are unchanged from v0.17.
 */
export const EXPERT_NAV_GROUPS: NavGroup[] = [
  { label: "Work", items: [HOME_NAV, GUIDED_NAV, DATASETS_NAV, BUILD_NAV, RUNS_NAV, EXPERIMENTS_NAV, MODELS_NAV, SCORE_NAV] },
  { label: "Automate & share", items: [WORKFLOWS_NAV, GOVERNANCE_NAV, INTEGRATIONS_NAV, DASHBOARDS_NAV] },
  { label: "Learn", items: [CATALOG_NAV, LIBRARY_NAV, CHOOSE_NAV] },
];

/** Expert navigation as one list (the command palette, chords and the phone nav). */
export const EXPERT_NAV: NavItem[] = EXPERT_NAV_GROUPS.flatMap((g) => g.items);

export const SETTINGS_NAV: NavItem = {
  to: "/settings",
  label: "Settings",
  icon: Settings,
  chord: ",",
  description: "Theme, mode and connection",
};

/**
 * Guided mode's navigation (v0.18 D23): seven items an analyst needs first (Guided, Help me
 * choose and the recipes, then dashboards, data, models and Make your own)…
 */
export const GUIDED_PRIMARY_NAV: NavItem[] = [GUIDED_NAV, CHOOSE_NAV, RECIPES_NAV, DASHBOARDS_NAV, DATASETS_NAV, MODELS_NAV, LIBRARY_NAV];

/** …and the other Expert areas (without Expert Home) behind "More". */
export const GUIDED_MORE_NAV: NavItem[] = EXPERT_NAV.filter((item) => item !== HOME_NAV && item !== WORKFLOWS_NAV && !GUIDED_PRIMARY_NAV.includes(item));

/** Guided mode's navigation as one list: the seven, then "More". */
export const GUIDED_MODE_NAV: NavItem[] = [...GUIDED_PRIMARY_NAV, ...GUIDED_MORE_NAV];

/** Is `pathname` one of the pages behind Guided mode's "More"? (It then opens by itself.) */
export function inGuidedMore(pathname: string): boolean {
  return GUIDED_MORE_NAV.some((item) => pathname === item.to || pathname.startsWith(`${item.to}/`));
}


/** A Guided-locked project (members below admin): the Guided screens and the Integrations hub. */
export const LOCKED_NAV: NavItem[] = [GUIDED_NAV, CHOOSE_NAV, RECIPES_NAV, INTEGRATIONS_NAV];
