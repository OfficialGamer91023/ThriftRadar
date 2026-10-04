"""GET /api/credits: photo attribution for the demo seed (CC BY / BY-SA require it). Spec: DESIGN.md §4.8
`validate_licenses` (the credits page is built from ATTRIBUTION.csv). Public: no session, no DB."""

import csv
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Request

router = APIRouter()


@lru_cache(maxsize=4)
def _load(path: str) -> list[dict]:
    p = Path(path)
    if not p.is_file():
        return []
    with open(p, newline="") as f:
        return [{k: r[k] for k in ("file", "author", "license", "license_url", "source_url", "notes")}
                for r in csv.DictReader(f)]


@router.get("/api/credits")
def credits(request: Request):
    return {"results": _load(str(Path(request.app.state.settings.seed_dir) / "ATTRIBUTION.csv"))}
