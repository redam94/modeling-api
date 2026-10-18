"""E77 data: ``response(speaker, chip)`` term labels simulated from the hierarchical naming
model on a WCS-like palette (truth in ``NAMING_TRUTH``).

``tiny`` is 12 speakers on a 38-chip palette (4 lightness rows of 8 hues plus 6 greys);
``full`` is the WCS geometry (8 rows of 40 hues plus 10 greys = 330 chips) with 25 speakers,
about the size of one WCS language.
"""

from __future__ import annotations

import xarray as xr
from mapi_plugin_science.colour_naming_data import NAMING_SCHEMA, naming_dataset
from mapi_spec.schema import DataSchema

SIZES = {"tiny": (12, 4, 8), "full": (25, 8, 40)}
"""speakers, lightness rows, hue columns"""


def generate(seed: int = 0, size: str = "tiny") -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    speakers, rows, hues = SIZES[size]
    return naming_dataset(speakers=speakers, rows=rows, hues=hues, seed=seed), {}


def schema() -> DataSchema:
    return NAMING_SCHEMA


def payload_new_speaker(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """The chips of one fitted speaker and of one speaker the model never saw."""
    first = str(data["speaker"].values[0])
    return data.isel(speaker=[0, 0]).assign_coords(speaker=[first, "visitor"])


def payload_new_chips(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """Two chips that are not on the fitted palette."""
    top = int(data["chip"].values.max())
    return data.isel(speaker=[0], chip=[0, 1]).assign_coords(chip=[top + 1, top + 2])
