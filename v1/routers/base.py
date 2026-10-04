import time
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, insert, update, delete, text
from loguru import logger

try:
    from ..database import async_engine, User, Note, Tag
    from ..dependencies import get_db, get_request_logger
    from ..schemas import (
        NoteResponse,
        NoteCreate,
        NoteUpdate,
        HealthCheckResponse,
        PoolStats
    )
except ImportError:
    from database import async_engine, User, Note, Tag
    from dependencies import get_db, get_request_logger
    from schemas import (
        UserCreate,
        UserResponse,
        NoteResponse,
        NoteCreate,
        NoteUpdate,
        HealthCheckResponse,
        PoolStats
    )

router = APIRouter()

# 1. Healthcheck & Telemetry Endpoint
@router.get("/health", response_model=HealthCheckResponse, tags=["Observability"])
async def health_check(session: AsyncSession = Depends(get_db)):
    """
    Validates database TCP connectivity, measures roundtrip ping latency, 
    and reports live connection pool utilization.
    """
    start_time = time.perf_counter()
    await session.execute(text("SELECT 1;"))
    latency_ms = round((time.perf_counter() - start_time) * 1000, 3)

    pool = async_engine.pool
    pool_stats = PoolStats(
        size=pool.size(),
        checked_in=pool.checkedin(),
        checked_out=pool.checkedout(),
        overflow=pool.overflow()
    )

    return HealthCheckResponse(
        status="healthy",
        database="connected",
        latency_ms=latency_ms,
        pool=pool_stats
    )

# 1.0 JSON Database Stats Endpoint
@router.get("/stats", tags=["Observability"])
async def get_db_stats(session: AsyncSession = Depends(get_db)):
    """
    Returns exact counts of users, notes, tags, and note_tags along with pool metrics.
    """
    start_time = time.perf_counter()
    query = text("""
        SELECT 
            (SELECT count(*) FROM users) AS users_count,
            (SELECT count(*) FROM notes) AS notes_count,
            (SELECT count(*) FROM tags) AS tags_count,
            (SELECT count(*) FROM note_tags) AS note_tags_count,
            (SELECT count(*) FROM notes WHERE status = 'active') AS active_notes,
            (SELECT count(*) FROM notes WHERE status = 'archived') AS archived_notes;
    """)
    result = await session.execute(query)
    row = result.mappings().one()
    latency_ms = round((time.perf_counter() - start_time) * 1000, 2)

    pool = async_engine.pool
    return {
        "status": "healthy",
        "counts": {
            "users": row["users_count"],
            "notes": row["notes_count"],
            "tags": row["tags_count"],
            "note_tags": row["note_tags_count"],
            "active_notes": row["active_notes"],
            "archived_notes": row["archived_notes"]
        },
        "pool": {
            "size": pool.size(),
            "checked_in": pool.checkedin(),
            "checked_out": pool.checkedout(),
            "overflow": pool.overflow()
        },
        "query_latency_ms": latency_ms
    }

# 1.01 Live HTML Dashboard
from fastapi.responses import HTMLResponse

@router.get("/dashboard", response_class=HTMLResponse, tags=["Observability"])
async def live_dashboard():
    """
    Serves a dark-mode real-time telemetry dashboard visualizing database counts and pool metrics.
    """
    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>PAUF Live Telemetry Dashboard</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #090d16;
      --card-bg: rgba(17, 24, 39, 0.85);
      --card-border: rgba(255, 255, 255, 0.08);
      --accent-blue: #38bdf8;
      --accent-indigo: #818cf8;
      --accent-emerald: #10b981;
      --accent-amber: #f59e0b;
      --accent-rose: #f43f5e;
      --text-main: #f3f4f6;
      --text-muted: #9ca3af;
      --text-dim: #6b7280;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background-color: var(--bg);
      color: var(--text-main);
      font-family: 'Inter', -apple-system, sans-serif;
      min-height: 100vh;
      padding: 2.5rem 1.5rem;
      background-image: 
        radial-gradient(circle at 15% 15%, rgba(56, 189, 248, 0.08) 0%, transparent 40%),
        radial-gradient(circle at 85% 85%, rgba(129, 140, 248, 0.08) 0%, transparent 40%);
    }
    .container { max-width: 1200px; margin: 0 auto; }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 1rem;
      margin-bottom: 2.5rem;
      padding-bottom: 1.5rem;
      border-bottom: 1px solid var(--card-border);
    }
    .brand { display: flex; align-items: center; gap: 0.85rem; }
    .brand-icon {
      width: 44px;
      height: 44px;
      border-radius: 12px;
      background: linear-gradient(135deg, #0284c7, #6366f1);
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 800;
      font-size: 1.25rem;
      color: #fff;
      box-shadow: 0 4px 20px rgba(2, 132, 199, 0.35);
    }
    h1 { font-size: 1.6rem; font-weight: 800; letter-spacing: -0.02em; }
    .subtitle { font-size: 0.85rem; color: var(--text-muted); margin-top: 0.2rem; }
    .status-badge {
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.4rem 0.85rem;
      background: rgba(16, 185, 129, 0.12);
      border: 1px solid rgba(16, 185, 129, 0.3);
      border-radius: 9999px;
      color: #34d399;
      font-size: 0.8rem;
      font-weight: 600;
    }
    .pulse-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background-color: #34d399;
      box-shadow: 0 0 10px #34d399;
      animation: pulse 1.5s infinite;
    }
    @keyframes pulse {
      0%, 100% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.4; transform: scale(0.85); }
    }
    .controls {
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }
    button, select, a.btn {
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid var(--card-border);
      color: var(--text-main);
      padding: 0.5rem 0.9rem;
      border-radius: 8px;
      font-size: 0.85rem;
      font-weight: 500;
      cursor: pointer;
      text-decoration: none;
      transition: all 0.2s ease;
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
    }
    button:hover, select:hover, a.btn:hover {
      background: rgba(255, 255, 255, 0.1);
      border-color: rgba(255, 255, 255, 0.2);
    }
    .btn-primary {
      background: linear-gradient(135deg, #0284c7, #2563eb);
      border: none;
      color: #fff;
    }
    .btn-primary:hover {
      background: linear-gradient(135deg, #0369a1, #1d4ed8);
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 1.25rem;
      margin-bottom: 2rem;
    }
    .card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 16px;
      padding: 1.5rem;
      backdrop-filter: blur(12px);
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.25);
      position: relative;
      overflow: hidden;
      transition: transform 0.2s ease, border-color 0.2s ease;
    }
    .card:hover {
      transform: translateY(-2px);
      border-color: rgba(255, 255, 255, 0.16);
    }
    .card::before {
      content: '';
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      height: 3px;
      background: linear-gradient(90deg, transparent, var(--card-accent, var(--accent-blue)), transparent);
    }
    .card-label {
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--text-muted);
      font-weight: 600;
      margin-bottom: 0.5rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .card-value {
      font-size: 2.2rem;
      font-weight: 800;
      color: #ffffff;
      font-family: 'JetBrains Mono', monospace;
      letter-spacing: -0.03em;
    }
    .card-subtext {
      font-size: 0.8rem;
      color: var(--text-dim);
      margin-top: 0.6rem;
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }
    .pill {
      font-size: 0.72rem;
      padding: 0.15rem 0.5rem;
      border-radius: 9999px;
      font-weight: 600;
    }
    .pill-emerald { background: rgba(16, 185, 129, 0.15); color: #34d399; }
    .pill-amber { background: rgba(245, 158, 11, 0.15); color: #fbbf24; }
    .pill-blue { background: rgba(56, 189, 248, 0.15); color: #38bdf8; }
    
    .panel {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 16px;
      padding: 1.5rem;
      margin-bottom: 2rem;
    }
    .panel-title {
      font-size: 1rem;
      font-weight: 700;
      margin-bottom: 1rem;
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }
    .metrics-row {
      display: flex;
      flex-wrap: wrap;
      gap: 2rem;
    }
    .metric-item { flex: 1; min-width: 140px; }
    .metric-name { font-size: 0.75rem; color: var(--text-muted); margin-bottom: 0.25rem; }
    .metric-val { font-size: 1.25rem; font-weight: 700; font-family: 'JetBrains Mono', monospace; }

    footer {
      text-align: center;
      font-size: 0.8rem;
      color: var(--text-dim);
      margin-top: 3rem;
    }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="brand">
        <div class="brand-icon">⚡</div>
        <div>
          <h1>PAUF Observability Dashboard</h1>
          <div class="subtitle">Live Database State & Connection Telemetry</div>
        </div>
      </div>
      <div class="controls">
        <div class="status-badge">
          <span class="pulse-dot"></span>
          <span id="conn-status">LIVE MONITORING</span>
        </div>
        <button id="refresh-btn" class="btn-primary" onclick="fetchStats()">↻ Refresh</button>
        <a href="/v1/stats" target="_blank" class="btn">JSON API</a>
        <a href="/docs" target="_blank" class="btn">Swagger Docs</a>
      </div>
    </header>

    <div class="grid">
      <!-- Users Card -->
      <div class="card" style="--card-accent: var(--accent-blue);">
        <div class="card-label">
          <span>Active Users</span>
          <span class="pill pill-blue">users tbl</span>
        </div>
        <div class="card-value" id="val-users">-</div>
        <div class="card-subtext">Total active seeded users</div>
      </div>

      <!-- Notes Card -->
      <div class="card" style="--card-accent: var(--accent-indigo);">
        <div class="card-label">
          <span>Total Notes</span>
          <span class="pill pill-emerald" id="val-active-pill">- active</span>
        </div>
        <div class="card-value" id="val-notes">-</div>
        <div class="card-subtext">
          <span class="pill pill-amber" id="val-archived-pill">- archived</span>
          <span>across all users</span>
        </div>
      </div>

      <!-- Tags Card -->
      <div class="card" style="--card-accent: var(--accent-emerald);">
        <div class="card-label">
          <span>Total Tags</span>
          <span class="pill pill-emerald">tags tbl</span>
        </div>
        <div class="card-value" id="val-tags">-</div>
        <div class="card-subtext">Categorization tags</div>
      </div>

      <!-- Note-Tags Card -->
      <div class="card" style="--card-accent: var(--accent-amber);">
        <div class="card-label">
          <span>Tag Relations</span>
          <span class="pill pill-amber">note_tags tbl</span>
        </div>
        <div class="card-value" id="val-note-tags">-</div>
        <div class="card-subtext">Many-to-many associations</div>
      </div>
    </div>

    <!-- Connection Pool & Engine Health -->
    <div class="panel">
      <div class="panel-title">
        <span>🔌 SQLAlchemy QueuePool Telemetry</span>
      </div>
      <div class="metrics-row">
        <div class="metric-item">
          <div class="metric-name">Pool Max Size</div>
          <div class="metric-val" id="pool-size">-</div>
        </div>
        <div class="metric-item">
          <div class="metric-name">Checked In (Idle)</div>
          <div class="metric-val" style="color: #34d399;" id="pool-checked-in">-</div>
        </div>
        <div class="metric-item">
          <div class="metric-name">Checked Out (Active)</div>
          <div class="metric-val" style="color: #38bdf8;" id="pool-checked-out">-</div>
        </div>
        <div class="metric-item">
          <div class="metric-name">Overflow Allowed</div>
          <div class="metric-val" style="color: #fbbf24;" id="pool-overflow">-</div>
        </div>
        <div class="metric-item">
          <div class="metric-name">Query Latency</div>
          <div class="metric-val" id="query-latency" style="color: #a78bfa;">- ms</div>
        </div>
        <div class="metric-item">
          <div class="metric-name">Last Updated</div>
          <div class="metric-val" style="font-size: 0.95rem; color: var(--text-muted);" id="last-updated">-</div>
        </div>
      </div>
    </div>

    <footer>
      PAUF PostgreSQL & FastAPI High-Load Optimization Suite &bull; Auto-refreshing every 2s
    </footer>
  </div>

  <script>
    function formatNumber(num) {
      if (num === null || num === undefined) return "-";
      return new Intl.NumberFormat().format(num);
    }

    async function fetchStats() {
      const btn = document.getElementById("refresh-btn");
      try {
        btn.textContent = "Loading...";
        const resp = await fetch("/v1/stats");
        if (!resp.ok) throw new Error("Network response was not ok");
        const data = await resp.json();

        // Update Counts
        document.getElementById("val-users").textContent = formatNumber(data.counts.users);
        document.getElementById("val-notes").textContent = formatNumber(data.counts.notes);
        document.getElementById("val-tags").textContent = formatNumber(data.counts.tags);
        document.getElementById("val-note-tags").textContent = formatNumber(data.counts.note_tags);
        
        document.getElementById("val-active-pill").textContent = formatNumber(data.counts.active_notes) + " active";
        document.getElementById("val-archived-pill").textContent = formatNumber(data.counts.archived_notes) + " archived";

        // Update Pool
        document.getElementById("pool-size").textContent = data.pool.size;
        document.getElementById("pool-checked-in").textContent = data.pool.checked_in;
        document.getElementById("pool-checked-out").textContent = data.pool.checked_out;
        document.getElementById("pool-overflow").textContent = data.pool.overflow;

        document.getElementById("query-latency").textContent = data.query_latency_ms + " ms";
        document.getElementById("last-updated").textContent = new Date().toLocaleTimeString();

        document.getElementById("conn-status").textContent = "CONNECTED (LIVE)";
      } catch (err) {
        console.error("Failed to fetch telemetry:", err);
        document.getElementById("conn-status").textContent = "OFFLINE";
      } finally {
        btn.textContent = "↻ Refresh";
      }
    }

    // Auto-fetch on load
    fetchStats();
    // Auto-refresh every 2 seconds
    setInterval(fetchStats, 2000);
  </script>
</body>
</html>
"""
    return HTMLResponse(content=html_content)

# 1.1 Create User
@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED, tags=["Users"])
async def create_user(
    payload: UserCreate,
    session: AsyncSession = Depends(get_db)
):
    stmt = (
        insert(User)
        .values(
            username=payload.username,
            email=payload.email
        )
        .returning(User)
    )
    result = await session.execute(stmt)
    return result.scalar_one()

# 1.2 List Users
@router.get("/users", response_model=List[UserResponse], tags=["Users"])
async def list_users(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db)
):
    stmt = select(User).order_by(User.id.asc()).limit(limit).offset(offset)
    result = await session.execute(stmt)
    return result.scalars().all()

# 2. List Notes (Baseline Pagination)
@router.get("/notes", response_model=List[NoteResponse], tags=["Notes"])
async def list_notes(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db)
):
    stmt = (
        select(Note)
        .order_by(Note.id.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(stmt)
    return result.scalars().all()

# 3. Read Single Note
@router.get("/notes/{note_id}", response_model=NoteResponse, tags=["Notes"])
async def get_note(
    note_id: int, 
    session: AsyncSession = Depends(get_db)
):
    stmt = select(Note).where(Note.id == note_id)
    result = await session.execute(stmt)
    note = result.scalar_one_or_none()

    if not note:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail=f"Note with ID {note_id} not found"
        )
    return note

# 4. Create Note
@router.post("/notes", response_model=NoteResponse, status_code=status.HTTP_201_CREATED, tags=["Notes"])
async def create_note(
    payload: NoteCreate, 
    session: AsyncSession = Depends(get_db)
):
    stmt = (
        insert(Note)
        .values(
            user_id=payload.user_id,
            title=payload.title,
            content=payload.content,
            status=payload.status or "active"
        )
        .returning(Note)
    )
    result = await session.execute(stmt)
    new_note = result.scalar_one()
    return new_note

# 5. Update Note
@router.put("/notes/{note_id}", response_model=NoteResponse, tags=["Notes"])
async def update_note(
    note_id: int, 
    payload: NoteUpdate, 
    session: AsyncSession = Depends(get_db)
):
    update_data = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="No update fields provided"
        )

    stmt = (
        update(Note)
        .where(Note.id == note_id)
        .values(**update_data)
        .returning(Note)
    )
    result = await session.execute(stmt)
    updated_note = result.scalar_one_or_none()

    if not updated_note:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail=f"Note with ID {note_id} not found"
        )
    return updated_note

# 6. Delete Note
@router.delete("/notes/{note_id}", tags=["Notes"])
async def delete_note(
    note_id: int, 
    session: AsyncSession = Depends(get_db)
):
    stmt = delete(Note).where(Note.id == note_id).returning(Note.id)
    result = await session.execute(stmt)
    deleted_id = result.scalar_one_or_none()

    if not deleted_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail=f"Note with ID {note_id} not found"
        )
    return {"status": "deleted", "id": deleted_id}

# 7. List User's Notes (Used heavily in 60% workload.lua traffic)
@router.get("/users/{user_id}/notes", response_model=List[NoteResponse], tags=["Users"])
async def get_user_notes(
    user_id: int,
    limit: int = Query(50, ge=1, le=100),
    session: AsyncSession = Depends(get_db)
):
    stmt = (
        select(Note)
        .where(Note.user_id == user_id)
        .order_by(Note.id.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return result.scalars().all()
