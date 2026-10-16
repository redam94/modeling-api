import tomllib
from pathlib import Path

import mapi_server

ROOT = Path(__file__).resolve().parents[3]

# The control plane never imports plugin code, the runtime SDK or array/columnar libraries
# (BUILD-PROGRAM import contract; the server image asserts the same dists absent).
CONTROL_PLANE_FORBIDDEN = {
    "mapi_sdk",
    "mapi_runner",
    "mapi_pymc",
    "mapi_sklearn",
    "mapi_steps",
    "mapi_plugin_mmm",
    "mapi_plugin_ts",
    "mapi_plugin_tabular",
    "mapi_plugin_statsmodels",
    "statsmodels",
    "mapi_plots.recipes",
    "mapi_plots.packs",
    "numpy",
    "xarray",
    "pandas",
    "pyarrow",
    "zarr",
}


def test_import() -> None:
    assert mapi_server.__version__


def test_import_contract_forbids_plugins_and_array_libraries() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["importlinter"]
    contracts = [c for c in config["contracts"] if c.get("source_modules") == ["mapi_server"]]
    assert len(contracts) == 1, "exactly one forbidden contract for mapi_server"
    contract = contracts[0]
    assert contract["type"] == "forbidden"
    missing = CONTROL_PLANE_FORBIDDEN - set(contract["forbidden_modules"])
    assert not missing, f"mapi_server's import contract does not forbid {sorted(missing)}"
