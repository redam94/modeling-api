import {
  Activity,
  Boxes,
  Database,
  FileCode2,
  Gauge,
  Home,
  Library,
  Settings,
  Sparkles,
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

export const EXPERT_NAV: NavItem[] = [
  { to: "/", label: "Home", icon: Home, chord: "h", description: "Activity and health at a glance" },
  { to: "/datasets", label: "Datasets", icon: Database, chord: "d", description: "Upload, map and inspect data" },
  { to: "/build", label: "Build", icon: FileCode2, chord: "b", description: "Write and validate model specs" },
  { to: "/runs", label: "Runs", icon: Activity, chord: "r", description: "Fits, dry-runs, ingests and scores" },
  { to: "/models", label: "Models", icon: Boxes, chord: "m", description: "Inspect fitted model versions" },
  { to: "/score", label: "Score", icon: Gauge, chord: "s", description: "Forecasts, scenarios and counterfactuals" },
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
