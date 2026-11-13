/**
 * Catalog glyphs: small inline SVG drawings built from chart parts and coloured by role token
 * classes only. Decorative by default (`aria-hidden`); pass `label` when the art carries meaning.
 * Signed-in surfaces only: never in readout, share or client markup, never inside plots (the
 * share and client bundles forbid this folder).
 *
 * The larger art (sign-in, Start here, empty states, not found) is generated raster art: see
 * `components/brand`.
 */
export { artA11y, type ArtProps } from "./art";
export { CatalogGlyph, glyphKind, glyphVariants, type GlyphKind } from "./catalog-glyph";
