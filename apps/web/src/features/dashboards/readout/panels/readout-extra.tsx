/**
 * The lazily loaded heavy part of the layout 2 readout (plan D18): the heavy panel renderers
 * and the results of the inputs (predictions, batch summaries, accuracy). The readout imports
 * this module only with `import()`, so it is one chunk (`readout-extra-*`, budget 48 KiB) that
 * a dashboard with only tiles, lines and notes never downloads. The three input sections (Try a
 * case, Check accuracy, Score a file) are lazy chunks of their own, each with a budget.
 */
export { HeavyBlock } from "./heavy-blocks";
export { ReadoutResults } from "./results";
