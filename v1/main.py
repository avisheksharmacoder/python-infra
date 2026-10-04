import time
import uuid
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from loguru import logger

try:
    from .logger import setup_logging
    from .database import init_db, close_db
    from .routers.base import router as base_router
except ImportError:
    from logger import setup_logging
    from database import init_db, close_db
    from routers.base import router as base_router

# 1. Lifespan Context Manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Configure Loguru JSONL sink and initialize database
    log_file_path = os.getenv("LOG_FILE_PATH", "/app/logs/app.jsonl")
    log_level = os.getenv("LOG_LEVEL", "INFO")
    setup_logging(log_file_path=log_file_path, log_level=log_level)
    
    logger.info("Application starting up... Initializing database.")
    await init_db()
    logger.info("Application startup complete. Ready to receive requests.")
    
    yield
    
    # Shutdown: Cleanly dispose connection pools
    logger.info("Application shutting down...")
    await close_db()
    logger.info("Application shutdown complete.")

# 2. FastAPI Application Instance
app = FastAPI(
    title="PAUF High-Load Performance Lab",
    description="PostgreSQL & FastAPI High-Concurrency Performance Optimization Harness",
    version="1.0.0",
    lifespan=lifespan
)

# 3. Request Correlation & JSONL Timing Middleware
@app.middleware("http")
async def telemetry_middleware(request: Request, call_next):
    # Correlation ID
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    request.state.request_id = request_id

    start_time = time.perf_counter()
    status_code = 500
    try:
        response: Response = await call_next(request)
        status_code = response.status_code
    except Exception as exc:
        duration_ms = round((time.perf_counter() - start_time) * 1000, 3)
        client_ip = request.client.host if request.client else "unknown"
        logger.bind(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            status_code=500,
            duration_ms=duration_ms,
            client_ip=client_ip
        ).exception(f"Unhandled error processing {request.method} {request.url.path}: {exc}")
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal Server Error", "request_id": request_id}
        )

    duration_ms = round((time.perf_counter() - start_time) * 1000, 3)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Process-Time"] = f"{duration_ms}ms"

    # Emit non-blocking JSONL log
    client_ip = request.client.host if request.client else "unknown"
    logger.bind(
        request_id=request_id,
        method=request.method,
        path=request.url.path,
        status_code=status_code,
        duration_ms=duration_ms,
        client_ip=client_ip
    ).info(f"{request.method} {request.url.path} -> {status_code} ({duration_ms}ms)")

    return response

# 4. Mount Modular Routers
# Baseline API routes under /v1
app.include_router(base_router, prefix="/v1")

# Root /health alias for direct container health checks
@app.get("/health", include_in_schema=False)
async def root_health():
    return {"status": "ok"}

# Root / and /dashboard redirects to live telemetry dashboard
from fastapi.responses import RedirectResponse

@app.get("/", include_in_schema=False)
@app.get("/dashboard", include_in_schema=False)
async def root_dashboard():
    return RedirectResponse(url="/v1/dashboard")

# Placeholder for future phase router mounts:
# app.include_router(phase01_router, prefix="/v1/phase01", tags=["Phase 01"])
