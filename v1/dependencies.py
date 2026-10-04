import uuid
from typing import AsyncGenerator, Generator
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from loguru import logger
try:
    from .database import AsyncSessionLocal, SyncSessionLocal
except ImportError:
    from database import AsyncSessionLocal, SyncSessionLocal

# 1. Primary Asynchronous Database Dependency
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that yields an isolated AsyncSession per request.
    Guarantees:
      - Automatic transaction commit if endpoint completes without error.
      - Automatic transaction rollback if an exception occurs.
      - Guaranteed connection release back to QueuePool in all cases.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception as exc:
            await session.rollback()
            raise exc
        finally:
            await session.close()

# 2. Synchronous Database Dependency (Used in Phase 1 for sync comparison)
def get_sync_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency yielding a synchronous Session.
    Used exclusively in Phase 1 to demonstrate event loop starvation vs threadpool offloading.
    """
    db = SyncSessionLocal()
    try:
        yield db
        db.commit()
    except Exception as exc:
        db.rollback()
        raise exc
    finally:
        db.close()

# 3. Request Correlation & Contextual Logger Dependency
def get_request_id(request: Request) -> str:
    """
    Extract or generate a unique correlation UUID for the incoming request.
    """
    req_id = getattr(request.state, "request_id", None)
    if not req_id:
        req_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        request.state.request_id = req_id
    return req_id

def get_request_logger(request: Request):
    """
    Returns a contextual Loguru logger instance bound with:
      - request_id
      - method
      - path
      - client_ip
    """
    req_id = get_request_id(request)
    client_ip = request.client.host if request.client else "unknown"
    return logger.bind(
        request_id=req_id,
        method=request.method,
        path=request.url.path,
        client_ip=client_ip
    )

if __name__ == "__main__":
    import asyncio
    async def test():
        # Verify get_db lifecycle
        async for session in get_db():
            assert isinstance(session, AsyncSession)
            print("Session dependency lifecycle verified successfully!")
            break
    asyncio.run(test())
