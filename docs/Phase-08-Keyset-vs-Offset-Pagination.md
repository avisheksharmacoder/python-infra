# Phase 8: Keyset (Cursor) vs. `OFFSET` Pagination

---

## 1. Executive Summary & Concept

### 1.1 The Layman's Explanation
Imagine a massive 100,000-page historical encyclopedia. You want to read **Page 5,000**.

* **The `OFFSET` Approach:**
  You open the encyclopedia to Page 1. You count: *"Page 1, Page 2, Page 3..."* all the way to Page 4,999. You throw every single one of those 4,999 pages in the shredder without reading them. Finally, you read Page 5,000. 
  When you want to read Page 5,001, you don't start from where you were—you go back to Page 1 and count 5,000 pages all over again! The deeper you scroll into the book, the longer you spend counting and shredding pages.
* **The Keyset (Cursor) Approach:**
  You place a bookmark at the exact sentence where you stopped reading. To get the next page, you open directly to the bookmark and immediately read the next 20 lines. Whether you are on Page 1 or Page 5,000, it takes less than 1 second.

In modern web applications (social media feeds, activity logs, product catalogs), using `OFFSET` pagination causes database servers to grind to a halt as users scroll deeper.

### 1.2 The Technical Reality & Mechanics
Almost all beginner REST APIs use `LIMIT` and `OFFSET`:
```sql
SELECT * FROM notes ORDER BY id DESC LIMIT 20 OFFSET 50000;
```

```
PostgreSQL Execution of OFFSET 50000 LIMIT 20:
[Row 1] [Row 2] [Row 3] ... [Row 50,000] | [Row 50,001 ... Row 50,020]
|<---------- SCANNED & DISCARDED ------->| |<--- RETURNED TO CLIENT -->|
(The database reads 50,020 rows off disk, sorts them, and discards 50,000 of them!)
```

* PostgreSQL does **not** have an internal pointer that jumps directly to the 50,000th row.
* Even if an index exists on `id`, the storage engine must traverse the index tree, inspect visibility maps for MVCC (Multi-Version Concurrency Control), track 50,000 index tuples, and discard them in memory.
* As `OFFSET` increases, **latency scales linearly $O(N)$**. At high offsets, a single pagination request can consume hundreds of megabytes of buffer memory and take hundreds of milliseconds.
* **The Data Drift Bug:** If a new note is inserted while a user is paginating, rows shift positions. The user sees duplicate records on subsequent pages or misses records entirely.

---

## 2. Checkpoint A: The Pitfall (Degraded State)

### 2.1 Degraded Code Pattern
Classic page/offset pagination query:

```python
# Degraded Implementation: OFFSET Pagination
from fastapi import FastAPI, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from .database import get_db
from .models import Note

app = FastAPI()

@app.get("/notes/offset")
async def list_notes_offset_pitfall(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db)
):
    offset = (page - 1) * page_size
    # DANGEROUS AT SCALE: Scans and discards 'offset' rows
    stmt = (
        select(Note)
        .order_by(Note.id.desc())
        .offset(offset)
        .limit(page_size)
    )
    result = await session.execute(stmt)
    return result.scalars().all()
```

### 2.2 Telemetry Fingerprint (How to Spot It)

1. **`EXPLAIN (ANALYZE, BUFFERS)` Analysis at Deep Offsets:**
   ```sql
   EXPLAIN (ANALYZE, BUFFERS) 
   SELECT * FROM notes ORDER BY id DESC LIMIT 20 OFFSET 50000;
   ```
   * **Output:**
     ```text
     Limit  (cost=3420.12..3421.49 rows=20 width=142) (actual time=242.110..242.185 rows=20 loops=1)
       Buffers: shared hit=4210 read=1240
       ->  Index Scan Backward using notes_pkey on notes  (cost=0.42..6840.24 rows=100000 width=142) 
           (actual time=0.042..238.910 rows=50020 loops=1)
     ```
   * **The telltale sign:** `rows=50020 loops=1` under the index scan. The engine had to read 50,020 rows just to give you 20.
   * Execution time is **~242ms** for a single query!

2. **Latency Degradation Curve:**
   * `page=1` (`OFFSET 0`): **1.2ms**
   * `page=500` (`OFFSET 10000`): **48ms**
   * `page=2500` (`OFFSET 50000`): **245ms**
   * `page=4500` (`OFFSET 90000`): **430ms**

---

## 3. Checkpoint B: The Fix (Production-Grade State)

### 3.1 Architectural Solution
Switch to **Keyset Pagination (Cursor-Based Pagination)**.

Instead of specifying an offset number, the client sends the identifier of the last record they received (`cursor` or `last_seen_id`). The database uses a standard B-Tree index seek:
```sql
SELECT * FROM notes 
WHERE id < :last_seen_id 
ORDER BY id DESC 
LIMIT 20;
```

```
B-Tree Index Seek on id:
                      [ Root Node ]
                     /             \
            [ Node 1-50k ]     [ Node 50k-100k ]
                                       \
                            [ Jump directly to ID 50000 ]
                            [ Read next 20 rows ] -> DONE!
(PostgreSQL reads exactly 20 rows. Zero rows discarded!)
```

### 3.2 Fixed Code Implementation

```python
# Fixed Implementation: Keyset (Cursor) Pagination
from typing import Optional
from fastapi import FastAPI, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from .database import get_db
from .models import Note
from pydantic import BaseModel

app = FastAPI()

class PaginatedNotesResponse(BaseModel):
    items: list[dict]
    next_cursor: Optional[int]

@app.get("/notes/cursor", response_model=PaginatedNotesResponse)
async def list_notes_cursor_fixed(
    cursor: Optional[int] = Query(None, description="The ID of the last note seen"),
    limit: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db)
):
    stmt = select(Note).order_by(Note.id.desc()).limit(limit)
    
    if cursor is not None:
        # B-Tree index seek: instant direct jump regardless of table size!
        stmt = stmt.where(Note.id < cursor)
        
    result = await session.execute(stmt)
    notes = result.scalars().all()
    
    next_cursor = notes[-1].id if len(notes) == limit else None
    
    return {
        "items": [{"id": n.id, "title": n.title} for n in notes],
        "next_cursor": next_cursor
    }
```

### 3.3 Optimized Execution Plan
Running `EXPLAIN (ANALYZE, BUFFERS)` on the keyset query:
```text
Limit  (cost=0.42..1.80 rows=20 width=142) (actual time=0.045..0.062 rows=20 loops=1)
  Buffers: shared hit=4 read=0
  ->  Index Scan Backward using notes_pkey on notes  (cost=0.42..3420.00 rows=50000 width=142) 
      (actual time=0.042..0.058 rows=20 loops=1)
      Index Cond: (id < 50000)
Total Execution Time: 0.081 ms
```
* **Buffers Read:** Dropped from 5,450 blocks to **4**.
* **Rows Scanned:** Dropped from 50,020 to **20**.
* **Execution Time:** Dropped from 242ms down to **0.08ms** (a 3,000x speedup).

---

## 4. Expected Performance Delta

| Metric | Checkpoint A (`OFFSET 50000`) | Checkpoint B (Keyset `id < 50000`) | Delta |
| :--- | :--- | :--- | :--- |
| **Execution Time** | 242.1ms | **0.08ms** | **3,000x faster** |
| **Buffer Hits / Disk I/O** | 5,450 pages | **4 pages** | **99.9% reduction** |
| **Throughput under Deep Pagination** | ~35 RPS | **4,200+ RPS** | **120x improvement** |
| **Data Consistency / Drift** | High (duplicate/missed rows) | **Zero drift** | Perfect pagination |
| **PostgreSQL Memory Consumption** | High buffer churn | **Flat baseline** | Extremely lightweight |

---

## 5. Step-by-Step Lab Runbook

1. **Step 1:** Ensure table has 100,000 notes populated.
2. **Step 2:** Profile the deep offset query in PostgreSQL:
   ```sql
   EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM notes ORDER BY id DESC LIMIT 20 OFFSET 50000;
   ```
3. **Step 3:** Profile the keyset query with the equivalent ID:
   ```sql
   EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM notes WHERE id < 50000 ORDER BY id DESC LIMIT 20;
   ```
4. **Step 4:** Benchmark the offset endpoint using `wrk`:
   ```bash
   wrk -t4 -c50 -d20s "http://localhost:8000/notes/offset?page=2500"
   ```
5. **Step 5:** Benchmark the cursor endpoint using `wrk`:
   ```bash
   wrk -t4 -c50 -d20s "http://localhost:8000/notes/cursor?cursor=50000"
   ```
6. **Step 6:** Compare the RPS and p99 latency between the two runs.

---

## 6. Senior Backend Interview Talking Points

* **When is `OFFSET` acceptable?** Only when datasets are small ($< 1,000$ rows) and users need random-access jump-to-page capabilities (e.g. an admin dashboard where a user clicks "Jump to Page 4").
* **Multi-Column Keyset Pagination:** If sorting by timestamp (`created_at`) rather than `id`, what happens when multiple notes have the exact same timestamp? A senior engineer handles ties with a composite tuple cursor:
  ```sql
  WHERE (created_at, id) < (:last_created_at, :last_id) 
  ORDER BY created_at DESC, id DESC
  ```
  Backed by a composite index on `(created_at DESC, id DESC)`.
* **Feed Stability:** Explain how keyset pagination prevents the "infinite scroll repeat bug" on mobile apps when new posts are created while the user is reading.
