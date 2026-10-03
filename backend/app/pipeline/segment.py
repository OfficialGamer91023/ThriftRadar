"""Split a post into item segments by caption. Spec: DESIGN.md §4.2 `segment_post`."""

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class SegMsg:
    seq: int
    kind: Literal["image", "text"]
    caption: str | None = None
    image_id: int | None = None  # None when the media is missing


@dataclass
class Segment:
    idx: int
    image_ids: list[int] = field(default_factory=list)
    caption: str | None = None


def _is_captioned(caption: str | None) -> bool:
    return bool(caption) and any(ch.isalnum() for ch in caption)


def _norm(caption: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", caption)).strip().casefold()


def segment_post(messages: list[SegMsg]) -> tuple[list[Segment], str]:
    ordered = sorted(messages, key=lambda m: m.seq)
    images = [m for m in ordered if m.kind == "image"]
    extra = [m.caption.strip() for m in ordered if m.kind == "text" and m.caption and m.caption.strip()]

    # Runs of captioned images; consecutive *among captioned images* with the same caption merge.
    run_starts: list[int] = []  # index into `images` where a new run starts
    last_norm: str | None = None
    for i, m in enumerate(images):
        if _is_captioned(m.caption):
            n = _norm(m.caption)
            if n != last_norm:
                run_starts.append(i)
                last_norm = n

    if len(run_starts) <= 1:
        caption = images[run_starts[0]].caption.strip() if run_starts else None
        segments = [Segment(0, [m.image_id for m in images if m.image_id is not None], caption)]
    else:
        segments = []
        bounds = run_starts + [len(images)]
        for r, (start, end) in enumerate(zip(bounds, bounds[1:])):
            members = images[(0 if r == 0 else start):end]  # leading uncaptioned photos join segment 0
            segments.append(Segment(r, [m.image_id for m in members if m.image_id is not None],
                                    images[start].caption.strip()))

    kept: list[Segment] = []
    for seg in segments:
        if seg.image_ids:
            seg.idx = len(kept)
            kept.append(seg)
        elif seg.caption:
            extra.append(seg.caption)
    return kept, "\n".join(extra)
