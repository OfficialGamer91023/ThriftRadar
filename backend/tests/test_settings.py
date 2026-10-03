import pytest

from app.settings import ConfigError, Settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no stray .env
    for var in ("DEMO_MODE", "VLM_DAILY_CAP", "NOTIFY_MACOS", "INGEST_TOKEN", "SENDER_HMAC_KEY"):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.parametrize("raw,expected", [(None, False), ("0", False), ("true", False), ("1 ", False), ("1", True)])
def test_demo_mode_only_exact_one(monkeypatch, raw, expected):
    if raw is not None:
        monkeypatch.setenv("DEMO_MODE", raw)
    assert Settings().demo_mode is expected


def test_demo_defaults_and_overrides(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    s = Settings()
    assert s.vlm_daily_cap == 100 and s.worker_poll_s == 0 and s.vlm_provider == "openrouter"
    monkeypatch.setenv("VLM_DAILY_CAP", "7")
    monkeypatch.setenv("NOTIFY_MACOS", "1")
    s = Settings()
    assert s.vlm_daily_cap == 7
    assert s.notify_macos is False  # forced off in demo


def test_local_defaults():
    s = Settings()
    assert s.vlm_daily_cap == 2000 and s.vlm_provider == "ollama" and s.notify_macos is True


def test_check_startup_requires_local_secrets(monkeypatch):
    with pytest.raises(ConfigError, match="INGEST_TOKEN"):
        Settings().check_startup()
    monkeypatch.setenv("INGEST_TOKEN", "t")
    monkeypatch.setenv("SENDER_HMAC_KEY", "k")
    Settings().check_startup()


def test_bad_provider_rejected(monkeypatch):
    monkeypatch.setenv("VLM_PROVIDER", "gpt")
    with pytest.raises(ValueError):
        Settings()
