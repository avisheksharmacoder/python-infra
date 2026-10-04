# Phase 6: Missing Composite Indexes on Filtering Queries

---

## 1. Executive Summary & Concept

### 1.1 The Layman's Explanation
Imagine you are looking for a specific friend in a massive physical phone book containing 100,000 people. You are looking for: **"Alex Smith, living in Chicago, with an Active phone line."**

* **Scenario A (No Index):** You have a stack of 100,000 loose sheets of paper in random order. To find Alex, you must pick up page 1, read it, throw it down, pick up page 2, and read every single sheet in the stack. That is a **Sequential Scan (`Seq Scan`)**.
* **Scenario B (Single-Column Index on Name only):** The book is sorted by name only. You quickly flip to "Alex" and find 2,000 people named Alex. You still have to read every one of those 2,000 lines to see who lives in Chicago and has an Active phone line.
* **Scenario C (Composite Index on Name + City + Status):** The book is organized alphabetically by `(Name, City, Status)`. You flip directly to the exact page for "Alex, Chicago, Active" in 2 seconds flat. That is a **Composite Index Scan**.

When your database queries filter by multiple columns (like `user_id` AND `status`), having an index on only one column forces the database to do heavy manual filtering in memory for every single web request.

### 1.2 The Technical Reality & Mechanics
Relational queries in production APIs almost always filter by multiple attributes:
```sql
SELECT * FROM notes WHERE user_id = 42 AND status = 'active';
```

```
Table: 100,000 rows. User 42 has 5,000 notes, but only 10 are 'active'.

Without Composite Index:
+---------------------------------------------------------------------------------+
| 1. Index Scan on idx_notes_user_id -> Identifies 5,000 row pointers (TIDs)      |
| 2. Bitmap Heap Scan -> Reads disk pages to load all 5,000 full note rows        |
| 3. In-Memory Filter -> Checks status == 'active' for all 5,000 rows             |
| 4. Discards 4,990 rows and returns 10 rows                                      |
| RESULT: 5,000 disk page buffer reads, high CPU churn, 45ms latency              |
+---------------------------------------------------------------------------------+

With Composite Index on (user_id, status):
+---------------------------------------------------------------------------------+
| 1. B-Tree Root -> Intermediate Nodes -> Leaf Node (user_id=42, status='active')  |
| 2. Direct Index Scan -> Points to the exact 10 rows directly                    |
| RESULT: 3 buffer reads, 0 discarded rows, 0.12ms latency                        |
+---------------------------------------------------------------------------------+
```

* In PostgreSQL, B-Tree indexes store sorted keys.
* If a composite index is created on `(user_id, status)`, PostgreSQL navigates the balanced tree directly to the exact subset matching both keys simultaneously.
* The **Leftmost Prefix Rule:** A composite index on `(user_id, status)` can accelerate queries on `user_id` alone, and queries on `user_id AND status`. However, it cannot accelerate queries that filter on `status` alone without `user_id`.

---

## 2. Checkpoint A: The Pitfall (Degraded State)

### 2.1 Degraded Schema & Query Pattern
The table has either no index or only an index on `user_id`, while the application frequently filters by `status`:

```python
# Degraded Implementation: Filtering without composite index
from fastapi import FastAPI, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from .database import get_db
from .models import Note

app = FastAPI()

@app.get("/users/{user_id}/notes")
async def list_user_active_notes_pitfall(
    user_id: int, 
    status: str = "active", 
    session: AsyncSession = Depends(get_db)
):
    # Generates: SELECT * FROM notes WHERE user_id = :user_id AND status = :status
    stmt = (
        select(Note)
        .where(Note.user_id == user_id)
        .where(Note.status == status)
    )
    result = await session.execute(stmt)
    return result.scalars().all()
```

### 2.2 Telemetry Fingerprint (How to Spot It)

1. **`EXPLAIN (ANALYZE, BUFFERS)` Analysis:**
   ```sql
   EXPLAIN (ANALYZE, BUFFERS) 
   SELECT * FROM notes WHERE user_id = 42 AND status = 'active';
   ```
   * **Degraded Output:**
     ```text
     Bitmap Heap Scan on notes  (cost=45.12..1890.30 rows=12 width=142) (actual time=12.450..42.105 rows=10 loops=1)
       Recheck Cond: (user_id = 42)
       Filter: (status = 'active'::text)
       Rows Removed by Filter: 4990  <-- MASSIVE FILTER OVERHEAD!
       Buffers: shared hit=482 read=312
       ->  Bitmap Index Scan on idx_notes_user_id  (cost=0.00..45.12 rows=5000 width=0)
     ```
   * Notice: `Rows Removed by Filter: 4990`. The database loaded 5,000 rows off storage only to throw 4,990 of them away.

2. **`pg_stat_statements` Metrics:**
   * `mean_exec_time`: **35ms – 85ms** per query.
   * `shared_blks_read`: Very high disk buffer read counts.

3. **Host Resource Utilization:**
   * Under 200 concurrent users (`wrk`), PostgreSQL container CPU hits **100%**.
   * FastAPI requests begin to queue and time out.

---

## 3. Checkpoint B: The Fix (Production-Grade State)

### 3.1 Architectural Solution
Add a targeted **Composite B-Tree Index** on both `user_id` and `status`.

In high-concurrency production databases, always create indexes using the `CONCURRENTLY` keyword so that table read and write operations are not locked during index creation:

```sql
-- Production-Grade Migration
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_notes_user_id_status 
ON notes (user_id, status);
```

In SQLAlchemy models, declare the composite index in the table `__table_args__`:

```python
# Fixed Model Definition
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Index
from sqlalchemy.orm import declarative_base

Base = declarative_base()

class Note(Base):
    __tablename__ = "notes"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String(255), nullable=False)
    content = Column(Text, nullable=False)
    status = Column(String(50), nullable=False, default="active")

    __table_args__ = (
        # Composite B-Tree index on (user_id, status)
        Index("idx_notes_user_id_status", "user_id", "status"),
    )
```

### 3.2 Optimized Execution Plan
Running `EXPLAIN (ANALYZE, BUFFERS)` after creating the composite index:
```text
Index Scan using idx_notes_user_id_status on notes  (cost=0.42..8.45 rows=10 width=142) (actual time=0.082..0.095 rows=10 loops=1)
  Index Cond: ((user_id = 42) AND (status = 'active'::text))
  Buffers: shared hit=3 read=0
Total Execution Time: 0.121 ms
```
* **Buffers Read:** Dropped from 794 down to **3**.
* **Rows Removed by Filter:** Dropped from 4,990 down to **0**.
* **Execution Time:** Dropped from 42ms down to **0.12ms**.

---

## 4. Expected Performance Delta

| Metric | Checkpoint A (Pitfall) | Checkpoint B (Fixed: Composite Index) | Delta |
| :--- | :--- | :--- | :--- |
| **Query Execution Time** | 42.1ms | **0.12ms** | **350x faster** |
| **Shared Buffer Reads** | 794 blocks | **3 blocks** | **99.6% I/O reduction** |
| **PostgreSQL CPU under Load** | Pinned at 100% | **< 12%** | **Massive CPU headroom** |
| **FastAPI Throughput (RPS)** | ~280 RPS | **3,800+ RPS** | **13.5x improvement** |
| **p99 Latency under 200 conns**| 1,450ms | **18ms** | **98.7% reduction** |

---

## 5. Step-by-Step Lab Runbook

1. **Step 1:** Ensure table has 100,000 rows seeded. Drop the composite index if it exists:
   ```sql
   DROP INDEX IF EXISTS idx_notes_user_id_status;
   ```
2. **Step 2:** Profile the slow query plan:
   ```sql
   EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM notes WHERE user_id = 42 AND status = 'active';
   ```
3. **Step 3:** Run `wrk` with 200 connections:
   ```bash
   wrk -t8 -c200 -d30s "http://localhost:8000/users/42/notes?status=active"
   ```
4. **Step 4:** Watch Postgres CPU hit 100% using `top` or `docker stats`.
5. **Step 5:** Create the composite index:
   ```sql
   CREATE INDEX CONCURRENTLY idx_notes_user_id_status ON notes (user_id, status);
   ```
6. **Step 6:** Re-run `EXPLAIN (ANALYZE, BUFFERS)` to confirm the `Index Scan`.
7. **Step 7:** Re-run the `wrk` benchmark. Observe Postgres CPU relax to ~10% and RPS jump to nearly 4,000 RPS.

---

## 6. Senior Backend Interview Talking Points

* **Column Order in Composite Indexes:** The leftmost prefix rule is crucial. If your queries filter on `WHERE user_id = ? AND status = ?`, an index on `(user_id, status)` works. The column with the highest selectivity (cardinality) should typically go first, but query pattern frequency dictates order.
* **Covering Indexes (`INCLUDE` clause):** If queries only select specific columns (e.g. `SELECT id, title FROM notes WHERE user_id = ? AND status = ?`), explain how PostgreSQL's `INCLUDE (id, title)` enables an **Index-Only Scan**, meaning the database never even touches the main table heap pages.
* **Index Write Overhead:** Senior engineers know that indexes are not free. Every `INSERT`, `UPDATE`, and `DELETE` must write to every index on the table. Adding 15 indexes to a table severely degrades write throughput.
