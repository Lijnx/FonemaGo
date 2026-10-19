from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: SecretStr = SecretStr("")
    db_path: Path = Path("data/fonemago.sqlite3")
    audio_dir: Path = Path("data/audio")
    content_path: Path = Path("content/catalog.json")
    max_voice_sec: int = Field(default=15, ge=1, le=120)
    max_voice_bytes: int = Field(default=1_048_576, ge=1024)
    log_level: str = "INFO"
