# Sharing plots: saved plots, images and embed links

The plots of a model are in the web app. The isolated plot sandbox renders them. You can use a
plot in a different place, for example a slide, a report, a BI dashboard, a Notion page or a
Slack thread. To do this, you can:

1. **save** the plot with a title (`POST /v1/plots`),
2. **render** the plot to PNG, SVG or PDF in the sandbox (`POST /v1/plots:render`), or
3. **embed** the plot with a link that has a token, is read-only and expires
   (`POST /v1/plots/{plot_id}/embeds`).

The three actions need a signed-in caller or an API token of the project. The embed link is
public. All persons who have the link can see that one plot. They can see it until the link
expires or until you revoke it.

Use the correct method for each tool:

- BI tools and Notion put an **embed link** in a frame and show the live chart.
- Chat tools, for example Slack and Teams, never put a page in a frame. They change a link
  into a card from the tags of its page. The tags of the embed page are the same for all links:
  a "Shared chart" card with no plot name and no image. Thus, post a **rendered PNG** to a Slack
  thread. If people must open the live view, also add the link.

## Save a plot

To save a plot of a model version, name its source. The API first does a check of the signed
bundle manifest of the model version. Then it sends the plot through the sanitizer again:

```bash
curl -s -X POST "$MAPI_URL/v1/plots" \
  -H "Authorization: Bearer $MAPI_TOKEN" -H "Content-Type: application/json" \
  -d '{
        "title": "EU response curves (v3)",
        "source": {"model_id": "mdl_01jaf3c1d2e3f4g5h6j7k8m9n0", "version": 3,
                   "plot_id": "mmm.response_curves"}
      }'
```

The answer is a `SavedPlot`. Its `embeddable` field tells if you can share the plot with a link
(see [what can be embedded](#what-can-be-embedded)).

You can also save a plot that you wrote (`"spec": {...}`, a `PlotSpec`). The plot must pass the
same sanitizer. If it does not pass, the answer is `422 PLOT_UNSAFE`. The plot must also contain
its data inline. `data_ref` datasets belong to a model version. Thus, to save that type of plot,
use `source`. The platform always records a hand-written plot with `author` provenance. The
value in its `provenance` field has no effect.

- `GET /v1/plots` lists the saved plots of the project (newest first).
- `GET /v1/plots/{plot_id}` returns one plot with its spec.
- `DELETE /v1/plots/{plot_id}` deletes the plot and its embed links.

## Render an image

`POST /v1/plots:render` changes one plot into a file. You can identify the plot by
`saved_plot_id`, by `source` (a plot of a model version) or inline (`plot`):

```bash
curl -s -X POST "$MAPI_URL/v1/plots:render" \
  -H "Authorization: Bearer $MAPI_TOKEN" -H "Content-Type: application/json" \
  -H "Prefer: wait=30" \
  -d '{"saved_plot_id": "plt_01jaf3n1p2q3r4s5t6v7w8x9y0", "format": "pdf", "width": 1200, "height": 600}'
```

| Field | Default | Meaning |
|---|---|---|
| `format` | `png` | `png`, `svg` or `pdf` |
| `scale` | `2` | The pixel density of PNG and PDF (0 < scale ≤ 8). SVG ignores it. |
| `width`, `height` | The size of the plot | The size in CSS pixels before `scale` (16–4096). For a composite plot (facets, concatenations), it is the size of each panel. |

A PNG or a PDF contains a maximum of 50 megapixels. This is the `width x height x scale²` of the
plot after layout, with all panels. If a request is larger, the answer is
`413 PAYLOAD_TOO_LARGE` at the start. Sometimes only the layout shows the size. Then the render
run fails with that code before it draws the plot. If you get this error, make the scale or the
size smaller.

A render is a `render` run in the sandbox. Thus it counts against your quotas, as all small runs
do. You can send `Prefer: wait=N` (a maximum of 60 seconds). If the run finishes in that time,
the answer is `200` with signed links to `renders/<plot>.<format>`. If not, the answer is `202`
with the run. Then poll `GET /v1/plots/renders/{run_id}` until `done` is true. The links expire
after a short time. Download the file. Do not keep the URL.

!!! note "Vega-Lite only, for now"
    vl-convert renders Vega-Lite plots. Plotly plots need kaleido and a Chrome or Chromium in
    the runner image. The images of the platform do not include them. Thus Plotly renders answer
    `422 MODE_NOT_SUPPORTED`, unless the operator enables them (`MAPI_RENDER_PLOTLY_ENABLED`).
    You can export a Plotly plot from the plot viewer.

### Rendering never fetches anything

The renderer gets the sanitized plot and its data as pinned inputs of the run:

- The API writes the plot as the `plots.json` of the run. It pins each bundle dataset of the
  plot (`data_ref`) to the verified object generation, as run inputs with the names
  `plotdata.<n>`.
- The sanitizer refused URL data, `href` encodings, image marks, and Plotly layout images or tile
  maps before. The runner refuses them again before it draws.
- vl-convert runs with no network access (`allowed_base_urls=[]`). Also, the sandbox has no
  egress.

An image has a limit of 32 MiB. The run has a time limit of `MAPI_RENDER_TIMEOUT_S` (5 minutes by
default).

## Embed links

```bash
curl -s -X POST "$MAPI_URL/v1/plots/plt_01jaf3n1p2q3r4s5t6v7w8x9y0/embeds" \
  -H "Authorization: Bearer $MAPI_TOKEN" -H "Content-Type: application/json" \
  -d '{"label": "Q4 board deck", "expires_in_days": 90,
       "allowed_origins": ["https://acme.notion.site"]}'
```

The answer (`PlotEmbedCreated`) is the **only** time that you see the link:

```json
{
  "embed": {"id": "emb_01jaf3p1q2r3s4t5v6w7x8y9z0", "display": "mpe_7k2m9q…", "status": "active", "...": "..."},
  "url": "https://plots.mapi.example/embed/mpe_7k2m9q4x0123456789abcdefghjkmnpqrstvwxyz",
  "token": "mpe_7k2m9q4x0123456789abcdefghjkmnpqrstvwxyz"
}
```

The platform keeps only a keyed hash of the token, as it does for API tokens. Paste the `url`
into the tool that shows the plot, or put it in a frame:

```html
<iframe src="https://plots.mapi.example/embed/mpe_7k2m…" width="900" height="520"
        style="border:0" loading="lazy" title="EU response curves"></iframe>
```

| Option | Default | Meaning |
|---|---|---|
| `expires_in_days` | 30 | 1 to 365 days |
| `label` | none | Your note. The list of links shows it. |
| `allowed_origins` | all sites | The https sites (`https://host[:port]`, a maximum of 10) whose pages can show the plot in a frame. If you open the link directly, it always works. |

- `GET /v1/plots/{plot_id}/embeds` lists the links of a plot. It shows their status (`active`,
  `expired` or `revoked`), the view count and the last view. It never shows their tokens.
- `DELETE /v1/plots/{plot_id}/embeds/{embed_id}` revokes one link. If you revoke a link two
  times, no problem occurs.

The audit log records each new link and each revoked link (`plot.embed_create`,
`plot.embed_revoke`).

### What can be embedded

You can embed only the plots that a model version drew with one of the **platform's own
recipes** (`core.*`, `mmm.*`, `ml.*`, `ts.*`, `choice.*`, `causal.*`). The science pack
(`sci.*`) and the network pack (`net.*`) are not in this list. Thus you cannot embed their plots. Share them
in the project, or render them to an image. The platform refuses plots from a model plugin and plots that a person wrote. The answer is `422 EMBED_NOT_ALLOWED`. This rule
exists because a shared link runs the spec of the plot in the browser of a stranger. Also in the
sandbox, a spec can navigate its own frame. Thus shared views run only specs that the platform
wrote. Share other plots in the project, or render them to an image.

A model version that you fit with uploaded plugin code (a dev overlay or a vetted overlay) can
embed none of its plots. The recipe that a plot names has no effect. The reason: the worker that
writes the plot documents also runs that code. Thus the server decides from the lineage of the
version, not from what a plot tells about itself. Existing links to these plots stop and do not
show them.

An embed also contains its data inline. Thus you cannot embed a dataset that is larger than
1.5 MB.

### The embed page

The link opens a page of the plot sandbox (`/embed/<token>`). The page reads the plot from the
public `GET /v1/embeds/{token}` of the API. It draws the plot with the same safeguards as the
plot frames of the app:

- The request sends no cookie or credential. If the API gets one, the API ignores it.
- The Content-Security-Policy of the page lets it fetch only the `/v1/embeds/` path of the API.
  The API answers cross-origin reads only from the origin of the plot sandbox.
- The browser validates the answer again (inline data only, and the same checks for unsafe
  constructs as the plot frames of the app). The page writes all text as text.
- If a site that is not in `allowed_origins` puts the page in a frame, the page shows a notice
  and not the plot.

If you can start render runs (the `runner` role), the platform renders a PNG of the plot when
you create the link. The page then shows an **Open as PNG** link, for tools that cannot show
interactive content. Editors can also create links, but without that image.

### Errors

| Status | Code | When |
|---|---|---|
| 404 | `NOT_FOUND` | The token does not exist, or a person deleted its plot. |
| 410 | `LINK_EXPIRED` | The link expired, or a person revoked it, or its tenant is suspended. |
| 429 | `RATE_LIMITED` | This link has too many views (`MAPI_EMBED_RATE_PER_MINUTE`), or one address sent too many requests (`MAPI_EMBED_IP_RATE_PER_MINUTE`). Wait for the time in `Retry-After`. |

## Hosting the embed page

The build of the plot sandbox has two pages: `index.html` (the plot frames of the app) and
`embed.html`. The server sends `embed.html` for each `/embed/<token>` path. The embed page needs
the origin of the API:

- In development, the build includes `VITE_API_URL` (default `http://127.0.0.1:8000`).
- The `mapi/web` image builds the page with a placeholder. When nginx serves the page, it puts
  `MAPI_API_URL` in the placeholder. It also adds the headers of the page (CSP with
  `connect-src … $MAPI_API_URL/v1/embeds/`, `frame-ancestors 'self' https:`).

The API allows cross-origin reads of `/v1/embeds/` only from `MAPI_PLOTS_ORIGIN`. Thus that
setting must be the origin from which the server sends the embed page.
