/**
 * The YAML tab, loaded when someone opens it: the `yaml` package and the YAML diagnostics stay
 * out of the Make your own page chunk (library-* budget).
 */
import { lazy, Suspense, type ComponentProps } from "react";

import { LoadingRows } from "@/components/common";

import type { YamlTab } from "./yaml-tab";

const YamlTabChunk = lazy(() => import("./yaml-tab"));

export function LazyYamlTab(props: ComponentProps<typeof YamlTab>) {
  return (
    <Suspense fallback={<LoadingRows rows={4} />}>
      <YamlTabChunk {...props} />
    </Suspense>
  );
}
