"""Guided templates stay readable by a client when a newer server adds a family."""

from __future__ import annotations

import pytest
from mapi_spec.api import Page
from mapi_spec.guided import GUIDED_FAMILIES, GUIDED_TEMPLATES, GuidedTemplate
from pydantic import ValidationError

FIRST = next(iter(GUIDED_TEMPLATES.values()))


def _wire(family: object) -> dict[str, object]:
    raw = FIRST.model_dump(mode="json")
    return {**raw, "family": family}


def test_an_unknown_family_is_kept_as_text() -> None:
    """Regression: v0.12 parsers rejected the whole template list when v0.13 added "causal"
    (a closed Literal); this version keeps a family it does not know."""
    page = Page[GuidedTemplate].model_validate(
        {"items": [_wire("survival"), FIRST.model_dump(mode="json")]}
    )
    assert [t.family for t in page.items] == ["survival", FIRST.family]


@pytest.mark.parametrize("family", ["", "Not A Family", 3, None])
def test_a_malformed_family_is_still_rejected(family: object) -> None:
    with pytest.raises(ValidationError):
        GuidedTemplate.model_validate(_wire(family))


def test_built_in_templates_use_known_families() -> None:
    assert {t.family for t in GUIDED_TEMPLATES.values()} <= GUIDED_FAMILIES
    assert "causal" in GUIDED_FAMILIES
