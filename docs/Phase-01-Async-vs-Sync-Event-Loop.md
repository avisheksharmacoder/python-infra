# Phase 1: Async vs. Sync Event Loop Blocking

---

## 1. Executive Summary & Concept

### 1.1 The Layman's Explanation
Imagine a busy bank teller who handles requests for 100 people in line. Normally, when a customer needs a heavy file from the storage vault, the teller stamps the ticket, passes it to a vault runner, and immediately begins helping the next customer in line. The teller never stands idle. That is **Asynchronous I/O**.

Now imagine the teller decides to leave their desk, walk all the way back to the vault, unlock the door, search through paper folders for 2 seconds, and walk back—all while keeping the single customer window completely closed. The entire queue of 100 people stands completely frozen outside. People get frustrated, line wait times explode, and customers walk away. 

That is what happens when you write an `async def` function in Python that calls synchronous database libraries like `psycopg2` or synchronous SQLAlchemy sessions.

### 1.2 The Technical Reality & Mechanics
FastAPI runs on an `asyncio` event loop (powered by `uvloop` inside Uvicorn). The event loop runs in a **single operating system thread**.

```
                Single OS Thread (Event Loop)
+-------------------------------------------------------------+
| Task 1 (HTTP Req) -> Awaiting DB -> Pauses Task 1           |
| Task 2 (HTTP Req) -> Awaiting DB -> Pauses Task 2           |
| Task 3 (HTTP Req) -> Calls SYNC DB -> THREAD FREEZES 200ms  |  <-- CATASTROPHIC BLOCK!
|                                                              |  (No other tasks can run)
| [Tasks 4, 5, 6... wait in OS socket backlog until unfreeze]  |
+-------------------------------------------------------------+
```

* When you declare a FastAPI route as `async def`, FastAPI executes it directly on the main event loop thread, assuming that every I/O operation will cooperatively yield control (`await`).
* If you execute synchronous blocking I/O (e.g., `psycopg2`, `time.sleep()`, or synchronous `session.query()`) inside an `async def` route, the Python interpreter halts the entire OS thread waiting for socket response bytes from PostgreSQL.
* Because the single event loop thread is frozen, **no other coroutine can run**. Heartbeats fail, other incoming HTTP requests sit unprocessed in the kernel TCP socket backlog, and throughput collapses.

---

## 2. Checkpoint A: The Pitfall (Degraded State)

### 2.1 Degraded Code Pattern
The developer defines the route as `async def` (believing it makes the API fast), but uses a synchronous database session under the hood:

```python
# Degraded Implementation
from fastapi import FastAPI, Depends
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from .models import Note

# Synchronous engine using psycopg2
sync_engine = create_engine("postgresql+psycopg2://user:pass@localhost:5432/notes_db")
SyncSessionLocal = sessionmaker(bind=sync_engine)

app = FastAPI()

def get_sync_db():
    db = SyncSessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.get("/users/{user_id}/notes")
async def get_user_notes_pitfall(user_id: int, db: Session = Depends(get_sync_db)):
    # CRITICAL BUG: Calling synchronous blocking DB driver inside async def!
    # The event loop thread is completely locked during this query execution.
    notes = db.query(Note).filter(Note.user_id == user_id).all()
    return notes
```

### 2.2 Telemetry Fingerprint (How to Spot It)

1. **Load Test Symptom (`wrk`):**
   ```bash
   wrk -t8 -c200 -d30s http://localhost:8000/users/42/notes
   ```
   * **Throughput:** Drops from ~3,500 RPS down to **~110–140 RPS**.
   * **p99 Latency:** Explodes to **> 2,400ms**.
   * **Socket Errors:** High timeout rates as connections pile up in the OS TCP backlog.

2. **PostgreSQL Telemetry:**
   ```sql
   SELECT state, count(*) FROM pg_stat_activity GROUP BY state;
   ```
   * Database CPU sits under **5%**! The database is idle and healthy, but Python cannot send or receive queries because its single thread is blocked.

3. **Event Loop Lag:**
   * Custom middleware tracking `asyncio` loop lag reveals latency spikes of 200ms–500ms between loop iterations.

---

## 3. Checkpoint B: The Fix (Production-Grade State)

### 3.1 Architectural Solution
There are two proper ways to handle database I/O in FastAPI:

#### Approach 1: Native Async Driver (`asyncpg` + `AsyncSession`) — Recommended
Use an asynchronous database driver that cooperatively yields control back to the event loop while waiting for network packets:

```python
# Fixed Implementation (Asyncpg)
from fastapi import FastAPI, Depends
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select
from .models import Note

async_engine = create_async_engine(
    "postgresql+asyncpg://user:pass@localhost:5432/notes_db",
    pool_size=30,
    max_overflow=20
)
AsyncSessionLocal = async_sessionmaker(bind=async_engine, class_=AsyncSession, expire_on_commit=False)

app = FastAPI()

async def get_async_db():
    async with AsyncSessionLocal() as session:
        yield session

@app.get("/users/{user_id}/notes")
async def get_user_notes_fixed(user_id: int, session: AsyncSession = Depends(get_async_db)):
    # Non-blocking async execution: yields control to the event loop while PostgreSQL executes
    stmt = select(Note).where(Note.user_id == user_id)
    result = await session.execute(stmt)
    return result.scalars().all()
```

#### Approach 2: Synchronous Route Offloading (`def` without `async`)
If you **must** use synchronous drivers (e.g., legacy libraries), define the route as regular `def` (not `async def`). FastAPI will automatically offload the entire function to an external worker threadpool (`anyio.to_thread`):

```python
@app.get("/users/{user_id}/notes")
def get_user_notes_threadpool(user_id: int, db: Session = Depends(get_sync_db)):
    # FastAPI runs regular 'def' endpoints in a separate background threadpool!
    # The main event loop remains free to process incoming requests.
    return db.query(Note).filter(Note.user_id == user_id).all()
```

---

## 4. Expected Performance Delta

| Metric | Checkpoint A (Pitfall) | Checkpoint B (Fixed: `asyncpg`) | Delta |
| :--- | :--- | :--- | :--- |
| **Throughput (RPS)** | ~120 RPS | **3,600+ RPS** | **30x improvement** |
| **p50 Latency** | 1,400ms | **12ms** | **99% reduction** |
| **p99 Latency** | 2,800ms | **28ms** | **99% reduction** |
| **Event Loop Lag** | > 400ms | **< 1ms** | Normal operation |
| **CPU Utilization** | Single core pegged at 100% waiting on I/O | Balanced event loop utilization | Fully non-blocking |

---

## 5. Step-by-Step Lab Runbook

1. **Step 1:** Mount the degraded endpoint in FastAPI using `async def` and `psycopg2`.
2. **Step 2:** Start the database telemetry terminal watcher:
   ```bash
   watch -n 1 'psql -U postgres -d notes_db -c "SELECT state, count(*) FROM pg_stat_activity GROUP BY state;"'
   ```
3. **Step 3:** Fire the load generator:
   ```bash
   wrk -t8 -c200 -d30s http://localhost:8000/users/1/notes
   ```
4. **Step 4:** Observe the collapsed RPS (~120 RPS) and verify that Postgres CPU is near 0%.
5. **Step 5:** Switch the route to `asyncpg` with `await session.execute()`.
6. **Step 6:** Re-run the identical `wrk` command. Observe immediate recovery to 3,500+ RPS.

---

## 6. Senior Backend Interview Talking Points

* **The Trap:** Junior developers think adding `async def` makes an endpoint faster. In reality, adding `async def` to blocking code makes it catastrophically slower than regular `def`.
* **FastAPI Internals:** Explain how FastAPI treats `def` vs `async def`: `def` functions get sent to an internal `threadpool` (default size 40), preventing event loop starvation. `async def` functions run directly on the event loop.
* **Network Sockets in Async:** In `asyncpg`, socket reads/writes register file descriptors with the OS event notification system (`epoll` on Linux, `kqueue` on macOS). Python sleeps until the kernel signals that the DB response packet has arrived.
