"""Settings loaded once from env. Spec: DESIGN.md §2.3."""

from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Fields whose default differs in demo mode, applied only when not set explicitly.
_DEMO_DEFAULTS = {
    "db_pool_max": 4,
    "vlm_provider": "openrouter",
    "vlm_model": "qwen/qwen2.5-vl-72b-instruct",
    "vlm_price_in_per_m": 0.80,
    "vlm_price_out_per_m": 1.00,
    "vlm_daily_cap": 100,
    "vlm_concurrency": 2,
    "vlm_timeout_s": 60,
    "worker_poll_s": 0,
    "trust_proxy_hops": 1,
    "models_dir": "/models",
}


class ConfigError(Exception):
    pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    # True only for the exact string "1"; "true", "0" and unset are all false.
    demo_mode_raw: str | None = Field(default=None, alias="DEMO_MODE")

    database_url: str = "postgresql://thrift:thrift@127.0.0.1:5433/thriftradar"
    db_pool_max: int = 8
    media_dir: str = str(Path(__file__).resolve().parents[2] / "data" / "media")
    models_dir: str = str(Path(__file__).resolve().parents[2] / "data" / "models")
    load_models: bool = True  # tests turn this off

    ingest_token: str | None = None
    sender_hmac_key: str | None = None
    session_secret: str | None = None
    demo_email: str = "demo@thriftradar.app"
    demo_password: str = "demo1234"

    vlm_provider: str = "ollama"
    vlm_model: str = "qwen2.5vl:7b"
    openrouter_api_key: str | None = None
    featherless_api_key: str | None = None
    vlm_price_in_per_m: float = 0.0
    vlm_price_out_per_m: float = 0.0
    vlm_daily_cap: int = 2000
    vlm_rpm: int = 20
    vlm_concurrency: int = 1
    vlm_timeout_s: int = 120
    vlm_max_attempts: int = 3
    vlm_max_images: int = 4
    vlm_max_side_px: int = 672
    vlm_required_fields: str = "brand,size,price"

    worker_poll_s: float = 2
    lease_local_s: int = 600
    lease_vlm_s: int = 180
    max_local_attempts: int = 3

    repost_window_days: int = 90
    phash_max_dist: int = 2  # calibrated 2026-10-03: d=2 15/15 same, d=4 4/15 (DESIGN §4.5)
    phash_xseller_max_dist: int = 2
    repost_emb_min_cos: float = 0.96  # calibrated 2026-10-03 (DESIGN §4.5 find_repost_embedding)
    siglip_brand_min_cos: float = 0.08  # calibrated 2026-10-03 (DESIGN §4.5 process_local step 11)
    siglip_brand_margin: float = 0.03
    ocr_max_images: int = 4

    notify_macos: bool = True
    match_min_text: float = 0.08  # calibrated 2026-10-03 (DESIGN §4.6)
    match_min_image: float = 0.80
    trust_proxy_hops: int = 0
    default_currency: str = ""

    upload_max_bytes: int = 5 * 1024 * 1024
    upload_max_images: int = 4
    ingest_max_bytes: int = 100 * 1024 * 1024
    ingest_max_images: int = 30

    @property
    def demo_mode(self) -> bool:
        return self.demo_mode_raw == "1"

    @property
    def required_fields(self) -> list[str]:
        return [f.strip() for f in self.vlm_required_fields.split(",") if f.strip()]

    @model_validator(mode="after")
    def _apply_mode_defaults(self) -> "Settings":
        if self.demo_mode:
            for name, value in _DEMO_DEFAULTS.items():
                if name not in self.model_fields_set:
                    setattr(self, name, value)
            self.notify_macos = False
        if self.vlm_provider not in ("ollama", "openrouter", "featherless", "off"):
            raise ValueError("VLM_PROVIDER must be ollama, openrouter, featherless or off")
        return self

    def check_startup(self) -> None:
        """Mode-specific required values; create_app calls this (DESIGN §4.3 pre 6)."""
        missing = []
        if not self.demo_mode:
            missing += [n for n in ("ingest_token", "sender_hmac_key") if not getattr(self, n)]
        else:
            missing += [n for n in ("session_secret", "sender_hmac_key") if not getattr(self, n)]
        if self.vlm_provider == "openrouter" and not self.openrouter_api_key:
            missing.append("openrouter_api_key")
        if self.vlm_provider == "featherless" and not self.featherless_api_key:
            missing.append("featherless_api_key")
        if missing:
            raise ConfigError("missing required settings: " + ", ".join(n.upper() for n in missing))
