/**
 * The recipe colours of earlier releases, each with the swatch that has the same role now.
 *
 * A plot document is stored when its model is fitted or when a user saves it, so its spec keeps
 * the colours of the release that made it. The sandbox draws such a colour as the colour of the
 * same role now, in the two themes: the old light value becomes the light value of today, and
 * the dark theme then applies the dark variant of that (`DARK_VARIANTS`). Without this table an
 * old colour got no dark variant, and near-black marks were invisible on the dark card.
 *
 * This table is apart from `recipe-palette.ts` on purpose: the Python test that holds
 * `DARK_VARIANTS` equal to `mapi_plots.palette.dark_variants()` reads every colour pair of that
 * file. When the palette changes again, add the light values that it drops here
 * (plots-core `test_the_plot_sandbox_maps_the_colours_of_earlier_releases` holds the two ends).
 */

/** `{old light: light of the same role now}`; keys and values are lowercase `#rrggbb`. */
export const LEGACY_COLOURS: Readonly<Record<string, string>> = {
  // Studio (v0.18 to v0.20)
  "#dcd8ff": "#bfdcec", // band
  "#3a2bd6": "#0072b2", // estimate
  "#18152f": "#1d1d1f", // observed
  "#c0182f": "#c00f2c", // alert
  "#0b6b3a": "#00704e", // ok
  "#84819b": "#86868b", // reference
  "#625f79": "#68686d", // muted
  "#2d2a45": "#424245", // strong
  "#6e6a8a": "#6e6e73", // baseline
  "#b56a00": "#8f5600", // scenario
  "#0a7fb5": "#1d93a5", // positive
  "#e0deec": "#e8e8ed", // empty
  // MEI (v0.16)
  "#c7dbff": "#bfdcec", // band
  "#0014cc": "#0072b2", // estimate
  "#121619": "#1d1d1f", // observed
  "#c20021": "#c00f2c", // alert
  "#036e1b": "#00704e", // ok
  "#767e8c": "#86868b", // reference
  "#5b6577": "#68686d", // muted
  "#333b48": "#424245", // strong
  "#6b7685": "#6e6e73", // baseline
  "#c45a00": "#8f5600", // scenario
  "#1f6fb4": "#1d93a5", // positive
  "#e3e6ec": "#e8e8ed", // empty
  // v0.15 and earlier. The band colour of those releases also drew lines, at a low opacity for the
  // areas: the estimate blue keeps the two legible (the pale band of today would not).
  "#3366cc": "#0072b2", // band and its lines
  "#1f4e99": "#0072b2", // estimate
  "#1b2333": "#1d1d1f", // observed
  "#c62828": "#c00f2c", // alert
  "#2e7d32": "#00704e", // ok
};
