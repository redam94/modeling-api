/**
 * v0.15: the "GPU" badge of a model that can use the GPU resource class
 * (`ModelManifest.resources.gpu`), with the help text of the class as its tooltip.
 */
import { Cpu } from "lucide-react";

import type { ModelCatalogEntry } from "@/api/types";
import { Badge } from "@/components/ui/primitives";
import { GPU_CLASS_HELP } from "@/lib/run-state";

type ModelManifest = ModelCatalogEntry["manifest"];

/** The model can use the GPU class. */
export function usesGpu(manifest: Pick<ModelManifest, "resources"> | null | undefined): boolean {
  return manifest?.resources?.gpu === true;
}

export function GpuBadge({ manifest, className }: { manifest: Pick<ModelManifest, "resources"> | null | undefined; className?: string }) {
  if (!usesGpu(manifest)) return null;
  return (
    <Badge variant="info" className={className} title={`The model can use the GPU class. ${GPU_CLASS_HELP}`} data-testid="gpu-badge">
      <Cpu aria-hidden /> GPU
    </Badge>
  );
}
