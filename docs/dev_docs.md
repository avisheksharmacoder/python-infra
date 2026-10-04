# Developer Operations & Workflow Guide (`dev_docs.md`)
## Commands, Container Lifecycle, Telemetry & Load Harness Runbook

This guide contains all the operational commands required to develop, rebuild, inspect, and benchmark the application inside Docker. Keep this reference open in a terminal pane while developing.

---

## 1. Daily Development & Code Reload Workflow

Because the application code is mounted live into the container (`./v1:/app`), **you do NOT need to rebuild the Docker image for standard Python code changes** (`.py` files).

### How Code Reload Works
* **Automatic Reload (Default):** Uvicorn runs with `--reload` (using `watchfiles`). Any edit made to `main.py`, `database.py`, `dependencies.py`, or `routers/` is instantly picked up in less than 1 second.
* **Inspect Live Reloads:**
  ```bash
  # Watch backend container stdout to verify reloads
  docker compose -f v1/Docker-compose.yaml logs -f backend
  ```

---

## 2. When & How to Rebuild the Docker Image

You only need to rebuild the Docker image in two scenarios:
1. **You added or modified libraries in `requirements.txt`.**
2. **You modified `Docker.backend` or environment variables in `Docker-compose.yaml`.**

### 2.1 Fast Rebuild (With Cache)
Uses `uv`'s cached wheels to rebuild in under 5 seconds:
```bash
docker compose -f v1/Docker-compose.yaml build backend
docker compose -f v1/Docker-compose.yaml up -d backend
```

### 2.2 Clean Rebuild From Scratch (No Cache)
Forces a fresh download and installation of all libraries via `uv`:
```bash
docker compose -f v1/Docker-compose.yaml build --no-cache backend
docker compose -f v1/Docker-compose.yaml up -d --force-recreate backend
```

### 2.3 One-Liner Rebuild & Restart
```bash
docker compose -f v1/Docker-compose.yaml up -d --build --force-recreate backend
```

---

## 3. Stack Lifecycle (Start, Stop, Status)

Always execute these commands from the project root (`/home/avisheks/Documents/LLMDeploy/pauf`) or inside `v1/`:

```bash
# Start the entire stack in the background (PostgreSQL + FastAPI)
docker compose -f v1/Docker-compose.yaml up -d

# Check status of both containers (health, ports, names)
docker compose -f v1/Docker-compose.yaml ps

# Restart the FastAPI backend only (instant restart)
docker compose -f v1/Docker-compose.yaml restart backend

# Stop the stack without deleting database data
docker compose -f v1/Docker-compose.yaml stop

# Bring down containers and network (PRESERVES database volume)
docker compose -f v1/Docker-compose.yaml down

# CRITICAL WARNING: Drops containers AND deletes all database records!
# Only use if you want a complete wipe!
docker compose -f v1/Docker-compose.yaml down -v
```

---

## 4. Database Seeding & Data Management

The baseline dataset consists of **1,000 users, 20 tags, and 100,000 notes** (approx. 100 notes per user) with 200,000 tag associations.

### 4.1 Run the Seeder Inside the Container
```bash
# Seeds 1,000 users and 100,000 notes in ~9 seconds
docker compose -f v1/Docker-compose.yaml exec backend python create_load.py
```

### 4.2 Reset & Re-Seed from Scratch
```bash
# Drops existing rows and re-seeds cleanly
docker compose -f v1/Docker-compose.yaml exec backend python create_load.py --reset
```

### 4.3 Verify Table Counts via PostgreSQL
```bash
docker exec pauf_postgres psql -U postgres -d pauf_db -c "
SELECT 'users' AS tbl, count(*) FROM users 
UNION ALL 
SELECT 'notes', count(*) FROM notes 
UNION ALL 
SELECT 'tags', count(*) FROM tags 
UNION ALL 
SELECT 'note_tags', count(*) FROM note_tags;
"
```

### 4.4 Live Observability Dashboard & JSON Stats
We have built both a real-time web dashboard and a fast JSON stats endpoint:

* **Live Web Dashboard:** Open in your browser:
  [http://localhost:8000/dashboard](http://localhost:8000/dashboard) (or [http://localhost:8000/](http://localhost:8000/))
  * Displays live counters for Users, Notes (active/archived), Tags, and Relations.
  * Shows SQLAlchemy `QueuePool` telemetry (Pool Size, Checked In, Checked Out, Overflow).
  * Auto-refreshes every 2 seconds.

* **Raw JSON Stats Endpoint:**
  ```bash
  curl http://localhost:8000/v1/stats
  ```
  **Sample Output:**
  ```json
  {
    "status": "healthy",
    "counts": {
      "users": 1001,
      "notes": 100159,
      "tags": 20,
      "note_tags": 199986,
      "active_notes": 70258,
      "archived_notes": 29901
    },
    "pool": {
      "size": 20,
      "checked_in": 19,
      "checked_out": 1,
      "overflow": -19
    },
    "query_latency_ms": 12.4
  }
  ```

---

## 5. Real-Time Telemetry & Observability Watchers

During any load test or phase lab, open **3 terminal panes** to observe the internal mechanics of Python and PostgreSQL in real time.

### Terminal Pane 1: Live JSONL Request Stream
Inspects incoming requests, status codes, execution duration, and correlation IDs:
```bash
# Raw stream
tail -f v1/logs/app.jsonl

# Pretty-printed stream with jq (if jq is installed)
tail -f v1/logs/app.jsonl | jq -c '{time: .timestamp, method: .method, path: .path, status: .status_code, ms: .duration_ms}'
```

### Terminal Pane 2: Database Connection States & Pool Contention
Monitors how many connections are active, idle, or stuck in `idle in transaction` (connection leaks):
```bash
watch -n 1 'docker exec pauf_postgres psql -U postgres -d pauf_db -c "
SELECT state, count(*) 
FROM pg_stat_activity 
GROUP BY state;
"'
```

### Terminal Pane 3: Slow Queries & Call Counts (`pg_stat_statements`)
Shows which SQL queries are consuming the most execution time and how many times they were called:
```bash
watch -n 1 'docker exec pauf_postgres psql -U postgres -d pauf_db -c "
SELECT 
    left(query, 65) AS query_snippet, 
    calls, 
    round(mean_exec_time::numeric, 2) AS mean_ms, 
    rows 
FROM pg_stat_statements 
WHERE query NOT LIKE \"%pg_stat%\"
ORDER BY mean_exec_time DESC 
LIMIT 5;
"'
```

### Reset Query Statistics Between Test Runs
Before running a new phase benchmark, always reset `pg_stat_statements` so previous metrics don't pollute the new test:
```bash
docker exec pauf_postgres psql -U postgres -d pauf_db -c "SELECT pg_stat_statements_reset();"
```

---

## 6. Running Load Tests with `wrk`

### 6.1 Multi-Action Load Test (`workload.lua`)
Simulates 1,000 concurrent users performing a 60/20/10/7/3 distribution of reads, creates, updates, and deletes:

```bash
# Standard 30-second benchmark (8 threads, 200 concurrent connections)
wrk -t8 -c200 -d30s -s v1/workload.lua http://localhost:8000

# Quick 10-second smoke test
wrk -t4 -c50 -d10s -s v1/workload.lua http://localhost:8000
```

### 6.2 Target a Specific Endpoint
When testing a specific phase pitfall or fix in isolation:
```bash
# Example: Testing User Notes list
wrk -t4 -c100 -d15s http://localhost:8000/v1/users/42/notes

# Example: Testing Healthcheck & DB ping latency under load
wrk -t4 -c100 -d15s http://localhost:8000/v1/health
```

---

## 7. Diagnostics & Troubleshooting Runbook

### Check Application Health & Connection Pool Metrics
```bash
curl -i http://localhost:8000/v1/health
```
**Expected Output:**
```json
{
  "status": "healthy",
  "database": "connected",
  "latency_ms": 2.15,
  "pool": {
    "size": 20,
    "checked_in": 19,
    "checked_out": 1,
    "overflow": -19
  }
}
```

### Interactive Shell Inside Containers
```bash
# Open bash shell inside the backend container
docker compose -f v1/Docker-compose.yaml exec backend bash

# Open direct psql shell inside the database container
docker exec -it pauf_postgres psql -U postgres -d pauf_db
```

### Check Container Resource Usage (CPU & Memory)
To verify if Python is pinning a single core (Phase 10) or if PostgreSQL is using high CPU:
```bash
docker stats pauf_backend pauf_postgres
```
