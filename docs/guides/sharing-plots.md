# Sharing plots: saved plots, images and embed links

A model's plots live in the web app, rendered in the isolated plot sandbox. To use one
somewhere else — a slide, a report, a BI dashboard, a Notion page or a Slack thread — you can:

1. **save** it under a title (`POST /v1/plots`),
2. **render** it to PNG, SVG or PDF in the sandbox (`POST /v1/plots:render`), or
3. **embed** it with a tokenised, read-only, expiring link (`POST /v1/plots/{plot_id}/embeds`).

All three need a signed-in caller or an API token of the project. The embed link itself is
public: anyone who has it can see that one plot until it expires or is revoked.

Where each one fits: BI tools and Notion frame an **embed link** and show the live chart.
Chat tools such as Slack and Teams never frame a page; they unfurl a link into a card from its
page tags, and the embed page's tags are the same for every link (a "Shared chart" card with no
plot name and no image), so post a **rendered PNG** to a Slack thread and add the link if
people should open the live view.

## Save a plot

Save a plot of a model version by naming its source. The API checks the model version's
signed bundle manifest first, and passes the plot through the sanitizer again:

```bash
curl -s -X POST "$MAPI_URL/v1/plots" \
  -H "Authorization: Bearer $MAPI_TOKEN" -H "Content-Type: application/json" \
  -d '{
        "title": "EU response curves (v3)",
        "source": {"model_id": "mdl_01jaf3c1d2e3f4g5h6j7k8m9n0", "version": 3,
                   "plot_id": "mmm.response_curves"}
      }'
```

The answer is a `SavedPlot`. Its `embeddable` field says whether it can be shared by link (see
[what can be embedded](#what-can-be-embedded)).

You can also save a plot you wrote yourself (`"spec": {...}`, a `PlotSpec`). It must pass the
same sanitizer (`422 PLOT_UNSAFE` otherwise) and carry its data inline: `data_ref` datasets
belong to a model version, so save that plot with `source` instead. A hand-written plot is
always recorded with `author` provenance, whatever its `provenance` field claims.

`GET /v1/plots` lists the project's saved plots (newest first), `GET /v1/plots/{plot_id}` returns
one with its spec, and `DELETE /v1/plots/{plot_id}` deletes it together with its embed links.

## Render an image

`POST /v1/plots:render` turns one plot into a file. Name it by `saved_plot_id`, by `source` (a
model version's plot) or inline (`plot`):

```bash
curl -s -X POST "$MAPI_URL/v1/plots:render" \
  -H "Authorization: Bearer $MAPI_TOKEN" -H "Content-Type: application/json" \
  -H "Prefer: wait=30" \
  -d '{"saved_plot_id": "plt_01jaf3n1p2q3r4s5t6v7w8x9y0", "format": "pdf", "width": 1200, "height": 600}'
```

| Field | Default | Meaning |
|---|---|---|
| `format` | `png` | `png`, `svg` or `pdf` |
| `scale` | `2` | Pixel density of PNG and PDF (0 < scale ≤ 8); SVG ignores it |
| `width`, `height` | the plot's own | Size in CSS pixels before `scale` (16–4096). For a composite plot (facets, concatenations) it is the size of each panel |

A PNG or PDF holds at most 50 megapixels (`width x height x scale²` of the laid-out plot, all
panels included): a bigger request answers `413 PAYLOAD_TOO_LARGE` up front, or fails the
render run with that code before anything is drawn when only the layout reveals the size.
Lower the scale or the size.

Rendering is a `render` run in the sandbox, so it counts against your quotas like any small
run. With `Prefer: wait=N` (at most 60 seconds) the answer is `200` with signed links to
`renders/<plot>.<format>` when the run finished in time, otherwise `202` with the run. Poll
`GET /v1/plots/renders/{run_id}` until `done` is true. The links are short-lived: download the
file, don't store the URL.

!!! note "Vega-Lite only, for now"
    Vega-Lite plots render with vl-convert. Plotly plots need kaleido and a Chrome or
    Chromium in the runner image, which the platform's images do not ship, so Plotly renders
    answer `422 MODE_NOT_SUPPORTED` unless the operator enables them
    (`MAPI_RENDER_PLOTLY_ENABLED`). Export a Plotly plot from the plot viewer instead.

### Rendering never fetches anything

The renderer gets the sanitized plot and its data as pinned inputs of the run:

- the API writes the plot as the run's `plots.json` and pins each of its bundle datasets
  (`data_ref`) to the verified object generation, as run inputs named `plotdata.<n>`;
- the sanitizer has already refused URL data, `href` encodings, image marks and Plotly
  layout images or tile maps, and the runner refuses them again before it draws;
- vl-convert runs with network access turned off (`allowed_base_urls=[]`), and the sandbox
  has no egress anyway.

An image is capped at 32 MiB and the run at `MAPI_RENDER_TIMEOUT_S` (5 minutes by default).

## Embed links

```bash
curl -s -X POST "$MAPI_URL/v1/plots/plt_01jaf3n1p2q3r4s5t6v7w8x9y0/embeds" \
  -H "Authorization: Bearer $MAPI_TOKEN" -H "Content-Type: application/json" \
  -d '{"label": "Q4 board deck", "expires_in_days": 90,
       "allowed_origins": ["https://acme.notion.site"]}'
```

The answer (`PlotEmbedCreated`) is the **only** time you see the link:

```json
{
  "embed": {"id": "emb_01jaf3p1q2r3s4t5v6w7x8y9z0", "display": "mpe_7k2m9q…", "status": "active", "...": "..."},
  "url": "https://plots.mapi.example/embed/mpe_7k2m9q4x0123456789abcdefghjkmnpqrstvwxyz",
  "token": "mpe_7k2m9q4x0123456789abcdefghjkmnpqrstvwxyz"
}
```

The platform stores only a keyed hash of the token, like API tokens. Paste the `url` into the
tool that shows the plot, or frame it:

```html
<iframe src="https://plots.mapi.example/embed/mpe_7k2m…" width="900" height="520"
        style="border:0" loading="lazy" title="EU response curves"></iframe>
```

| Option | Default | Meaning |
|---|---|---|
| `expires_in_days` | 30 | 1 to 365 days |
| `label` | none | Your note, shown in the list of links |
| `allowed_origins` | any site | The https sites (`https://host[:port]`, at most 10) whose pages may show the plot in a frame. Opened directly, the link always works |

`GET /v1/plots/{plot_id}/embeds` lists a plot's links with their status (`active`, `expired`
or `revoked`), view count and last view, never their tokens. `DELETE
/v1/plots/{plot_id}/embeds/{embed_id}` revokes one; revoking twice is harmless. Creating and
revoking links are recorded in the audit log (`plot.embed_create`, `plot.embed_revoke`).

### What can be embedded

Only plots that a model version drew with one of the **platform's own recipes** (`core.*`,
`mmm.*`, `ml.*`, `ts.*`, `choice.*`). Plots written by a model plugin, and plots written by
hand, are refused with `422 EMBED_NOT_ALLOWED`. The rule exists because a shared link runs the
plot's spec in a stranger's browser: even inside the sandbox a spec could navigate its own
frame, so shared views only ever run specs the platform wrote. Share other plots inside the
project, or render them to an image.

A model version fitted with uploaded plugin code (a dev or vetted overlay) can embed none of
its plots, whatever recipe they name: the same worker that writes the plot documents runs
that code, so the server decides from the version's lineage, not from what a plot says about
itself. Existing links to such plots stop serving them.

An embed also carries its data inline, so a dataset larger than 1.5 MB cannot be embedded.

### The embed page

The link opens a page of the plot sandbox (`/embed/<token>`). It reads the plot from the API's
public `GET /v1/embeds/{token}` and draws it with the same safeguards as the app's plot
frames:

- the request sends no cookie or credential, and the API ignores any it is given;
- the page's Content-Security-Policy lets it fetch the API's `/v1/embeds/` path and nothing
  else; the API answers cross-origin reads from the plot-sandbox origin only;
- the answer is validated again in the browser (inline data only, the same unsafe-construct
  checks as the app's plot frames), and every text is written as text;
- framed by a site that is not in `allowed_origins`, the page shows a notice instead of the
  plot.

When you can start render runs (the `runner` role), a PNG of the plot is rendered when you
create the link. The page then offers an **Open as PNG** link, for tools that cannot show
interactive content. Editors can create links too, without that image.

### Errors

| Status | Code | When |
|---|---|---|
| 404 | `NOT_FOUND` | The token does not exist, or its plot was deleted |
| 410 | `LINK_EXPIRED` | The link expired, was revoked, or its tenant is suspended |
| 429 | `RATE_LIMITED` | Too many views of this link (`MAPI_EMBED_RATE_PER_MINUTE`), or too many requests from one address (`MAPI_EMBED_IP_RATE_PER_MINUTE`); wait for `Retry-After` |

## Hosting the embed page

The plot sandbox build has two pages: `index.html` (the app's plot frames) and `embed.html`,
served for every `/embed/<token>` path. The embed page needs the API's origin:

- in development, `VITE_API_URL` (default `http://127.0.0.1:8000`) is built in;
- the `mapi/web` image builds it with a placeholder and nginx fills in `MAPI_API_URL` when
  it serves the page, together with the page's own headers (CSP with
  `connect-src … $MAPI_API_URL/v1/embeds/`, `frame-ancestors 'self' https:`).

The API allows cross-origin reads of `/v1/embeds/` from `MAPI_PLOTS_ORIGIN` only, so that
setting must be the origin the embed page is served from.
