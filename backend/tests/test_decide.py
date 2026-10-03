"""needs_vlm decision table. Spec: DESIGN.md §4.5 `needs_vlm`."""

import pytest

from app.pipeline.decide import SegFlags, needs_vlm

FULL = {"brand": "Nike", "size_label": "EU 42", "price_amount": 4500}


@pytest.mark.parametrize("attrs,flags,policy,provider,expected", [
    (FULL, SegFlags(multi_item=True), "never", "ollama", (False, "policy", [])),
    ({}, SegFlags(), "auto", "off", (False, "policy", [])),
    ({}, SegFlags(repost=True, multi_item=True), "auto", "ollama", (False, "repost", [])),
    ({}, SegFlags(no_shoe=True), "auto", "ollama", (False, "no_shoe", [])),
    (FULL, SegFlags(multi_item=True), "auto", "ollama", (True, "multi_item", [])),
    ({"brand": "Nike"}, SegFlags(multi_item=True), "auto", "ollama", (True, "multi_item", ["size", "price"])),
    (FULL, SegFlags(), "auto", "openrouter", (False, "local_sufficient", [])),
    ({"brand": "Nike", "size_label": "EU 42"}, SegFlags(), "auto", "ollama", (True, "missing:price", ["price"])),
    ({}, SegFlags(), "auto", "ollama", (True, "missing:brand,size,price", ["brand", "size", "price"])),
])
def test_decision_table(attrs, flags, policy, provider, expected):
    d = needs_vlm(attrs, flags, policy, provider)
    assert (d.needed, d.reason, d.missing) == expected


def test_price_on_request_counts_as_price():
    attrs = {"brand": "Nike", "size_label": "EU 42", "price_amount": None, "price_on_request": True}
    assert needs_vlm(attrs, SegFlags(), "auto", "ollama").reason == "local_sufficient"


def test_model_colour_condition_not_required_unless_configured():
    assert not needs_vlm(FULL, SegFlags(), "auto", "ollama").needed
    d = needs_vlm(FULL, SegFlags(), "auto", "ollama", required=["brand", "model"])
    assert d.needed and d.missing == ["model"]
