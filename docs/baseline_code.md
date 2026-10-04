# Phase 0: Baseline Substrate & Infrastructure Implementation Plan
## Architecture, Schemas, Containerization & Load Harness

---

## Executive Overview
Phase 0 establishes the entire operational foundation (The Baseline Substrate) for the 10-phase performance engineering curriculum. Everything is executed **exclusively inside Docker containers**, using:
* **Python Runtime:** `python:3.14-slim`
* **PostgreSQL Engine:** `postgres:latest` with `pg_stat_statements` preloaded
* **Web Framework:** FastAPI with Uvicorn/Gunicorn
* **Database Driver & ORM:** `asyncpg` with SQLAlchemy 2.0 (Async Engine & Session)
* **Structured Telemetry:** `loguru` streaming JSONL format to disk
* **Load Harness:** Multi-action `wrk` Lua script simulating 1,000 concurrent users

This plan is structured into **6 discrete sections** to be reviewed and implemented sequentially.

---

## Section 1: Container Infrastructure & Docker Orchestration

### 1.1 Goal & Scope
Provide a containerized environment where all dependencies, system libraries, database engines, and runtime volumes are configured cleanly without touching the host system's local Python environment.

### 1.2 Component Specifications

#### A. `Docker.backend` (Dockerfile)
* **Base Image:** `python:3.14-slim`
* **System Packages:** `curl`, `gcc`, `build-essential`, `libpq-dev` (required to compile C extensions like `psycopg2-binary` for Phase 1 comparisons and `uvloop`).
* **Working Directory:** `/app`
* **Volume Mount Targets:**
  * `./v1:/app` (live code mounting for iterative phase changes)
  * `./v1/logs:/app/logs` (host-accessible structured JSONL log files)
* **Port Exposure:** `8000`
* **Default Entrypoint:** Uvicorn running with ASGI reload enabled:
  ```bash
  uvicorn main:app --host 0.0.0.0 --port 8000 --reload
  ```

#### B. `Docker-compose.yaml`
Orchestrates two interdependent services:
1. **`db` (PostgreSQL):**
   * **Image:** `postgres:latest`
   * **Environment:**
     * `POSTGRES_USER=postgres`
     * `POSTGRES_PASSWORD=postgres`
     * `POSTGRES_DB=pauf_db`
   * **Command Flag Overrides:**
     ```yaml
     command: >
       postgres 
       -c shared_preload_libraries=pg_stat_statements
       -c pg_stat_statements.track=all
       -c pg_stat_statements.max=10000
       -c max_connections=100
       -c shared_buffers=256MB
     ```
   * **Healthcheck:** Uses `pg_isready -U postgres -d pauf_db` (interval 2s, timeout 3s, retries 5).
   * **Volumes:** Named volume `pgdata:/var/lib/postgresql/data` for persistence across container restarts.
   * **Ports:** Exposes `5432:5432` for direct host-level `psql` telemetry inspections.

2. **`backend` (FastAPI Application):**
   * **Build Context:** Built using `Docker.backend`.
   * **Dependencies:** `depends_on` configured with `condition: service_healthy` on `db` so FastAPI never attempts to connect before PostgreSQL is listening on TCP sockets.
   * **Environment Variables:**
     * `DATABASE_URL=postgresql+asyncpg://postgres:postgres@db:5432/pauf_db`
     * `SYNC_DATABASE_URL=postgresql+psycopg2://postgres:postgres@db:5432/pauf_db` (for Phase 1 comparison)
     * `LOG_LEVEL=INFO`
     * `LOG_FILE_PATH=/app/logs/app.jsonl`
   * **Ports:** Exposes `8000:8000`.

#### C. `requirements.txt` Resolution
To satisfy all baseline and phase requirements:
```text
fastapi>=0.115.0
uvicorn[standard]>=0.32.0
gunicorn>=23.0.0
sqlalchemy>=2.0.36
asyncpg>=0.30.0
psycopg2-binary>=2.9.10
pydantic>=2.10.0
pydantic-settings>=2.6.0
loguru>=0.7.2
httpx>=0.28.0
redis>=5.2.0
greenlet>=3.1.1
```

---

## Section 2: Structured JSONL Logging with Loguru (`logger.py`)

### 2.1 Goal & Scope
Implement high-performance, non-blocking structured logging that outputs each event as an independent JSON object per line (JSONL). This allows programmatic parsing, log analysis under load, and detailed latency attribution.

### 2.2 Technical Design
* **File Location:** Mounted at `/app/logs/app.jsonl` (visible on host as `v1/logs/app.jsonl`).
* **Rotation Policy:** `500 MB` per file to avoid runaway disk usage during load testing.
* **Retention Policy:** `7 days`.
* **Compression:** `zip` on rotated archives.
* **Asynchronous Sink:** `enqueue=True` (uses a background worker thread so logging I/O never blocks the FastAPI event loop).

### 2.3 JSONL Telemetry Schema
Every line emitted to `app.jsonl` will conform to the following schema:
```json
{
  "timestamp": "2026-10-04T15:50:00.123456+00:00",
  "level": "INFO",
  "process_id": 412,
  "request_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
  "method": "GET",
  "path": "/users/42/notes",
  "status_code": 200,
  "duration_ms": 3.42,
  "client_ip": "172.20.0.1",
  "message": "Request completed successfully",
  "extra": {}
}
```

### 2.4 Uvicorn / Standard Library Interception
Configure a custom `InterceptHandler` to redirect standard Python `logging` and Uvicorn access/error logs into the Loguru JSONL pipeline, ensuring a single unified log format.

---

## 3. Database Engine, Connection Pooling & Unified Schema (`database.py`)

### 3.1 Goal & Scope
Establish an optimized async SQLAlchemy 2.0 engine and define the core domain tables that support **all 10 phases** without requiring schema changes in later phases.

### 3.2 Async Engine & Pool Configuration
```python
# Target Configuration
engine = create_async_engine(
    DATABASE_URL,
    pool_size=20,            # Core persistent connection pool
    max_overflow=10,         # Maximum burst connections allowed
    pool_timeout=10.0,       # Fail fast instead of hanging client requests
    pool_pre_ping=True,      # Tests socket liveness with 'SELECT 1' before checkout
    pool_recycle=1800,       # Re-establishes stale TCP connections every 30 minutes
    echo=False               # Keep disabled under high load to avoid console I/O bottlenecks
)
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,  # Essential for async SQLAlchemy to avoid lazy reload crashes
    autoflush=False
)
```

### 3.3 Domain Schema Definitions (The 4 Unified Tables)

1. **`users` Table:**
   * `id`: `Integer`, Primary Key, autoincrement.
   * `username`: `String(50)`, unique, index=True.
   * `email`: `String(100)`, nullable=False.
   * `note_count`: `Integer`, default=0, nullable=False (Required for **Phase 7** atomic counter & lost-update tests).
   * `created_at`: `DateTime(timezone=True)`, server_default=func.now().
   * *Relationships:* `notes` (1-to-many with cascade delete).

2. **`notes` Table:**
   * `id`: `Integer`, Primary Key, autoincrement.
   * `user_id`: `Integer`, `ForeignKey("users.id", ondelete="CASCADE")`, nullable=False.
   * `title`: `String(255)`, nullable=False.
   * `content`: `Text`, nullable=False.
   * `status`: `String(50)`, default="active", nullable=False (Required for **Phase 6** composite index experiments: `active` vs `archived`).
   * `created_at`: `DateTime(timezone=True)`, server_default=func.now() (Required for **Phase 8** Keyset pagination).
   * *Relationships:* `user` (Many-to-1), `tags` (Many-to-many via `note_tags`).

3. **`tags` Table:**
   * `id`: `Integer`, Primary Key, autoincrement.
   * `name`: `String(50)`, unique=True, nullable=False.
   * *Relationships:* `notes` (Many-to-many via `note_tags`).

4. **`note_tags` Table (Association Table):**
   * `note_id`: `Integer`, `ForeignKey("notes.id", ondelete="CASCADE")`, primary_key=True.
   * `tag_id`: `Integer`, `ForeignKey("tags.id", ondelete="CASCADE")`, primary_key=True.
   * *(Required for **Phase 3** N+1 query cascades and **Phase 4** Pydantic serialization traps).*

### 3.4 Table Initialization Helper (`init_db`)
An asynchronous startup routine executing:
```python
async with engine.begin() as conn:
    await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_stat_statements;"))
    await conn.run_sync(Base.metadata.create_all)
```

---

## 4. Session Lifecycle & Request Dependencies (`dependencies.py`)

### 4.1 Goal & Scope
Provide safe, leak-proof dependency injection for database sessions across all endpoints, establishing the standard pattern used in **Phase 2 (Fix)**.

### 4.2 Async Generator Pattern
```python
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
```
* **Commit on Success:** If the route finishes without an unhandled error, changes are committed automatically.
* **Rollback on Error:** If an HTTP exception or crash occurs, the transaction is cleanly rolled back.
* **Guaranteed Return to Pool:** The `finally: await session.close()` ensures the underlying socket is returned to SQLAlchemy's `QueuePool`, preventing any connection from lingering in `idle in transaction`.

### 4.3 Contextual Request Logger Dependency
Provides routes with a bound logger instance carrying the current `request_id` for correlated tracing.

---

## 5. Application Scaffolding, Middleware & Modular APIRouters (`main.py`, `schemas.py`, `routers/`)

### 5.1 Goal & Scope
Create the core FastAPI application skeleton, validation schemas, timing middleware, and a modular router hierarchy allowing clean plugging of future phase endpoints.

### 5.2 Schemas & Data Contracts (`schemas.py`)
Using Pydantic V2 (`from_attributes = True`):
* `UserBase`, `UserCreate`, `UserResponse`
* `NoteBase`, `NoteCreate`, `NoteUpdate`, `NoteResponse`
* `TagResponse`
* `NoteWithTagsResponse`
* `HealthCheckResponse` (reports database status, pool utilization, and roundtrip ping latency)

### 5.3 Request Telemetry Middleware
An ASGI middleware in `main.py` that:
1. Reads or generates `X-Request-ID` (UUID4).
2. Captures high-resolution start time (`time.perf_counter()`).
3. Executes the request through the route handler.
4. Calculates `duration_ms`.
5. Emits a structured log line to `app.jsonl`.
6. Attaches `X-Request-ID` and `X-Process-Time` to response headers.

### 5.4 Base Router Endpoints (`routers/base.py`)
Provides the baseline endpoints that will be exercised by the load test:
* `GET /health`: Runs `SELECT 1;` and returns pool statistics (`checkedin`, `checkedout`, `overflow`).
* `GET /notes`: List notes with default pagination.
* `GET /notes/{id}`: Fetch single note details.
* `POST /notes`: Create a new note.
* `PUT /notes/{id}`: Update an existing note.
* `DELETE /notes/{id}`: Delete a note.
* `GET /users/{id}/notes`: List all notes for a specific user.

### 5.5 Phase Modular Architecture
FastAPI `include_router` setup configured to import future phase subrouters:
```python
# Routers mounted cleanly under /v1
app.include_router(base_router, prefix="/v1", tags=["Baseline"])
# Future phases plug in here:
# app.include_router(phase01_router, prefix="/v1/phase01", tags=["Phase 01"])
```

---

## 6. Seed Data Engine & High-Load Simulator (`create_load.py` & `workload.lua`)

### 6.1 Goal & Scope
Provide a fast, deterministic seeder that builds the exact dataset required for realistic benchmarking, and configure the multi-action Lua script for `wrk`.

### 6.2 The Seeder Engine (`create_load.py`)
Executable directly inside the container via:
```bash
docker compose exec backend python create_load.py
```
* **Data Volume Target:**
  * **1,000 Users:** `user_1` to `user_1000`.
  * **20 Tags:** `work`, `personal`, `urgent`, `project`, `ideas`, etc.
  * **100,000 Notes:** Exactly 100 notes per user.
    * 70% marked as `status = 'active'`.
    * 30% marked as `status = 'archived'`.
    * Distributed across the past 180 days with valid timestamps.
  * **200,000 Note-Tag Associations:** 2 random tags linked to each note.
* **Performance Mechanism:** Uses SQLAlchemy Core bulk multi-row inserts (`insert(Table).values(batch)` in batches of 5,000 rows). Populates all 100,000 rows in **under 5 seconds**.

### 6.3 The Multi-Action Load Script (`workload.lua`)
A Lua script for `wrk` implementing the realistic user distribution:
* **User ID Range:** Randomized `1` to `1,000`.
* **Note ID Range:** Randomized `1` to `100,000`.
* **Traffic Distribution:**
  * **60% `GET /v1/users/{user_id}/notes`** (List user notes)
  * **20% `GET /v1/notes/{note_id}`** (Single note detail)
  * **10% `POST /v1/notes`** (Create note with dynamic JSON payload)
  * **7% `PUT /v1/notes/{note_id}`** (Update note with dynamic JSON payload)
  * **3% `DELETE /v1/notes/{note_id}`** (Delete note)

#### Standard wrk Invocation Command:
```bash
wrk -t8 -c200 -d30s -s v1/workload.lua http://localhost:8000
```

---

## Verification & Handoff Checklist
When Section 1 through Section 6 are implemented, the environment will be verified with:
1. `docker compose up -d --build` $\rightarrow$ both containers start healthy.
2. `docker compose exec backend python create_load.py` $\rightarrow$ 1,000 users & 100,000 notes seeded.
3. `curl -i http://localhost:8000/v1/health` $\rightarrow$ returns HTTP 200 OK with pool stats.
4. `cat v1/logs/app.jsonl | head -n 5` $\rightarrow$ outputs valid JSONL logs.
5. `wrk -t4 -c50 -d10s -s v1/workload.lua http://localhost:8000` $\rightarrow$ load generator executes without fatal network errors.
