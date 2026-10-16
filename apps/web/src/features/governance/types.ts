/** Governance types: aliases of the generated OpenAPI components (`mapi_spec.governance`). */
import type { components } from "@/api/schema";

type Arrayify<T> = T extends (infer E)[] ? Arrayify<E>[] : T extends object ? { [K in keyof T]: Arrayify<T[K]> } : T;
type S = { [K in keyof components["schemas"]]: Arrayify<components["schemas"][K]> };

export type GovernancePolicy = S["GovernancePolicy"];
export type GovernancePolicyCreate = S["GovernancePolicyCreate"];
export type GovernancePolicyUpdate = S["GovernancePolicyUpdate"];
export type PolicyCheck = S["PolicyCheck"];
export type PolicyMatch = S["PolicyMatch"];
export type CheckSeverity = S["CheckSeverity"];
export type CheckThresholds = S["CheckThresholds"];
export type CheckDirection = S["CheckDirection"];
export type GovernanceCheckDef = S["GovernanceCheckDef"];
export type ModelValidation = S["ModelValidation"];
export type ValidationStatus = S["ValidationStatus"];
export type ValidationEvent = S["ValidationEvent"];
export type ValidationRequest = S["ValidationRequest"];
export type ValidationTransitionRecord = S["ValidationTransitionRecord"];
export type Approval = S["Approval"];
export type ApprovalCreate = S["ApprovalCreate"];
export type CheckResult = S["CheckResult"];
export type CheckStatus = S["CheckStatus"];
export type EvidenceItem = S["EvidenceItem"];
export type EvidencePack = S["EvidencePack"];
export type EvidencePackRequest = S["EvidencePackRequest"];
export type Capability = S["Capability"];

export type PolicyScope = "project" | "tenant";
