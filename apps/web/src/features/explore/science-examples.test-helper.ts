/**
 * The example views of the sections for scientific data, as the spec freezes them
 * (`packages/spec/tests/data/explorer_science/<section>.json`, valid against `mapi_spec.views`).
 * For tests only (reads files with node:fs).
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { parseView, type ScienceSection, type SectionViews } from "./views";

const DIR = resolve(__dirname, "../../../../../packages/spec/tests/data/explorer_science");

/** The example view of a section, checked against its format tag. */
export function scienceExample<S extends ScienceSection>(section: S): SectionViews[S] {
  return parseView(section, JSON.parse(readFileSync(resolve(DIR, `${section}.json`), "utf-8")));
}

/** A deep copy (to change an example in one test only). */
export function clone<T>(v: T): T {
  return JSON.parse(JSON.stringify(v)) as T;
}
