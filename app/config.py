"""Runtime configuration, overridable through environment variables."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_url: str = os.getenv("GEO_DB_URL", "sqlite:///./data/geo.db")
    max_upload_bytes: int = int(os.getenv("GEO_MAX_UPLOAD_MB", "50")) * 1024 * 1024
    # Zip-bomb guards for shapefile archives
    max_unzipped_bytes: int = int(os.getenv("GEO_MAX_UNZIPPED_MB", "500")) * 1024 * 1024
    max_zip_members: int = int(os.getenv("GEO_MAX_ZIP_MEMBERS", "200"))
    max_features: int = int(os.getenv("GEO_MAX_FEATURES", "200000"))


settings = Settings()
