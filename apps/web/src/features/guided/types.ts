/**
 * DTO aliases Guided mode uses (generated from the server's OpenAPI document). Kept beside the
 * feature so `src/api/types.ts` stays the shared list of the Expert screens.
 */
import type { components } from "@/api/schema";

type Arrayify<T> = T extends (infer E)[] ? Arrayify<E>[] : T extends object ? { [K in keyof T]: Arrayify<T[K]> } : T;
type S = { [K in keyof components["schemas"]]: Arrayify<components["schemas"][K]> };

export type GuidedTemplate = S["GuidedTemplate"];
export type GuidedQuestion = S["GuidedQuestion"];
export type RoleSuggestions = S["RoleSuggestions"];
export type QuestionSuggestion = S["QuestionSuggestion"];
export type RoleCandidate = S["RoleCandidate"];
export type ColumnProfile = S["ColumnProfile"];
export type ColumnKind = ColumnProfile["dtype"];
export type GuidedAnswers = S["GuidedAnswers"];
export type GuidedDraft = S["GuidedDraft"];
export type BusinessPriors = S["BusinessPriors"];
export type ChannelBelief = S["ChannelBelief"];
export type ShareBelief = S["ShareBelief"];
export type LiftTest = S["LiftTest"];
export type PriorTranslation = S["PriorTranslation"];
export type PriorTranslationRequest = S["PriorTranslationRequest"];
export type PriorTranslationResult = S["PriorTranslationResult"];
export type PriorPreview = S["PriorPreview"];
export type DistributionSummary = S["DistributionSummary"];
export type FanChart = S["FanChart"];
export type WhatIfCompile = S["WhatIfCompile"];
export type ForecastCompile = S["ForecastCompile"];
export type CounterfactualCompile = S["CounterfactualCompile"];
export type GuidedCompileBody = WhatIfCompile | ForecastCompile | CounterfactualCompile;
export type Endpoint = S["Endpoint"];
export type EndpointPromote = S["EndpointPromote"];
export type PromotionGateDecision = S["PromotionGateDecision"];
export type GateReason = S["GateReason"];
export type SavedPlot = S["SavedPlot"];
export type SavedPlotCreate = S["SavedPlotCreate"];
export type PlotEmbedCreate = S["PlotEmbedCreate"];
export type PlotEmbedCreated = S["PlotEmbedCreated"];
export type TenantProject = S["Project"];

/** The JSON document of a stored ModelSpec revision (as the API returns it). */
export type SpecDocument = Record<string, unknown>;
