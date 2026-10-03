"""Shared fixtures. Tests run against the compose Postgres (DESIGN.md §6)."""

import os

import psycopg
import pytest

from app.migrate import apply_migrations
from app.settings import Settings

PG_BASE = os.environ.get("TEST_PG_BASE", "postgresql://thrift:thrift@127.0.0.1:5433")

# Tests never read backend/.env (it holds the real provider and keys) and never spend money: tests that need
# a VLM pass a fake backend explicitly. Blank keys guard against keys exported in the shell.
Settings.model_config["env_file"] = None
for _key in ("FEATHERLESS_API_KEY", "OPENROUTER_API_KEY", "VLM_PROVIDER"):
    os.environ.pop(_key, None)


def _recreate(dbname: str) -> str:
    with psycopg.connect(f"{PG_BASE}/postgres", autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{dbname}"')
    return f"{PG_BASE}/{dbname}"


@pytest.fixture(scope="session")
def db_url() -> str:
    """A fresh local-role test database with migrations applied, once per session."""
    url = _recreate("thriftradar_test")
    apply_migrations(url)
    return url


@pytest.fixture(scope="session")
def demo_db_url() -> str:
    url = _recreate("thriftradar_test_demo")
    apply_migrations(url, demo=True)
    return url


@pytest.fixture
def fresh_db_url() -> str:
    """An empty database with no migrations, for migration tests."""
    return _recreate("thriftradar_test_fresh")
