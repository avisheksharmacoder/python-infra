from typing import List, Dict, Any
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

try:
    from ..database import Note
    from ..dependencies import get_db, get_sync_db
    from ..schemas import NoteResponse
except ImportError:
    from database import Note
    from dependencies import get_db, get_sync_db
    from schemas import NoteResponse

router = APIRouter()

@router.get("/info", tags=["Phase 01: Async vs Sync"])
def phase01_info() -> Dict[str, Any]:
    """
    Overview of Phase 1 benchmarks and mechanics.
    """
    return {
        "phase": "01",
        "title": "Async vs. Sync Event Loop Blocking",
        "endpoints": {
            "pitfall": "/v1/phase01/pitfall/users/{user_id}/notes",
            "optimized": "/v1/phase01/optimized/users/{user_id}/notes",
            "threadpool": "/v1/phase01/threadpool/users/{user_id}/notes",
        },
        "description": "Compares event-loop starvation when synchronous blocking database calls (psycopg2) are used inside 'async def' versus non-blocking native asyncpg and threadpool offloading."
    }

# 1. Checkpoint A: The Pitfall (Degraded State)
# Declared as 'async def', but executes synchronous psycopg2 blocking queries directly on the main event loop thread!
@router.get(
    "/pitfall/users/{user_id}/notes",
    response_model=List[NoteResponse],
    tags=["Phase 01: Async vs Sync"]
)
async def get_user_notes_pitfall(
    user_id: int,
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_sync_db)
):
    """
    PITFALL: Declared with 'async def', but calls synchronous psycopg2 session.
    The single event loop OS thread freezes during the entire PostgreSQL socket roundtrip.
    Under concurrent load, all other tasks are blocked in the event loop queue.
    """
    stmt = (
        select(Note)
        .where(Note.user_id == user_id)
        .order_by(Note.id.desc())
        .limit(limit)
    )
    result = db.execute(stmt)
    return result.scalars().all()

# 2. Checkpoint B: The Production Fix (Approach 1: Native Async Driver asyncpg)
# Declared as 'async def', uses asynchronous asyncpg driver and awaits socket I/O.
@router.get(
    "/optimized/users/{user_id}/notes",
    response_model=List[NoteResponse],
    tags=["Phase 01: Async vs Sync"]
)
async def get_user_notes_optimized(
    user_id: int,
    limit: int = Query(50, ge=1, le=100),
    session: AsyncSession = Depends(get_db)
):
    """
    PRODUCTION FIX: Native non-blocking asyncpg driver with AsyncSession.
    Cooperatively yields control back to uvloop event loop with 'await', allowing
    hundreds of other concurrent requests to be multiplexed while PostgreSQL executes.
    """
    stmt = (
        select(Note)
        .where(Note.user_id == user_id)
        .order_by(Note.id.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return result.scalars().all()

# 3. Checkpoint C: Alternative Mitigation (Approach 2: Regular 'def' Threadpool Offload)
# Declared as regular 'def' (NOT async def). FastAPI offloads it to anyio worker threadpool.
@router.get(
    "/threadpool/users/{user_id}/notes",
    response_model=List[NoteResponse],
    tags=["Phase 01: Async vs Sync"]
)
def get_user_notes_threadpool(
    user_id: int,
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_sync_db)
):
    """
    THREADPOOL MITIGATION: Declared as regular 'def' (not 'async def').
    FastAPI detects synchronous function and offloads execution to an external
    worker threadpool (anyio.to_thread), keeping the main event loop responsive.
    """
    stmt = (
        select(Note)
        .where(Note.user_id == user_id)
        .order_by(Note.id.desc())
        .limit(limit)
    )
    result = db.execute(stmt)
    return result.scalars().all()
