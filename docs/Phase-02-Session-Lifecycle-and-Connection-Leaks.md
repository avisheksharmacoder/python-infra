# Phase 2: Session Lifecycle & Connection Leaking

---

## 1. Executive Summary & Concept

### 1.1 The Layman's Explanation
Imagine a small library that owns 20 copies of a popular reference encyclopedia. Students come in, sign out an encyclopedia, take it to their desks, and study. 

Now imagine that when students finish reading or when they get interrupted by a phone call, they simply leave the encyclopedia abandoned on their desks and walk out the door without returning it to the librarian. Within two hours, every single copy is sitting abandoned in the library. When the next 50 students walk in, the librarian tells them: *"No books available, please wait."* The students wait in line until they give up and leave angry, even though nobody is actively reading those 20 books.

In backend systems, database connections are those books. If your code borrows a connection to talk to the database and fails to return it because of an error or careless coding, the connection stays locked forever. Soon, your entire API grinds to a halt.

### 1.2 The Technical Reality & Mechanics
In PostgreSQL, each client connection is not just a light thread—it is a **dedicated backend operating system process** (forked by PostgreSQL's `postmaster`) with allocated memory buffers (`work_mem`, private caches, transaction tracking structures). Because each connection consumes significant memory and CPU overhead, PostgreSQL enforces a strict limit: `max_connections = 100`.

To avoid the overhead of opening and closing TCP sockets for every HTTP request, applications use a **Connection Pool** (e.g., SQLAlchemy's `QueuePool`).

```
FastAPI Application Pool (e.g. pool_size=10, max_overflow=5)
[Conn 1: In-Use] [Conn 2: In-Use] [Conn 3: In-Use] ... [Conn 15: In-Use]
                                 │
                 (All connections checked out!)
                                 ▼
Incoming HTTP Request #16 -> Waits 30 seconds for a connection ->
  TIMEOUT ERROR: QueuePool limit reached. Unable to checkout connection!
```

* An `AsyncSession` wraps an underlying raw database connection.
* If a route instantiates an `AsyncSession` manually without strict `try...finally` or `async with` context managers, any exception (e.g., validation failure, 404 HTTP exception, or timeout) terminates the function **without executing the cleanup/close routine**.
* The raw TCP connection remains checked out in PostgreSQL in an `idle in transaction` state. The database holds open transaction locks and snapshot memory buffers, waiting for a command that will never come.
* Within minutes of concurrent traffic, the connection pool is 100% depleted. Every subsequent HTTP request across the entire application hangs and dies.

---

## 2. Checkpoint A: The Pitfall (Degraded State)

### 2.1 Degraded Code Patterns

#### Pattern 1: Manual Instantiation with Early Return or Exception
```python
# Degraded Implementation: Missing cleanup on error
from fastapi import FastAPI, HTTPException
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select
from .models import Note

engine = create_async_engine("postgresql+asyncpg://user:pass@localhost:5432/notes_db", pool_size=10, max_overflow=5)
AsyncSessionLocal = async_sessionmaker(bind=engine, class_=AsyncSession)

app = FastAPI()

@app.get("/notes/{note_id}")
async def get_note_leaky(note_id: int):
    # DANGEROUS: Manual session instantiation without context manager
    session = AsyncSessionLocal()
    
    stmt = select(Note).where(Note.id == note_id)
    result = await session.execute(stmt)
    note = result.scalar_one_or_none()
    
    if not note:
        # CATASTROPHIC BUG: Raising HTTPException bypasses session.close()!
        # The database connection is leaked permanently in 'idle in transaction'.
        raise HTTPException(status_code=404, detail="Note not found")
        
    await session.close()
    return note
```

#### Pattern 2: The Shared Global Session Anti-Pattern
Some developers attempt to share a single global `session` variable across all routes. Under concurrent requests, multiple tasks interleave transaction commands (`BEGIN`, `COMMIT`, `ROLLBACK`) on the exact same connection, corrupting transaction state and throwing concurrency errors (`InterfaceError: cannot perform operation: another operation is in progress`).

### 2.2 Telemetry Fingerprint (How to Spot It)

1. **PostgreSQL Connection Telemetry:**
   ```sql
   SELECT state, count(*) FROM pg_stat_activity GROUP BY state;
   ```
   * **Output under load:**
     ```
              state          | count 
     ------------------------+-------
      idle                   |     1
      idle in transaction    |    15  <-- LEAK DETECTED!
      active                 |     0
     ```
   * `idle in transaction` count climbs steadily and never drops, even when client traffic stops.

2. **FastAPI Application Logs:**
   ```
   sqlalchemy.exc.TimeoutError: QueuePool limit of size 10 overflow 5 reached, connection timed out, timeout 30.00
   ```

3. **User Impact:**
   * After ~50 failed requests, **every single endpoint** (even unrelated health checks that touch the DB) returns HTTP 500 Internal Server Error.

---

## 3. Checkpoint B: The Fix (Production-Grade State)

### 3.1 Architectural Solution
In FastAPI, sessions must always be managed using **Dependency Injection with an Asynchronous Generator (`yield`) wrapped in an `async with` context manager**.

This pattern guarantees that:
1. Every incoming HTTP request gets its own isolated session.
2. If the endpoint succeeds, changes are committed.
3. If an exception occurs (or if the client disconnects halfway through), the transaction is cleanly rolled back.
4. In all circumstances (`finally`), the connection is returned to the pool.

```python
# Fixed Implementation: Resilient Dependency Injection
from typing import AsyncGenerator
from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select
from .models import Note

engine = create_async_engine(
    "postgresql+asyncpg://user:pass@localhost:5432/notes_db",
    pool_size=30,
    max_overflow=20,
    pool_pre_ping=True,      # Tests connection liveness before checkout
    pool_timeout=10.0,       # Fail fast instead of hanging requests for 30s
    pool_recycle=1800        # Reconnect stale sockets every 30 minutes
)
AsyncSessionLocal = async_sessionmaker(
    bind=engine, 
    class_=AsyncSession, 
    expire_on_commit=False,
    autoflush=False
)

app = FastAPI()

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

@app.get("/notes/{note_id}")
async def get_note_safe(note_id: int, session: AsyncSession = Depends(get_db)):
    stmt = select(Note).where(Note.id == note_id)
    result = await session.execute(stmt)
    note = result.scalar_one_or_none()
    
    if not note:
        # Cleanly handled: FastAPI dependency teardown still runs and releases connection!
        raise HTTPException(status_code=404, detail="Note not found")
        
    return note
```

---

## 4. Expected Performance Delta

| Metric | Checkpoint A (Pitfall) | Checkpoint B (Fixed: Dependency Generator) | Delta |
| :--- | :--- | :--- | :--- |
| **System Stability** | Crashes after ~15 404-requests | Runs indefinitely under sustained load | **100% stable** |
| **Connection Leak Rate** | +1 leaked socket per 404 error | **0 leaked connections** | Fixed |
| **Active `idle in transaction`** | Stays pinned at max pool size | **0** (connections returned instantly) | Healthy pool |
| **Throughput under Error Load** | Drops to 0 RPS (Deadlock/Exhaustion) | **4,200+ RPS** | Complete recovery |

---

## 5. Step-by-Step Lab Runbook

1. **Step 1:** Configure a small pool size in `database.py` (`pool_size=5, max_overflow=0`) to make the leak immediately visible.
2. **Step 2:** Mount the leaky endpoint `/notes/{note_id}` and request non-existent notes (`/notes/999999`) to trigger 404s.
3. **Step 3:** Start the PostgreSQL activity monitor:
   ```bash
   watch -n 1 'psql -U postgres -d notes_db -c "SELECT state, count(*) FROM pg_stat_activity GROUP BY state;"'
   ```
4. **Step 4:** Send 10 requests:
   ```bash
   for i in {1..10}; do curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/notes/999999; done
   ```
5. **Step 5:** Observe: After 5 requests, `idle in transaction` reaches 5, and subsequent requests hang for 30s before throwing `QueuePool limit reached`.
6. **Step 6:** Replace the route with `session: AsyncSession = Depends(get_db)`.
7. **Step 7:** Re-run the loop with 1,000 requests. Verify that `idle in transaction` remains at 0 and all requests complete in under 5ms.

---

## 6. Senior Backend Interview Talking Points

* **`idle` vs `idle in transaction`:** Be ready to explain the profound difference. An `idle` connection is harmless (it's sitting in the pool waiting for work). An `idle in transaction` connection holds open locks, prevents PostgreSQL `VACUUM` from cleaning dead row versions, and wastes engine memory.
* **The `pool_pre_ping=True` Parameter:** Why is it critical in production? In cloud environments (AWS RDS, Kubernetes), firewalls or load balancers silently drop idle TCP sockets after 5–15 minutes. `pool_pre_ping` emits a lightweight `SELECT 1` when checking out a connection, discarding dead sockets transparently before executing application queries.
* **Session Scope vs Request Scope:** Sessions should never outlive the HTTP request. Always bind session lifecycle to the ASGI request lifecycle using dependencies.
