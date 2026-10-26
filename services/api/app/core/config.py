from pathlib import Path

from pydantic_settings import (
    BaseSettings,
    SettingsConfigDict,
)


BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    database_url: str
    demo_mode: bool = False
    demo_tenant_id: str = "fixture-tenant-zhangqing"
    demo_account_set_id: str = "fixture-account-set-general-vat"
    storage_dir: Path = BASE_DIR / "storage"
    ocr_model_root: Path = Path.home() / ".cache" / "zhangqing-ai" / "ocr-models"
    max_upload_bytes: int = 12 * 1024 * 1024

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
