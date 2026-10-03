from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from app.core.config import settings

engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session():
    """Yield one async SQLAlchemy session for a FastAPI request.

    Yields:
        AsyncSession configured by the shared session factory.

    Raises:
        Exception: Database/session exceptions propagate to the request handler;
            the context manager still closes the session.
    """
    async with SessionLocal() as session:
        yield session
