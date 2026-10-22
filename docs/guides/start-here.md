---
description: "Start here: the welcome tour, your first-steps checklist, the one-click sample project and the playgrounds of modeling-api - and how to replay or turn off the tips."
---

# Start here <span class="mapi-badge">v0.18</span>

New to modeling-api? The app has a few things to help you find your feet. None of them gets
in your way, and you can turn them all off.

- **A welcome** on your first visit, with a two-minute tour.
- **A checklist** of first steps, which ticks itself as you go.
- **A sample project**: a real model, fitted for you on example data.
- **Playgrounds**: sliders and settings to play with, with nothing to break.

## The welcome and the tours

On your first visit to an organisation, a small welcome card asks if you'd like a quick look
around. Pick **Take the 2-minute tour**, **Explore the sample project**, or **Not now**. The
card never blocks the page: click anywhere else and carry on.

A tour points at one part of the page at a time, with a short note. Use **Next** (or → and
Enter), **Back** (or ←) and **Skip the tour** (or Esc). If a part is not on the screen, the
tour says so and offers to take you there.

| Tour | What it shows | Link |
|---|---|---|
| A two-minute look around | Start here, the checklist, the playground, the navigation, Make your own and help | `?tour=welcome` |
| Bring in data | Templates, the upload and the column mapping in Guided mode | `?tour=data` |
| Fit a model | The sample's fit, its progress and its chains | `?tour=fit` |
| Read the results | The headline, the scorecard and the plots of a report | `?tour=results` |
| Ask what if | The sliders, the answer with its range and the exact result | `?tour=whatif` |
| Share a dashboard | Presets, panels, publish and share | `?tour=dashboard` |
| Make your own | The catalog, a chart playground, Add your own and your first model recipe | `?tour=make_your_own` |

Replay a tour from your user menu, or add `?tour=<id>` to any page's address. Some tours need
the sample project; they tell you if it is not there yet. `?tour=off` keeps tours from
starting by themselves during that visit.

## Your checklist

The checklist on Home (and as a strip on the Guided page) has eight first steps: take the
tour, open the sample project, fit a model, read the results, move a budget slider, make a
dashboard, bring your own data, and make your first model recipe. Each step ticks itself when
you do it, anywhere in the app.

Your progress is stored on the server, for you in this organisation. It follows you to any
browser, and nobody else sees it change. A finished step stays finished. When all are done,
or when you hide it, Start here becomes a one-line banner.

**Settings › Tips and tours** turns tips off, and **Reset tips** starts the checklist, the
tours and the welcome card again.

## The sample project

The sample project is a real marketing-mix model on two years of weekly revenue and TV,
search and social spend in four regions (our synthetic data). The platform fits it for your
organisation the first time somebody asks, in about a minute or two. Then the report, the
what-if sliders, dashboards and sharing all work on it, the same as on your own data.

- **Make it:** the sample card on Home (or the welcome card). This needs the editor role in
  some project, because the fit uses compute. The card shows the stages: set up, load data,
  fit.
- **Join it:** when the sample exists, anybody in the organisation opens it from the same
  card. They join the sample project with a limited role; nothing is fitted again.
- **If it fails:** the card says why. The person who made it, or a tenant admin, can try
  again (up to three attempts in all).
- **Hide or retire it:** **Hide it** hides the card for you. A tenant admin can retire the sample:
  it disappears for everyone and the extra memberships go. Nothing is deleted.

The sample also comes with one model recipe, one prep preset and one chart template, so
**Catalog › Yours** and **Make your own** have something in them from the start.

Operators turn samples off for the whole platform with `MAPI_SAMPLES_ENABLED=false`.

## The playgrounds

- **What if, on Home and in the Catalog.** Pick a channel and slide its spend: the answer and
  its range change at once. This is example data, worked out in your browser, so nothing is
  sent anywhere. **Open the real model** takes you to the sample project.
- **Try it, for a chart.** In **Catalog › Plots**, **Try it** on a card opens the recipe
  playground: the chart's settings form next to its example. The title and the size change at
  once. Other settings draw the chart again on the server, which takes about ten seconds;
  settings you tried before come back quickly from a cache. **Copy as YAML** gives you the
  snippet to put the chart into a spec (`outputs.plots`) or a workflow (`plot.render`).
  Where live previews are off (on Cloud Run, by default), you change the settings and pick
  **Apply**.
- **Explore, on the sample.** The Guided report's explore step is a full what-if on the
  sample's real model.

## For operators

| Setting | Default | What it does |
|---|---|---|
| `MAPI_SAMPLES_ENABLED` | `true` | Offer the sample project (off: no sample anywhere) |
| `MAPI_SAMPLE_MAX_ATTEMPTS` | `3` | Attempts one sample may take, the first included |
| `MAPI_PLOT_PREVIEW_ENABLED` | `true` (off with the Cloud Run executor) | Live recipe previews in the playground |
| `MAPI_PLOT_PREVIEW_PER_MINUTE` | `20` | Preview runs one user may start in a minute |
| `MAPI_PLOT_PREVIEW_PER_DAY` | `200` | Preview runs one tenant may start in 24 hours |

All the settings are in the [server settings reference](../reference/generated/server-settings.md).
The e2e test setup writes `mapi.noTour=1` into the browser's local storage, so tours never
start by themselves in browser tests. It is a convenience, not a security control.

## Where next

- [Your first MMM in Guided mode](guided-tutorial.md)
- [Your first extensions](../tutorials/your-first-extensions.md): your own model, step, chart
  and workflow.
