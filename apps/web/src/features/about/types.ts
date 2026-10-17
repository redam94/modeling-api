/**
 * DTO aliases of the About cards, the chooser and dataset About (generated from the server's
 * OpenAPI document), kept beside the feature like `features/guided/types.ts`.
 */
import type { components } from "@/api/schema";

type Arrayify<T> = T extends (infer E)[] ? Arrayify<E>[] : T extends object ? { [K in keyof T]: Arrayify<T[K]> } : T;
type S = { [K in keyof components["schemas"]]: Arrayify<components["schemas"][K]> };

export type About = S["About"];
export type AboutKind = S["AboutKind"];
export type AboutReference = S["AboutReference"];
export type CatalogAbout = S["CatalogAbout"];
export type ChooserChoice = S["ChooserChoice"];
export type ChooserQuestion = S["ChooserQuestion"];
export type ChooserQuestionnaire = S["ChooserQuestionnaire"];
export type ChooserResult = S["ChooserResult"];
export type DatasetAbout = S["DatasetAbout"];
export type DatasetAboutUpdate = S["DatasetAboutUpdate"];
export type Difficulty = S["Difficulty"];
export type Recommendation = S["Recommendation"];
export type TypicalRuntime = S["TypicalRuntime"];

/** Answers by question id (several values for `multiple` questions). */
export type ChooserAnswers = Record<string, string[]>;

/** The kinds "Help me choose" ranks (`kind` of `GET /v1/catalog/recommendations`). */
export type ChooserKind = Exclude<AboutKind, "dataset" | "spec" | "split" | "node_kind">;
