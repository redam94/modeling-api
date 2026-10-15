import mapi_plots
import pytest


def test_import() -> None:
    assert mapi_plots.__version__


def test_lazy_exports_resolve() -> None:
    pytest.importorskip("altair")
    for name in mapi_plots.__all__:
        assert getattr(mapi_plots, name) is not None, name
    assert set(dir(mapi_plots)) == set(mapi_plots.__all__)
    with pytest.raises(AttributeError):
        _ = mapi_plots.nope
