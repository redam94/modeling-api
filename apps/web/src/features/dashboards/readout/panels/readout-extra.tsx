/**
 * The lazily loaded part of the layout 2 readout (plan D18): the heavy panel renderers, the
 * results of the inputs and the three input sections. The readout imports this module only
 * with `import()`, so it is one chunk (`readout-extra-*`, budget 48 KiB) that a dashboard with
 * only tiles, lines and notes never downloads.
 */
export { CheckAccuracy } from "../check-accuracy";
export { ScoreFile } from "../score-file";
export { TryACase } from "../try-a-case";
export { HeavyBlock } from "./heavy-blocks";
export { ReadoutResults } from "./results";
