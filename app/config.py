from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Africa Water Intelligence Platform"
    app_version: str = "3.0.0"
    environment: str = "development"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"

    cors_origins: str = "*"

    base_dir: str = str(BASE_DIR)
    data_dir: str = str(BASE_DIR / "data")
    cache_dir: str = str(BASE_DIR / "cache")
    natural_earth_dir: str = str(BASE_DIR / "data" / "natural_earth")

    africa_min_lon: float = -20.0
    africa_max_lon: float = 55.0
    africa_min_lat: float = -35.0
    africa_max_lat: float = 38.0

    gee_service_account_email: Optional[str] = None
    gee_service_account_key_path: Optional[str] = None
    gee_service_account_json: Optional[str] = None
    gee_project_id: Optional[str] = None

    stac_url: str = "https://planetarycomputer.microsoft.com/api/stac/v1"
    stac_days_back: int = 30
    stac_collection_s1: str = "sentinel-1-grd"
    stac_collection_s2: str = "sentinel-2-l2a"
    stac_collection_s3: str = "sentinel-3-olci"
    stac_collection_landsat: str = "landsat-c2-l2"

    request_timeout_seconds: int = 90
    default_buffer_km: float = 5.0
    default_export_scale_m: int = 30

    @property
    def cors_origin_list(self) -> List[str]:
        raw = (self.cors_origins or "*").strip()
        if raw == "*":
            return ["*"]
        return [item.strip() for item in raw.split(",") if item.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.cache_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.natural_earth_dir).mkdir(parents=True, exist_ok=True)
    return settings
