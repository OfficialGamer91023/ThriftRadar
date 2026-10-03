"""When the VLM is called. Spec: DESIGN.md §4.5 `needs_vlm`. Pure."""

from dataclasses import dataclass, field

REQUIRED = ("brand", "size", "price")


@dataclass(frozen=True)
class SegFlags:
    repost: bool = False
    no_shoe: bool = False
    multi_item: bool = False


@dataclass(frozen=True)
class Decision:
    needed: bool
    reason: str
    missing: list[str] = field(default_factory=list)


def have_fields(attrs: dict) -> set[str]:
    """Fields present in a listing's attributes, whatever their source. Price on request counts as a price."""
    have = set()
    if attrs.get("brand"):
        have.add("brand")
    if attrs.get("size_label"):
        have.add("size")
    if attrs.get("price_amount") is not None or attrs.get("price_on_request"):
        have.add("price")
    for f in ("model", "colour", "condition"):
        if attrs.get(f):
            have.add(f)
    return have


def needs_vlm(attrs: dict, flags: SegFlags, policy: str, provider: str,
              required: tuple[str, ...] | list[str] = REQUIRED) -> Decision:
    if policy == "never" or provider == "off":
        return Decision(False, "policy")
    if flags.repost:
        return Decision(False, "repost")
    if flags.no_shoe:
        return Decision(False, "no_shoe")
    missing = [f for f in required if f not in have_fields(attrs)]
    if flags.multi_item:
        return Decision(True, "multi_item", missing)
    if not missing:
        return Decision(False, "local_sufficient")
    return Decision(True, "missing:" + ",".join(missing), missing)
