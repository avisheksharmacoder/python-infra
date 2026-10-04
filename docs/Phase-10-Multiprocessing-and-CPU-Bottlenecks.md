# Phase 10: Multiprocessing, Event Loop Sizing & CPU Saturation

---

## 1. Executive Summary & Concept

### 1.1 The Layman's Explanation
Imagine an 8-lane superhighway leading to a massive toll bridge. The bridge is capable of handling 50,000 cars per hour.

However, the toll authority only hired **1 single toll collector** and stationed him in Lane 1. Lanes 2 through 8 are barricaded shut with orange cones. 

Even if that one toll collector is a superhero who processes a car every 2 seconds, cars back up for 10 miles down the highway. The drivers honk in anger, and the bridge is completely empty beyond the toll booth. 

Now imagine opening all 8 lanes and placing a toll collector in each lane. The traffic jam instantly evaporates, and the capacity of the bridge is fully realized.

In backend systems, modern servers have 4, 8, 16, or 64 CPU cores. If you start your FastAPI app with a standard single-worker command (`uvicorn main:app`), Python only runs on **1 single CPU core**. The remaining 7 or 15 cores sit completely idle while your server chokes under traffic.

### 1.2 The Technical Reality & Mechanics
Python (specifically CPython) contains the **Global Interpreter Lock (GIL)**.

```
Multi-Core Host Server (e.g. 4 CPU Cores)

Running Single Worker: uvicorn main:app (PITFALL)
+-------------------+-------------------+-------------------+-------------------+
|      Core 0       |      Core 1       |      Core 2       |      Core 3       |
|  [Uvicorn Worker] |      (IDLE)       |      (IDLE)       |      (IDLE)       |
|   100% CPU LOAD   |      0% LOAD      |      0% LOAD      |      0% LOAD      |
+-------------------+-------------------+-------------------+-------------------+
Throughput Ceiling: ~3,800 RPS (Single Core Saturated!)
```

```
Running Process Cluster: gunicorn -w 4 -k uvicorn.workers.UvicornWorker (FIX)
+-------------------+-------------------+-------------------+-------------------+
|      Core 0       |      Core 1       |      Core 2       |      Core 3       |
| [Uvicorn Worker 1]| [Uvicorn Worker 2]| [Uvicorn Worker 3]| [Uvicorn Worker 4]|
|    90% CPU LOAD   |    90% CPU LOAD   |    90% CPU LOAD   |    90% CPU LOAD   |
+-------------------+-------------------+-------------------+-------------------+
Throughput: ~14,500+ RPS (Linear Multi-Core Saturation!)
```

* An `asyncio` event loop is strictly single-threaded. It achieves concurrency through non-blocking I/O multiplexing (`epoll`), not parallelism.
* Even though database calls are non-blocking, web servers perform significant **CPU-bound work**:
  1. Parsing HTTP headers and TLS handshakes.
  2. Deserializing incoming JSON strings into Python dictionaries.
  3. Validating schemas through Pydantic V2.
  4. Serializing internal Python objects into JSON response byte streams.
* When that single CPU core reaches 100% utilization, the event loop can no longer process network socket events in a timely manner.
* Throughput hits a hard wall. Increasing client concurrency from 200 to 1,000 connections does not increase RPS; it only inflates latency.

---

## 2. Checkpoint A: The Pitfall (Degraded State)

### 2.1 Degraded Deployment Configuration
Deploying FastAPI using a single Uvicorn process in Docker or production:

```dockerfile
# Degraded Dockerfile CMD
# Running single process on a multi-core container
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Or running directly:
```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

### 2.2 Telemetry Fingerprint (How to Spot It)

1. **Host CPU Core Monitoring (`mpstat` or `htop`):**
   ```bash
   mpstat -P ALL 1
   ```
   * **Output under 500 concurrent connections:**
     ```text
     CPU    %usr   %sys  %iowait  %idle
     all    25.1    4.2     0.1   70.6   <-- 70% of server CPU is WASTED!
       0    98.2    1.8     0.0    0.0   <-- Core 0 pinned at 100%
       1     0.2    0.1     0.0   99.7
       2     0.1    0.1     0.0   99.8
       3     0.3    0.1     0.0   99.6
     ```
   * One core is on fire; all other cores are asleep.

2. **Throughput Plateau in `wrk`:**
   ```bash
   wrk -t8 -c200 -d30s http://localhost:8000/notes/1
   # Yields ~3,800 RPS

   wrk -t8 -c500 -d30s http://localhost:8000/notes/1
   # Yields ~3,850 RPS (RPS did not increase, but p99 latency doubled from 25ms to 58ms!)
   ```

3. **Database Health:**
   * PostgreSQL CPU is sitting comfortably at 20%. The bottleneck is 100% inside Python's single-core CPU ceiling.

---

## 3. Checkpoint B: The Fix (Production-Grade State)

### 3.1 Architectural Solution
Deploy a **Master-Worker Process Cluster** using Gunicorn with Uvicorn worker classes or native Uvicorn multi-worker mode.

```
                    Incoming HTTP Traffic (:8000)
                                 │
                   Linux Kernel (SO_REUSEPORT)
         ┌───────────────┬───────────────┬───────────────┐
         ▼               ▼               ▼               ▼
   [ Worker 1 ]    [ Worker 2 ]    [ Worker 3 ]    [ Worker 4 ]
   (Process 101)   (Process 102)   (Process 103)   (Process 104)
     Core 0          Core 1          Core 2          Core 3
```

Under this model:
* A master process forks $N$ independent worker processes (typically $N = \text{available CPU cores}$).
* Each worker has its own independent Python interpreter, its own memory heap, its own GIL, and its own `uvloop` event loop.
* The Linux kernel distributes incoming TCP connections across workers using the `SO_REUSEPORT` socket option.

### 3.2 Fixed Production Configuration

#### Approach 1: Gunicorn with Uvicorn Workers (Industry Standard)
Gunicorn acts as a battle-tested process supervisor, automatically restarting crashed workers, managing heartbeats, and handling graceful zero-downtime reloads:

```bash
gunicorn main:app \
    --workers 4 \
    --worker-class uvicorn.workers.UvicornWorker \
    --bind 0.0.0.0:8000 \
    --timeout 60 \
    --keep-alive 5 \
    --backlog 2048 \
    --access-logfile - \
    --error-logfile -
```

#### Approach 2: Native Uvicorn Workers
```bash
uvicorn main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --workers 4 \
    --loop uvloop \
    --http httptools
```

#### Tuning Database Connection Pools for Multi-Worker Deployments
**CRITICAL WARNING:** When running multiple worker processes, connection pools are **multiplied by the number of workers**:
$$\text{Total DB Connections} = \text{Workers} \times (\text{pool\_size} + \text{max\_overflow})$$
If each worker configures `pool_size = 30` and you run 4 workers, your application can open **120 connections**, exceeding PostgreSQL's `max_connections = 100`.

Adjust your pool parameters in `database.py`:
```python
# Sized safely for 4 workers against max_connections = 100
engine = create_async_engine(
    DATABASE_URL,
    pool_size=15,       # 15 * 4 workers = 60 connections
    max_overflow=5,     # 5 * 4 workers = 20 connections max
    pool_pre_ping=True
)
```

---

## 4. Expected Performance Delta

| Metric | Checkpoint A (1 Worker) | Checkpoint B (4 Workers) | Delta |
| :--- | :--- | :--- | :--- |
| **Throughput (RPS)** | ~3,800 RPS | **14,200+ RPS** | **3.7x linear scaling** |
| **Total Host CPU Utilization** | ~25% (1 core maxed) | **~92% across all 4 cores** | Full hardware utilization |
| **p99 Latency at 500 Conns** | 58ms | **14ms** | **75% reduction** |
| **System Resilience** | If process crashes, app dies | Master process auto-restarts worker | High availability |

---

## 5. Step-by-Step Lab Runbook

1. **Step 1:** Start FastAPI in single worker mode:
   ```bash
   uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
   ```
2. **Step 2:** Open a terminal with CPU monitoring:
   ```bash
   mpstat -P ALL 1
   ```
3. **Step 3:** Fire `wrk` with 500 connections for 30 seconds:
   ```bash
   wrk -t8 -c500 -d30s http://localhost:8000/notes/1
   ```
4. **Step 4:** Observe that only one CPU core is busy; record RPS (~3,800 RPS).
5. **Step 5:** Terminate Uvicorn and start Gunicorn with 4 workers:
   ```bash
   gunicorn main:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000
   ```
6. **Step 6:** Re-run the identical `wrk` command.
7. **Step 7:** Observe all 4 cores firing at 80%–95% utilization and record throughput scaling past 14,000 RPS.

---

## 6. Senior Backend Interview Talking Points

* **Worker Sizing Rule of Thumb:** The classic Gunicorn formula is:
  $$\text{Workers} = (2 \times \text{CPUs}) + 1$$
  However, for asynchronous frameworks like FastAPI with `uvloop`, a ratio of **1 worker per physical/virtual CPU core** is typically optimal, as async workers already handle I/O concurrency within the event loop.
* **Kubernetes Pod Sizing vs In-Container Multiprocessing:** In Kubernetes architectures, should you run 1 worker per Pod with horizontal pod autoscaling (HPA), or multiple workers per Pod?
  * *Senior answer:* Running multiple workers per container (e.g. 2–4 workers) utilizes multi-core container resource limits more efficiently and reduces the overhead of running multiple Kubernetes Pod replicas and sidecar proxies.
* **CPython Free-Threaded (PEP 703):** Mention knowledge of ongoing Python 3.13+ developments regarding the removal of the GIL (free-threaded Python), and how true multi-threading will eventually complement multi-worker deployments.
