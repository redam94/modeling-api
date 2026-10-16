import {
  Activity,
  BarChart3,
  Boxes,
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
  { to: "/members", label: "Members", icon: Users, chord: "p", description: "Who can do what in this project" },
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

export const EXPERT_NAV: NavItem[] = [
  { to: "/", label: "Home", icon: Home, chord: "h", description: "Activity and health at a glance" },
  { to: "/datasets", label: "Datasets", icon: Database, chord: "d", description: "Upload, map and inspect data" },
  { to: "/build", label: "Build", icon: FileCode2, chord: "b", description: "Write and validate model specs" },
  { to: "/runs", label: "Runs", icon: Activity, chord: "r", description: "Fits, dry-runs, ingests and scores" },
  { to: "/models", label: "Models", icon: Boxes, chord: "m", description: "Inspect fitted model versions" },
  { to: "/score", label: "Score", icon: Gauge, chord: "s", description: "Forecasts, scenarios and counterfactuals" },
  { to: "/integrations", label: "Integrations", icon: Plug, chord: "i", description: "Endpoints, snippets, webhooks and share links" },
  { to: "/catalog", label: "Catalog", icon: Library, chord: "c", description: "Models, steps and plot recipes" },
];

export const SETTINGS_NAV: NavItem = {
  to: "/settings",
  label: "Settings",
  icon: Settings,
  chord: ",",
  description: "Theme, mode and connection",
};

export const GUIDED_NAV: NavItem = {
  to: "/guided",
  label: "Guided",
  icon: Sparkles,
  chord: "g",
  description: "Task-based flows for analysts",
};
