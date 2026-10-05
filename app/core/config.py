from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = (
        "postgresql+asyncpg://omniguardian:local-development-only@localhost:5432/omniguardian"
    )
    redis_url: str = "redis://localhost:6379/0"
    demo_api_key: str = "local-demo-key-change-before-deploying"
    db_pool_size: int = 20
    db_max_overflow: int = 30


@lru_cache
def get_settings() -> Settings:
    return Settings()
