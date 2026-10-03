"""Apply SQL migrations in order. Spec: DESIGN.md §4.3 `apply_migrations`."""

import argparse
import logging
import os
import re
import sys
from pathlib import Path

import psycopg

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_NAME_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


class WrongRole(Exception):
    pass


def _files(directory: Path) -> list[Path]:
    found = [p for p in directory.iterdir() if p.is_file() and _NAME_RE.match(p.name)]
    return sorted(found, key=lambda p: int(_NAME_RE.match(p.name).group(1)))


def migration_files(demo: bool) -> list[tuple[str, Path]]:
    files = [(p.name, p) for p in _files(MIGRATIONS_DIR)]
    if demo:
        files += [(f"demo/{p.name}", p) for p in _files(MIGRATIONS_DIR / "demo")]
    return files


def apply_migrations(conninfo: str, demo: bool = False) -> list[str]:
    applied_now: list[str] = []
    with psycopg.connect(conninfo, autocommit=False) as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('thriftradar.migrate'))")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            applied = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
            has_meta = conn.execute("SELECT to_regclass('db_meta') IS NOT NULL").fetchone()[0]
            if has_meta and not demo:
                row = conn.execute("SELECT value FROM db_meta WHERE key = 'role'").fetchone()
                if row and row[0] == "demo":
                    raise WrongRole("database role is 'demo'; run with --demo")
            for name, path in migration_files(demo):
                if name in applied:
                    continue
                conn.execute(path.read_text())
                conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
                applied_now.append(name)
                log.info("migration applied: %s", name)
            if applied_now:
                conn.execute(
                    "INSERT INTO db_meta (key, value) VALUES ('schema_version', %s)"
                    " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                    (applied_now[-1],),
                )
    return applied_now


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.migrate")
    parser.add_argument("--demo", action="store_true", help="also apply demo lockdown migrations")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    conninfo = os.environ.get("DATABASE_URL", "postgresql://thrift:thrift@127.0.0.1:5433/thriftradar")
    try:
        names = apply_migrations(conninfo, demo=args.demo)
    except WrongRole as e:
        log.error("%s", e)
        return 2
    log.info("migrations done: %d applied", len(names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
