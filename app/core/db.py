from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings

settings = get_settings()
engine = create_async_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_pre_ping=True,
    connect_args={"server_settings": {"statement_timeout": "15000", "lock_timeout": "10000"}},
)
Session = async_sessionmaker(engine, expire_on_commit=False)
