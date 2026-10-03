"""create_post: the one writer of posts, post_messages and images rows. Spec: DESIGN.md §4.3."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import psycopg
from psycopg.types.json import Jsonb

PRIORITY_BY_SOURCE = {"whatsapp": 0, "demo_upload": 0, "chat_export": 10, "demo_seed": 20}


class OverlapConflict(Exception):
    """Another post already owns some of these image messages; the caller re-filters and retries."""


@dataclass(frozen=True)
class ImageInput:
    sha256: bytes
    phash: int
    width: int
    height: int
    bytes: int


@dataclass(frozen=True)
class MsgInput:
    msg_key: str
    kind: Literal["image", "text"]
    sent_at: datetime
    caption: str | None = None
    image: ImageInput | None = None
    missing_media: bool = False
    reject_reason: str | None = None


@dataclass(frozen=True)
class PostInput:
    source: str
    idempotency_key: str
    chat_ref: str
    sender_ref: str
    messages: list[MsgInput]
    sender_jid: str | None = None
    vlm_policy: str = "auto"
    seed_attrs: dict | None = None
    owner_session: str | None = None


@dataclass(frozen=True)
class CreateResult:
    status: Literal["created", "duplicate"]
    post_id: int
    image_ids: list[int] = field(default_factory=list)


def create_post(conn: psycopg.Connection, p: PostInput) -> CreateResult:
    """One transaction (a savepoint if the caller already opened one)."""
    sent = [m.sent_at for m in p.messages]
    with conn.transaction():
        row = conn.execute(
            """INSERT INTO posts (source, idempotency_key, chat_ref, sender_ref, sender_jid,
                                  first_msg_at, last_msg_at, priority, vlm_policy, seed_attrs, owner_session)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (idempotency_key) DO NOTHING
               RETURNING id""",
            (p.source, p.idempotency_key, p.chat_ref, p.sender_ref, p.sender_jid, min(sent), max(sent),
             PRIORITY_BY_SOURCE[p.source], p.vlm_policy,
             Jsonb(p.seed_attrs) if p.seed_attrs is not None else None, p.owner_session),
        ).fetchone()
        if row is None:
            existing = conn.execute(
                "SELECT id FROM posts WHERE idempotency_key = %s", (p.idempotency_key,)).fetchone()
            return CreateResult("duplicate", existing[0])
        post_id = row[0]

        image_ids: list[int] = []
        image_id_by_msg: dict[int, int] = {}
        for i, m in enumerate(p.messages):
            if m.image is None:
                continue
            img = m.image
            image_id = conn.execute(
                """INSERT INTO images (post_id, seq, sha256, phash, width, height, bytes)
                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (post_id, len(image_ids), img.sha256, img.phash, img.width, img.height, img.bytes),
            ).fetchone()[0]
            image_ids.append(image_id)
            image_id_by_msg[i] = image_id

        inserted: set[str] = set()
        for i, m in enumerate(p.messages):
            r = conn.execute(
                """INSERT INTO post_messages (post_id, chat_ref, msg_key, seq, kind, sent_at, caption,
                                              image_id, missing_media, reject_reason)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (chat_ref, msg_key) DO NOTHING
                   RETURNING msg_key""",
                (post_id, p.chat_ref, m.msg_key, i, m.kind, m.sent_at, m.caption,
                 image_id_by_msg.get(i), m.missing_media, m.reject_reason),
            ).fetchone()
            if r is not None:
                inserted.add(r[0])
        if any(m.kind == "image" and m.msg_key not in inserted for m in p.messages):
            raise OverlapConflict(post_id)
    return CreateResult("created", post_id, image_ids)
