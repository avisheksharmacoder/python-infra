# Phase 7: Deadlocks & Write Concurrency (Row Contention)

---

## 1. Executive Summary & Concept

### 1.1 The Layman's Explanation
Imagine a shared bank account owned by two business partners. The account currently has **$100**.

At the exact same millisecond:
* Partner A deposits **$50** via their phone app.
* Partner B deposits **$50** at an ATM.

**The Catastrophic Bug (Lost Update):**
1. Partner A's phone reads the balance: **$100**.
2. Partner B's ATM reads the balance: **$100**.
3. Partner A adds $50 in their head ($150) and saves **$150** to the database.
4. One millisecond later, Partner B adds $50 in their head ($150) and saves **$150** to the database.

The final balance in the bank is **$150**. Partner A's $50 deposit literally vanished into thin air!

**The Deadlock Disaster:**
Now imagine Partner A locks Box 1 and needs Box 2. Partner B locks Box 2 and needs Box 1. Both stand frozen staring at each other forever. Neither will back down. In PostgreSQL, the engine detects this standstill and terminates one of them with an error: `deadlock detected`.

### 1.2 The Technical Reality & Mechanics
High-concurrency write operations frequently update shared state (user quotas, follower counts, inventory levels, wallet balances).

```
Timeline: Concurrent Read-Modify-Write (Lost Update)
Transaction 1 (Worker A)               Transaction 2 (Worker B)
------------------------               ------------------------
BEGIN;                                 BEGIN;
SELECT count FROM users (gets 10)       
                                       SELECT count FROM users (gets 10)
count = 10 + 1 (in Python)
                                       count = 10 + 1 (in Python)
UPDATE users SET count = 11;
COMMIT;
                                       UPDATE users SET count = 11; <-- OVERWRITES A!
                                       COMMIT;
(Actual total operations: 2. Recorded count: 11 instead of 12!)
```

```
Timeline: The Deadlock Cycle (Lock Contention)
Transaction 1                          Transaction 2
------------------------               ------------------------
BEGIN;                                 BEGIN;
UPDATE notes WHERE id = 10; (locks 10) 
                                       UPDATE notes WHERE id = 20; (locks 20)
UPDATE notes WHERE id = 20;            
(Waits for Tx 2 to release 20...)      UPDATE notes WHERE id = 10;
                                       (Waits for Tx 1 to release 10...)
                                       
*** DEADLOCK DETECTED! PostgreSQL aborts one transaction after deadlock_timeout ***
```

* Under PostgreSQL's default `READ COMMITTED` isolation level, a plain `SELECT` statement does not lock the row.
* Performing arithmetic inside Python (`user.count = user.count + 1`) introduces a time window between the read and the write where other transactions read stale state.
* When updating multiple rows, if transactions acquire locks in different orders (Tx 1 updates A then B; Tx 2 updates B then A), a circular wait condition is formed. PostgreSQL's internal deadlock detector runs every `deadlock_timeout` (default 1 second), forcibly aborting one transaction with an exception.

---

## 2. Checkpoint A: The Pitfall (Degraded State)

### 2.1 Degraded Code Pattern
Application-level Read-Modify-Write pattern for incrementing counters:

```python
# Degraded Implementation: Race Condition & Lost Updates
from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from .database import get_db
from .models import User

app = FastAPI()

@app.post("/users/{user_id}/increment-notes-pitfall")
async def increment_notes_pitfall(user_id: int, session: AsyncSession = Depends(get_db)):
    # Step 1: Plain SELECT without row lock
    stmt = select(User).where(User.id == user_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    # Step 2: In-memory Python calculation (VULNERABLE TO RACE CONDITIONS)
    user.note_count = user.note_count + 1
    
    # Step 3: Write back
    await session.commit()
    return {"user_id": user.id, "note_count": user.note_count}
```

### 2.2 Telemetry Fingerprint (How to Spot It)

1. **Data Consistency Audit:**
   * Fire 500 concurrent requests against user 42:
     ```bash
     wrk -t4 -c50 -d10s -s post_increment.lua http://localhost:8000/users/42/increment-notes-pitfall
     ```
   * Query the resulting database count:
     ```sql
     SELECT id, note_count FROM users WHERE id = 42;
     ```
   * **Result:** 500 HTTP 200 responses were returned, but `note_count` only increased by **~185**! More than 60% of updates were silently lost!

2. **Lock Contention in `pg_stat_activity`:**
   ```sql
   SELECT pid, wait_event_type, wait_event, query 
   FROM pg_stat_activity 
   WHERE wait_event IS NOT NULL;
   ```
   * Sessions pile up with `wait_event_type = 'Lock'` and `wait_event = 'tuple'`.

3. **Deadlock Errors in PostgreSQL Logs:**
   ```text
   ERROR: deadlock detected
   DETAIL: Process 4120 waits for ShareLock on transaction 88921; blocked by process 4121.
   Process 4121 waits for ShareLock on transaction 88920; blocked by process 4120.
   HINT: See server log for query details.
   ```

---

## 3. Checkpoint B: The Fix (Production-Grade State)

### 3.1 Architectural Solutions

#### Solution 1: Single Atomic SQL Update (Recommended: Highest Throughput)
Push the computation entirely into the database engine. In PostgreSQL, single SQL statements are inherently atomic and thread-safe:

```python
# Fixed Implementation 1: Atomic SQL Update (Zero Race Conditions, No Lost Updates)
from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import update
from .database import get_db
from .models import User

app = FastAPI()

@app.post("/users/{user_id}/increment-notes-atomic")
async def increment_notes_atomic(user_id: int, session: AsyncSession = Depends(get_db)):
    # Atomic evaluation inside PostgreSQL: UPDATE users SET note_count = note_count + 1
    stmt = (
        update(User)
        .where(User.id == user_id)
        .values(note_count=User.note_count + 1)
        .returning(User.note_count)
    )
    result = await session.execute(stmt)
    new_count = result.scalar_one_or_none()
    
    if new_count is None:
        raise HTTPException(status_code=404, detail="User not found")
        
    await session.commit()
    return {"user_id": user_id, "note_count": new_count}
```

#### Solution 2: Pessimistic Row Locking (`SELECT ... FOR UPDATE`)
When complex business logic in Python must inspect the row before updating (e.g. checking whether `user.balance >= withdrawal_amount`), lock the row exclusively:

```python
# Fixed Implementation 2: Pessimistic Locking
from sqlalchemy import select

@app.post("/users/{user_id}/safe-withdraw")
async def safe_withdraw(user_id: int, amount: int, session: AsyncSession = Depends(get_db)):
    # SELECT ... FOR UPDATE acquires an exclusive row-level lock
    stmt = select(User).where(User.id == user_id).with_for_update()
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    
    if user.balance < amount:
        raise HTTPException(status_code=400, detail="Insufficient funds")
        
    user.balance -= amount
    await session.commit() # Lock released at commit
    return {"balance": user.balance}
```

#### Solution 3: Eliminating Deadlocks with Ordered Locking
If a transaction must update multiple records (e.g. transferring notes between User A and User B), **always sort the IDs and acquire locks in identical ascending order**:
```python
# Guaranteed deadlock avoidance:
ordered_ids = sorted([user_a_id, user_b_id])
for uid in ordered_ids:
    await session.execute(select(User).where(User.id == uid).with_for_update())
```

---

## 4. Expected Performance Delta

| Metric | Checkpoint A (Pitfall) | Checkpoint B (Fixed: Atomic SQL) | Delta |
| :--- | :--- | :--- | :--- |
| **Data Integrity / Accuracy** | 40%–60% data loss | **100% Exact Accuracy** | **Flawless consistency** |
| **Deadlock Exceptions** | Common under high load | **0 Deadlocks** | Completely eliminated |
| **Throughput (RPS)** | ~450 RPS (Lock contention) | **3,900+ RPS** | **8.6x improvement** |
| **Row Lock Wait Time** | High (`Lock:tuple` wait events) | **Zero wait events** | Instant execution |

---

## 5. Step-by-Step Lab Runbook

1. **Step 1:** Reset user 42's note count:
   ```sql
   UPDATE users SET note_count = 0 WHERE id = 42;
   ```
2. **Step 2:** Mount the pitfall endpoint.
3. **Step 3:** Send exactly 500 concurrent increment requests using Python script or `wrk`.
4. **Step 4:** Query the database:
   ```sql
   SELECT note_count FROM users WHERE id = 42;
   ```
5. **Step 5:** Observe the shortfall (counter will be around 170–220 instead of 500).
6. **Step 6:** Mount the atomic update endpoint `update(User).values(note_count=User.note_count + 1)`.
7. **Step 7:** Reset counter to 0 and re-run the exact same 500 requests.
8. **Step 8:** Verify that `SELECT note_count FROM users WHERE id = 42;` returns **exactly 500**.

---

## 6. Senior Backend Interview Talking Points

* **Isolation Levels vs Explicit Locks:** In interview discussions, explain why changing isolation levels to `SERIALIZABLE` is not always the best answer. Under high write contention, `SERIALIZABLE` throws serialization errors (`40001: could not serialize access due to concurrent update`), forcing client-side retries. Atomic SQL updates or `SELECT FOR UPDATE` handle contention gracefully without retries.
* **Deadlock Avoidance Rule:** A deadlock can only occur if there is a cyclical dependency in resource acquisition. Enforcing a global deterministic ordering (e.g. always lock resource with lowest ID first) mathematically guarantees that a cycle can never form.
* **`SKIP LOCKED` for Job Queues:** When multiple workers poll a database table for tasks (e.g. an async worker queue in Postgres), mention `SELECT ... FOR UPDATE SKIP LOCKED`. It allows workers to grab the next available unlocked row without blocking each other.
