import { describe, expect, it } from "vitest";

import type { WorkflowDoc } from "@/features/workflows/types";

import { mockValidateWorkflow } from "./workflow-validate";
import { CATALOG, TEMPLATES } from "./workflows";
import generated from "./workflows.gen.json";

describe("the mock workflow validator", () => {
  it.each(TEMPLATES.map((t) => [t.id, t] as const))("accepts the gallery template %s", (_id, t) => {
    const v = mockValidateWorkflow(t.document as unknown as WorkflowDoc, CATALOG);
    expect(v.issues?.filter((i) => i.severity === "error")).toEqual([]);
    expect(v.valid).toBe(true);
  });

  it.each(generated.validation_vectors.map((v) => [v.name, v] as const))("reports what the server reports for %s", (_name, vector) => {
    const v = mockValidateWorkflow(vector.document as unknown as WorkflowDoc, CATALOG);
    expect(v.valid).toBe(vector.valid);
    const got = (v.issues ?? []).filter((i) => i.severity === "error").map((i) => `${i.code} ${i.pointer}`);
    expect(new Set(got)).toEqual(new Set(vector.issues.map((i) => `${i.code} ${i.pointer}`)));
  });
});
