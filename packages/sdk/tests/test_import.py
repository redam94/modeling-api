"""The public API is importable from the package root."""

import mapi_sdk


def test_import_and_public_api() -> None:
    assert mapi_sdk.__version__
    for name in mapi_sdk.__all__:
        assert hasattr(mapi_sdk, name), name
