#!/usr/bin/env python3
"""Write an ``EmbedView`` of a real platform recipe, for the embed-page screenshot.

``make docs-screenshots-tutorials`` serves this JSON as the API's ``GET /v1/embeds/{token}``
answer (Playwright intercepts the request), so the screenshot shows exactly what the plot
sandbox's embed page draws for a shared ``mmm.response_curves`` plot::

    uv run python scripts/docs_embed_view.py .local/embed-view.json
"""

from __future__ import annotations

import sys
from pathlib import Path

from mapi_plots.examples import example_inputs
from mapi_plots.recipes import get_recipe
from mapi_spec.sharing import EmbedView

RECIPE = "mmm.response_curves"


def main(out: str) -> int:
    data, options = example_inputs()[RECIPE]
    plot = get_recipe(RECIPE).render(data, **options)
    view = EmbedView(
        title="EU response curves (v3)",
        caption_plain=plot.caption_plain,
        plot=plot,
        generated_at="2026-10-15T09:40:00Z",
        expires_at="2026-11-14T09:40:00Z",
        allowed_origins=[],
        image_url="https://storage.googleapis.com/mapi-prod-bundles/t/acme/embeds/plot.png?X-Goog-Signature=x",
    )
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(view.model_dump_json(exclude_none=True), "utf-8")
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ".local/embed-view.json"))
