/**
 * Studio illustrations (plans/v018-direction.md §6): inline SVG React components built from MMM
 * chart parts and coloured by role token classes only. Decorative by default (`aria-hidden`);
 * pass `label` when the art carries meaning. Signed-in surfaces only: never in readout, share or
 * client markup, never inside plots (the share and client bundles forbid this folder).
 *
 * Pass empty-state art to `EmptyState`'s `illustration` slot; `components/common.tsx` never
 * imports this module, so pages without art do not load it.
 */
export { Art, artA11y, type ArtProps } from "./art";
export { CatalogGlyph, glyphKind, glyphVariants, type GlyphKind } from "./catalog-glyph";
export { EmptyDashboardsArt } from "./empty-dashboards";
export { EmptyDatasetsArt } from "./empty-datasets";
export { EmptyLibraryArt } from "./empty-library";
export { EmptyModelsArt } from "./empty-models";
export { EmptyRunsArt } from "./empty-runs";
export { EmptySearchArt } from "./empty-search";
export { EmptyWorkflowsArt } from "./empty-workflows";
export { HeroArt } from "./hero";
