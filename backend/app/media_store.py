"""Content-addressed media stores keyed by sha256 of the normalized JPEG. Spec: DESIGN.md §4.3."""

import os
import uuid
from pathlib import Path
from typing import Protocol

from psycopg_pool import ConnectionPool

from app import db
from app.images import normalize_image


class MediaMissing(Exception):
    pass


class ReadOnlyStore(Exception):
    pass


class MediaStore(Protocol):
    writable: bool

    def put(self, sha: bytes, data: bytes) -> None: ...

    def get(self, sha: bytes) -> bytes: ...


class LocalDirStore:
    writable = True

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def path(self, sha: bytes) -> Path:
        h = sha.hex()
        return self.root / h[:2] / f"{h}.jpg"

    def put(self, sha: bytes, data: bytes) -> None:
        dest = self.path(sha)
        if dest.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.parent / f".tmp-{uuid.uuid4().hex}"
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dest)  # concurrent writers of the same sha replace with identical bytes

    def get(self, sha: bytes) -> bytes:
        try:
            return self.path(sha).read_bytes()
        except FileNotFoundError:
            raise MediaMissing(sha.hex()) from None


class BundledStore:
    """Seed images, normalized once at construction and held in memory."""

    writable = False

    def __init__(self, images_dir: str | Path, files: list[str], max_side: int = 2048):
        self._blobs: dict[bytes, bytes] = {}
        for name in files:
            norm = normalize_image((Path(images_dir) / name).read_bytes(), max_side=max_side)
            self._blobs[norm.sha256] = norm.jpeg

    def put(self, sha: bytes, data: bytes) -> None:
        raise ReadOnlyStore("BundledStore is read-only")

    def get(self, sha: bytes) -> bytes:
        try:
            return self._blobs[sha]
        except KeyError:
            raise MediaMissing(sha.hex()) from None

    def __contains__(self, sha: bytes) -> bool:
        return sha in self._blobs


class PgBlobStore:
    """Demo uploads (DESIGN §2.4 media_blobs); purged after 24 h."""

    writable = True

    def __init__(self, pool: ConnectionPool):
        self.pool = pool

    def put(self, sha: bytes, data: bytes) -> None:
        with db.tx(self.pool) as conn:
            conn.execute(
                "INSERT INTO media_blobs (sha256, data) VALUES (%s, %s) ON CONFLICT (sha256) DO NOTHING",
                (sha, data),
            )

    def get(self, sha: bytes) -> bytes:
        with db.tx(self.pool) as conn:
            row = conn.execute("SELECT data FROM media_blobs WHERE sha256 = %s", (sha,)).fetchone()
        if row is None:
            raise MediaMissing(sha.hex())
        return bytes(row[0])


class CompositeStore:
    def __init__(self, stores: list[MediaStore]):
        self.stores = stores
        self.writable = any(s.writable for s in stores)

    def put(self, sha: bytes, data: bytes) -> None:
        for s in self.stores:
            if s.writable:
                s.put(sha, data)
                return
        raise ReadOnlyStore("no writable store")

    def get(self, sha: bytes) -> bytes:
        for s in self.stores:
            try:
                return s.get(sha)
            except MediaMissing:
                continue
        raise MediaMissing(sha.hex())
