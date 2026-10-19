/**
 * The section list of the Data Explorer from the indexes and the section states (pure; see
 * `shapes.test.ts`). Since v0.10 the runner detects the shapes of the data (images, maps,
 * networks, series, ...), orders the sections by relevance (`ExplorerIndex.order`) and marks
 * each section that does not apply (`reason: "not_applicable"`). Those sections leave the
 * list and go into a closed "Not for this data" list; they still open from a link. An index
 * from before v0.10 (no `order`, no `reason`) keeps the thirteen sections of v0.5.
 */
import type { Exploration, SectionState } from "./api";
import { CLASSIC_ORDER, DATA_ORDER, DATA_SECTION_META } from "./sections-meta";
import { indexOrder, indexShapes, isScienceSection, skipReason, type DataSection, type ExplorerIndexView, type ShapeKind } from "./views";

export interface HiddenSection {
  section: DataSection;
  title: string;
  /** Why it does not apply, in plain words. */
  message: string;
}

export interface NavPlan {
  /** The sections of the list, by relevance. */
  shown: DataSection[];
  /** The sections that do not apply to this data (the "Not for this data" list). */
  hidden: HiddenSection[];
  /** The index tells which sections apply (v0.10 and later). */
  adaptive: boolean;
  /** The shapes the runner found (empty for an older index). */
  shapes: ShapeKind[];
}

type IndexLike = ExplorerIndexView | null | undefined;

/** The index of a finished explore run (its sections win over the ingest's). */
export function runIndex(run: Exploration | null | undefined): IndexLike {
  return run?.done ? (run.index) : null;
}

/** The index tells which sections apply: it has an `order`, or a section with a `reason`. */
export function isAdaptive(index: IndexLike): boolean {
  if (!index) return false;
  return indexOrder(index) !== null || (index.sections ?? []).some((s) => skipReason(s) !== null);
}

/** The order of the sections: the run's or the ingest's relevance order, then every other section. */
export function sectionOrder(indexes: readonly IndexLike[]): DataSection[] {
  const order = indexes.map(indexOrder).find((o) => o !== null) ?? [];
  return [...new Set([...order, ...DATA_ORDER])];
}

const DEFAULT_REASON = "This view does not apply to this data.";

/**
 * Which sections the list shows, in which order, and which ones go into "Not for this data".
 * `indexes`: the finished run's index first, then the ingest's.
 */
export function navPlan(states: readonly SectionState<DataSection>[], indexes: readonly IndexLike[]): NavPlan {
  const adaptive = indexes.some(isAdaptive);
  const byId = new Map(states.map((s) => [s.section, s]));
  const shapes = indexes.map(indexShapes).find((s) => s.length > 0) ?? [];
  if (!adaptive) {
    // Before v0.10: the thirteen sections, plus a section for scientific data that a run computed.
    const extra = DATA_ORDER.filter((s) => isScienceSection(s) && (byId.get(s)?.status ?? "missing") !== "missing");
    return { shown: [...CLASSIC_ORDER, ...extra], hidden: [], adaptive, shapes };
  }
  const shown: DataSection[] = [];
  const hidden: HiddenSection[] = [];
  for (const section of sectionOrder(indexes)) {
    const st = byId.get(section);
    if (st?.status === "skipped" && st.reason === "not_applicable") {
      hidden.push({ section, title: DATA_SECTION_META[section].title, message: st.message ?? DEFAULT_REASON });
    } else {
      shown.push(section);
    }
  }
  return { shown, hidden, adaptive, shapes };
}

/** The sections to compute when the user asks for "the remaining views": the missing ones of the list. */
export function missingSections(states: readonly SectionState<DataSection>[], plan: NavPlan): DataSection[] {
  const byId = new Map(states.map((s) => [s.section, s]));
  const pool = plan.adaptive ? plan.shown : DATA_ORDER;
  return pool.filter((s) => (byId.get(s)?.status ?? "missing") === "missing");
}

/**
 * "7 of 13 views are ready. ...": the sections of the list that are missing by name. For a
 * dataset from before v0.10, the sections for scientific data are not in the list: they are
 * named apart, so the numbers agree with the list.
 */
export function readyText(ready: number, listed: number, missing: readonly DataSection[], shown: readonly DataSection[]): string {
  const inList = missing.filter((s) => shown.includes(s));
  const extra = missing.length > inList.length;
  const names = inList.map((s) => DATA_SECTION_META[s].title.toLowerCase()).join(", ");
  if (!inList.length) return `${ready} of ${listed} views are ready. The sandbox computes the views for scientific data when you ask for them.`;
  return `${ready} of ${listed} views are ready. The sandbox computes the others (${names})${extra ? " and the views for scientific data" : ""} when you ask for them.`;
}

const SHAPE_WORDS: Record<ShapeKind, string> = {
  timeseries: "Series over time",
  sequence: "Series along an index",
  image: "Image",
  vector_field: "Vector field",
  areal_map: "Map of areas",
  points: "Points",
  network: "Network",
  tree: "Tree",
  circular: "Directions or times of day",
  profile: "Profile",
  table: "Table",
};

const SHAPE_SECTION: Record<ShapeKind, DataSection> = {
  timeseries: "timeseries",
  sequence: "timeseries",
  image: "images",
  vector_field: "images",
  areal_map: "map",
  points: "points",
  network: "network",
  tree: "tree",
  circular: "circular",
  profile: "profiles",
  table: "correlation",
};

/** "Image", "Map of areas", ... */
export function shapeWord(kind: string): string {
  return SHAPE_WORDS[kind as ShapeKind] ?? kind.replaceAll("_", " ");
}

/** The section that shows a shape best: the shape's first known section, else the default one. */
export function shapeSection(shape: { kind: string; sections?: readonly string[] | undefined }): DataSection {
  const known = new Set<string>(DATA_ORDER);
  const first = (shape.sections ?? []).find((s) => known.has(s));
  return (first as DataSection | undefined) ?? SHAPE_SECTION[shape.kind as ShapeKind] ?? "structure";
}
