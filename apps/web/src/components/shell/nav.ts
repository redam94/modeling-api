import {
  Activity,
  BadgeCheck,
  BarChart3,
  Boxes,
  Compass,
  Database,
  FileCode2,
  Gauge,
  Home,
  KeyRound,
  Library,
  Plug,
  Settings,
  ShieldCheck,
  Sparkles,
  Users,
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

/** Expert navigation; Guided stays one click away (opening it switches the mode). */
export const EXPERT_NAV: NavItem[] = [
  { to: "/", label: "Home", icon: Home, chord: "h", description: "Recent activity and the health of the project" },
  GUIDED_NAV,
  { to: "/datasets", label: "Datasets", icon: Database, chord: "d", description: "Upload, map and examine data" },
  { to: "/build", label: "Build", icon: FileCode2, chord: "b", description: "Write and validate model specs" },
  { to: "/runs", label: "Runs", icon: Activity, chord: "r", description: "Fits, dry-runs, ingests and scores" },
  { to: "/models", label: "Models", icon: Boxes, chord: "m", description: "Examine fitted model versions" },
  { to: "/score", label: "Score", icon: Gauge, chord: "s", description: "Forecasts, scenarios and counterfactuals" },
  // M3.5: automated analyses and the governance that validates models.
  WORKFLOWS_NAV,
  { to: "/governance", label: "Governance", icon: BadgeCheck, chord: "v", description: "Policies, validations and approvals" },
  INTEGRATIONS_NAV,
  { to: "/catalog", label: "Catalog", icon: Library, chord: "c", description: "About cards for all models, steps, checks and templates" },
  CHOOSE_NAV,
];

export const SETTINGS_NAV: NavItem = {
  to: "/settings",
  label: "Settings",
  icon: Settings,
  chord: ",",
  description: "Theme, mode and connection",
};

/**
 * Guided mode's navigation: Guided, Help me choose and the recipes first, then the Expert areas
 * (without Expert Home).
 */
export const GUIDED_MODE_NAV: NavItem[] = [GUIDED_NAV, CHOOSE_NAV, RECIPES_NAV, ...EXPERT_NAV.filter((item) => item.to !== "/" && item !== GUIDED_NAV && item !== CHOOSE_NAV && item !== WORKFLOWS_NAV)];

/** A Guided-locked project (members below admin): the Guided screens and the Integrations hub. */
export const LOCKED_NAV: NavItem[] = [GUIDED_NAV, CHOOSE_NAV, RECIPES_NAV, INTEGRATIONS_NAV];
