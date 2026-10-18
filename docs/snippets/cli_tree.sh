#!/usr/bin/env bash
# Tree datasets and multi-file versions with the CLI (v0.8). Included by docs/guides/tree-data.md.
# conformance runs docs/snippets/tree_data.py, which checks that each command and option exists.

# --8<-- [start:concat]
# Two files, one for each market, stacked along a new 'market' dim
mapi datasets upload sales_nl.nc sales_de.nc --name sales --concat market=NL,DE
# --8<-- [end:concat]

# --8<-- [start:merge]
# Two files on the same markets and weeks: merge their variables, keep the shared labels
mapi datasets upload sales.nc weather.nc --name sales_weather --merge --join inner
# --8<-- [end:merge]

# --8<-- [start:node]
# One file for each node: the image at the root, the PSF file under /instrument
mapi datasets upload image.nc psf.nc --name lens --node / --node /instrument
# --8<-- [end:node]

# --8<-- [start:hdf5]
# 1. Upload the raw HDF5 file without an ingest, and show its groups and arrays
mapi datasets inspect lens lens_raw.h5
# 2. Write a mapping for the arrays (mapping.json), then ingest the staged upload
mapi datasets ingest lens upl_01jaf3d1e2f3g4h5j6k7m8n9p0 --mapping mapping.json
# Or do the two steps in one: upload and ingest with the mapping
mapi datasets upload lens_raw.h5 --name lens --mapping mapping.json
# --8<-- [end:hdf5]
