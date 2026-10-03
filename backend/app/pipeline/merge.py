"""Merge local attributes with a VLM item. Spec: DESIGN.md §4.5 `merge_attributes`. Pure.

Field precedence: the first source in each list that has a value wins; attr_sources records the winner.
"""

PRECEDENCE: dict[str, tuple[str, ...]] = {
    "price": ("caption", "seed", "vlm"),
    "size": ("caption", "seed", "ocr", "vlm"),
    "brand": ("caption", "seed", "ocr", "vlm", "siglip", "knn"),
    "model": ("seed", "vlm", "caption"),
    "colour": ("seed", "vlm", "caption"),
    "gender": ("seed", "vlm", "caption"),
    "condition": ("caption", "seed", "vlm"),
}
# Listing columns that belong to each attr_sources group.
GROUPS: dict[str, tuple[str, ...]] = {
    "price": ("price_amount", "currency", "price_on_request"),
    "size": ("size_label", "size_eu", "size_approx"),
    "brand": ("brand",), "model": ("model",), "colour": ("colour",), "gender": ("gender",),
    "condition": ("condition",),
}


def _present(group: str, attrs: dict) -> bool:
    if group == "price":
        return attrs.get("price_amount") is not None or bool(attrs.get("price_on_request"))
    if group == "size":
        return attrs.get("size_label") is not None
    return bool(attrs.get(group))


def _rank(group: str, source: str | None) -> int:
    order = PRECEDENCE[group]
    return order.index(source) if source in order else len(order)


def merge_attributes(local: dict, sources: dict, vlm: dict) -> tuple[dict, dict]:
    """-> (attrs, attr_sources). A VLM null never overwrites anything."""
    attrs, srcs = dict(local), dict(sources)
    for group, cols in GROUPS.items():
        if not _present(group, vlm):
            continue
        have = _present(group, attrs)
        if have and _rank(group, srcs.get(group)) <= _rank(group, "vlm"):
            continue
        for c in cols:
            if c in vlm:
                attrs[c] = vlm[c]
        if group == "price" and not attrs.get("currency"):
            attrs["currency"] = local.get("currency")
        srcs[group] = "vlm"
    return attrs, srcs
