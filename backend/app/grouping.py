"""Python twin of the listener's AlbumBuffer join rule. Spec: DESIGN.md §4.2 `group_messages`, §4.1 `AlbumBuffer`.

Both implementations are tested against shared/grouping_cases.json.
"""

from dataclasses import dataclass, field
from typing import Literal

MB = 1024 * 1024


@dataclass(frozen=True)
class RawMsg:
    key: str
    chat: str
    sender: str
    ts: float  # seconds; the importer passes epoch seconds
    kind: Literal["image", "text"]
    caption: str | None = None
    bytes: int = 0
    missing: bool = False
    extra: dict = field(default_factory=dict, compare=False)  # importer-specific data, passed through


@dataclass
class Group:
    chat: str
    sender: str
    messages: list[RawMsg]
    first_ts: float
    last_ts: float
    images: int = 0
    total_bytes: int = 0
    order: int = 0  # arrival index of the first message

    @property
    def has_usable_image(self) -> bool:
        return any(m.kind == "image" and not m.missing for m in self.messages)


def group_messages(
    msgs: list[RawMsg],
    idle_s: float = 60,
    max_span_s: float = 180,
    max_images: int = 30,
    max_bytes: int = 60 * MB,
    stats: dict | None = None,
) -> list[Group]:
    """Group an arrival-ordered list. Pure apart from filling `stats`."""
    open_groups: dict[tuple[str, str], Group] = {}
    closed: list[Group] = []

    for i, m in enumerate(msgs):
        k = (m.chat, m.sender)
        g = open_groups.get(k)
        if g is not None and any(x.key == m.key for x in g.messages):
            continue
        if g is not None:
            joins = (
                m.ts - g.last_ts <= idle_s
                and m.ts - g.first_ts <= max_span_s
                and (m.kind != "image" or (g.images < max_images and g.total_bytes + m.bytes <= max_bytes))
            )
            if not joins:
                closed.append(open_groups.pop(k))
                g = None
        if g is None:
            g = Group(m.chat, m.sender, [], first_ts=m.ts, last_ts=m.ts, order=i)
            open_groups[k] = g
        g.messages.append(m)
        g.last_ts = max(g.last_ts, m.ts)
        if m.kind == "image":
            g.images += 1
            g.total_bytes += m.bytes

    closed.extend(open_groups.values())
    closed.sort(key=lambda g: g.order)
    if stats is not None:
        for g in closed:
            if not g.has_usable_image:
                key = "dropped_all_missing" if any(m.kind == "image" for m in g.messages) else "dropped_text_only"
                stats[key] = stats.get(key, 0) + 1
    return [g for g in closed if g.has_usable_image]
