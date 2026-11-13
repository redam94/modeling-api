---
description: For contributors - how the look of the docs and the web app is built, the building blocks for a page with one example each, how to add a page to the navigation and the search, how to add generated art and how to refresh the vendored files.
---

# The docs theme <span class="mapi-badge">v0.21</span>

This page is for contributors who write or change a docs page. It tells you where the look
comes from, which blocks you can use, and which tests fail when a rule is broken.

## The rule

Chrome is monochrome. Colour is for data.

- Chrome is all that is not content: the header, the navigation, buttons, links and tiles.
  It is ink on the background.
- Colour has a fixed meaning: orange is new or needs attention, blue is information, green is
  good and red is danger.
- Depth comes from two surfaces (the background and one grey), not from borders or shadows.

| Part | Where it is |
|---|---|
| Tokens of the docs (`--mono-*`, light and dark) | `docs/stylesheets/mono.css` |
| Header, tabs, search dialog, sidebars, footer | `docs/stylesheets/mono-chrome.css` |
| What Markdown makes: text, code, tables, admonitions | `docs/stylesheets/mono-content.css` |
| The building blocks of this page | `docs/stylesheets/mono-blocks.css` |
| Templates | `overrides/main.html`, `overrides/home.html` and `overrides/partials/` |
| The script (search dialog, section labels, scroll reveal) | `docs/javascripts/mono.js` |
| Tokens of the web app (the same values, with the names of shadcn) | `apps/web/src/app.css` |

Do not write a colour value on a page. Use a block, or add a token.

## Quick path: add a page

1. Write the page in `docs/`. Start it with one `# Title`. The theme moves this title into
   the head of the page, above the breadcrumbs.
2. Add the page to `nav:` in `mkdocs.yml`, in the tab where a reader looks for it. The file
   can stay in its folder. A test fails when a page is not in the nav.
3. Run the fast tests and the build:

    ```bash
    uv run pytest -q conformance/tests/test_docs_nav.py conformance/tests/test_docs_theme.py
    make docs-build
    uv run python scripts/gen_docs_search_index.py
    ```

The last command writes the docs index of the web app again. Run it after each change to the
nav, to a page title or to a second-level heading.

## Building blocks

Use the blocks from Markdown. The extensions `md_in_html` and `attr_list` are on. The
examples are for a page in a folder of `docs/`, thus the paths start with `../`.

| Block | Use it for |
|---|---|
| `mono-band`, `mono-wrap` | A full-width section and its column, on a page with `template: home.html` |
| `mono-kicker`, `mono-display`, `mono-headline`, `mono-lead` | The head of a band |
| `mono-tiles`, `mono-icon` | A grid of tiles, each with an icon and one link |
| `mono-actions` | A row with one button and text links |
| `mono-figures` | A row of large numbers |
| `mono-steps` | A numbered sequence (only for steps in a real sequence) |
| `mono-links` | Stacked link rows, one line for each page |
| `mono-chip` | A small label |
| `data-reveal` | A block that fades in when it scrolls into view |

### A band with its head

A page with `template: home.html` in its front matter has no sidebars and no width limit.
Each top-level block of such a page must be a band.

```markdown
<section class="mono-band mono-band--alt mono-band--tight tex-contours" markdown>
<div class="mono-wrap mono-center" markdown>

<p class="mono-kicker">Quick links</p>

## Do a task. { .mono-headline data-reveal }

Each task has one page. **The bold words are ink.**
{ .mono-lead }

</div>
</section>
```

| Class | Effect |
|---|---|
| `mono-band--alt` | The grey surface |
| `mono-band--dark` | Dark in the two schemes |
| `mono-band--tight` | Less space above and below |
| `mono-band--hero` with `mono-hero--home`, `--learn`, `--guides`, `--extend`, `--reference` or `--operate` | A centred head with a backdrop. Add `mono-band--left` for a head at the left |
| `tex-contours`, `tex-lattice`, `tex-curves` | A faint corner texture, in the dark scheme only |
| `mono-wrap--narrow` | A 764 px column for running text |
| `mono-headline--s` | The smaller headline |

The page headline is `# Title { .mono-display }`. Use it one time on a page.

### Tiles

Write tiles as a Markdown list. The build checks the links. The link with `mono-more` is at
the bottom of the tile and makes the full tile a link.

```markdown
<div class="mono-tiles mono-tiles--3" markdown>

-   ![](../assets/brand/icons/guides.webp){ .mono-icon }

    ### Read a guide

    One or two sentences.

    [Open the guides](../guides/index.md){ .mono-more }

</div>
```

| Class | Effect |
|---|---|
| (none) | Two tiles in a row |
| `mono-tiles--3`, `mono-tiles--4` | Three or four tiles in a row |
| `mono-tiles--7-5` | Rows of a wide and a narrow tile |
| `mono-tiles--compact` | Small tiles for a list of tasks |
| `mono-tiles--routes` | A tile with two or three links. No link fills the tile |

Do not write a tile as a link element in Markdown. The build adds a link to each heading, and
a link in a link breaks the tile.

### Actions

```markdown
<div class="mono-actions" markdown>
[Start the tutorial](../guides/guided-tutorial.md){ .md-button .md-button--primary }
[Read the quickstart](../getting-started/quickstart.md){ .mono-more }
</div>
```

### Figures

Each number needs a label that links the page that proves it.

```markdown
<div class="mono-figures" markdown>
<div markdown>
<p class="mono-figure-num is-orange">57</p>

Plot recipes, in the [plot recipes catalog](../reference/generated/plots-catalog.md).
</div>
</div>
```

The classes `is-orange`, `is-blue`, `is-green` and `is-red` give a number its meaning.

### Steps

```markdown
<div class="mono-steps" markdown>

1.  ### Upload your file

    One or two sentences.

2.  ### Fit the model

    One or two sentences.

</div>
```

The class `mono-tour` puts a `mono-steps` block and a block of at most two screenshots in
two columns. The home page uses it.

### Link rows

In a row, the bold text is the title, the plain text is one line of description and the
italic text is a short label at the right.

```markdown
<div class="mono-links" markdown>

- [**Spec format** The parts of a spec, with examples. *Reference*](../reference/spec-format.md)

</div>
```

### Chips and badges

```markdown
<span class="mono-chip is-blue">Read only</span>
<span class="mapi-badge">v0.21</span>
```

A `mapi-badge` in a heading shows on the page but not in the title of a search result.

## Navigation and search

| You want to | Change this |
|---|---|
| Put a page in a tab | `nav:` in `mkdocs.yml`. The tab of a result and the art of the page head come from the nav |
| Add a release page | Add the page `whats-new-<version>.md` as the first entry of Home, What's new. Link it from the What's new band of `docs/index.md`. Old release pages go down in the search with no edit |
| Make a task page rank first | Add it to `TASK_PAGES` in `scripts/mkdocs_hooks.py` and link it from `docs/index.md`. A test fails when the home page does not link it |
| Rank one page up or down | The other lists in `scripts/mkdocs_hooks.py`, or `search: {boost: 2}` in the front matter of the page. The front matter wins |
| Keep a page out of the search | `SEARCH_EXCLUDE` in `scripts/mkdocs_hooks.py` |
| Change the list for an empty search field | `extra.search_popular` in `mkdocs.yml`: 6 to 8 entries with `title`, `url` (a site path that ends in `/`) and `hint` |
| Add a top-level folder to `docs/` | `SECTION_LABELS` in `docs/javascripts/mono.js` and `mono_sections` in `overrides/main.html`. The two maps must name the same folders |

The search gives each page a rank from its path:

| Rank | Pages |
|---|---|
| 4 | The sign-in guide |
| 2 | The home page, the landing pages and the pages in `TASK_PAGES` |
| 1 | All other pages |
| 0.5 | The generated catalogs and the science tutorials |
| 0.2 | The Python API pages, the roadmap, the verification records, this page and all release pages but the newest two |

The web app has its own small index of the docs
(`apps/web/src/features/search/docs-index.gen.json`). The script
`scripts/gen_docs_search_index.py` makes it from the nav, the page titles and the
second-level headings. `make web-check` fails when the file is not current.

## Generated art

The backdrops and the icons were generated for this project. They are in
`docs/assets/brand/` and `docs/assets/brand/icons/`. The art of the web app is in
`apps/web/src/assets/brand/`.

**Style rules** for a new image:

- A pure black ground, to each edge. No frame and no margin.
- Thin line-work in near-white.
- One orange element and one blue element.
- No text, no letters, no digits and no logo.

**Size.** Save the file as WebP.

| Kind | Pixels | Size |
|---|---|---|
| Hero backdrop (`hero-<name>.webp`) | 2400 x 1018, with an empty black centre | At most 120 KB |
| Corner texture (`tex-<name>.webp`) | 1920 x 1072, art in one corner only | The files are 23 KB to 39 KB |
| Tile icon (`icons/<name>.webp`) | 192 x 192, shown at 56 px | The files are 2 KB to 5 KB |

**Provenance.** Add one row for each new file to the `PROVENANCE.md` beside it
(`docs/assets/brand/PROVENANCE.md` or `docs/assets/brand/icons/PROVENANCE.md`): the file,
the model endpoint, the full prompt, the request id and the date. Write down each change that
you made after the generation, for example a crop or a blur.

**Theme treatment.** One file serves the two schemes. In the dark scheme the black ground
disappears (`mix-blend-mode: screen`). In the light scheme the theme inverts the image and
blends it into the page, so the lines are dark on white. A hero backdrop and a texture show
only in the dark scheme or in a `mono-band--dark` band. Thus, look at a new image in the two
schemes before you commit it.

**Never hotlink.** Commit the file. A page must not load an image, a font or a script from a
different host. For a new backdrop or texture, add the file name to `BRAND_MANIFEST` in
`conformance/tests/test_docs_theme.py`.

## Vendored files

The docs site loads no file from another host. Three sets of files are copied into the
repository, each with its licence text beside it.

| Files | Licence | How to refresh |
|---|---|---|
| mermaid, in `docs/assets/vendor/mermaid/` | MIT | Change `VERSION` and `INTEGRITY` in `scripts/vendor_mermaid.py`, then run `uv run python scripts/vendor_mermaid.py --update-hash` and read the diff. `--check` verifies the committed file |
| Inter and JetBrains Mono, in `docs/assets/fonts/` | OFL-1.1 | Copy the eight `latin` and `latin-ext` variable `woff2` files from the packages `@fontsource-variable/inter` and `@fontsource-variable/jetbrains-mono`. Write the new version in `docs/assets/fonts/LICENSE.txt`. Each file must have an `@font-face` rule in `docs/stylesheets/mono.css` |
| Redoc, in `docs/assets/vendor/redoc/` | MIT | `scripts/vendor_redoc.py`, in the same way as mermaid |

`overrides/main.html` loads mermaid only on a page that has a diagram, before the script of
the theme. This needs full page loads. Do not turn on `navigation.instant`.

## The tests that guard this

| Test file | What it checks |
|---|---|
| `conformance/tests/test_docs_theme.py` | No other host in the theme, the fonts and their licence, the pinned mermaid file, the hooks that the theme script needs, a rule for each block, the art manifest, the centred tabs |
| `conformance/tests/test_docs_nav.py` | Six tabs, each page in the nav one time, the landing pages, the home page, the search ranks and the search separator |
| `conformance/tests/test_docs_search_index.py` | The docs index of the web app is current and small |
| `conformance/tests/test_docs_v021.py` | The release page, the labels that pages show as controls, and this page |
| `apps/web/src/theme-contrast.test.ts`, `apps/web/src/theme-tokens.test.ts`, `apps/web/src/theme-series-parity.test.ts` | The tokens of the web app: contrast in the two themes and the same series colours in the app, the plot sandbox and `mapi_plots.palette` |

`make docs-test` runs the four Python files with the other docs tests. `make docs-build`
builds the site in strict mode.

## Checklist

- [ ] The page starts with one `# Title` and has a `description` in its front matter.
- [ ] The page is in the nav, in the tab where a reader looks for it.
- [ ] The page uses blocks and tokens, and no colour value.
- [ ] Each image is a committed file. A new art file has its row in `PROVENANCE.md`.
- [ ] You looked at the page in the light and the dark scheme, and at 390 px wide.
- [ ] `make docs-test` and `make docs-build` pass.
- [ ] The docs index of the web app is current.

The rules for the words are in [Writing style](writing-style.md). The licences of the fonts,
the scripts and the art are in [Licensing](licensing.md#the-docs-site-and-the-art).
