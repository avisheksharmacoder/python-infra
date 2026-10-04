import sys
import os
import json
import logging
from datetime import timezone
from loguru import logger

class InterceptHandler(logging.Handler):
    """
    Redirect standard library logging (e.g. uvicorn, sqlalchemy) into Loguru.
    """
    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )

def json_formatter(record) -> str:
    """
    Official Loguru formatter pattern for structured JSON Lines output.
    """
    log_dict = {
        "timestamp": record["time"].astimezone(timezone.utc).isoformat(),
        "level": record["level"].name,
        "process_id": record["process"].id,
        "thread_name": record["thread"].name,
        "message": record["message"],
        "module": record["name"],
        "function": record["function"],
        "line": record["line"],
    }

    # Extract extra attributes passed via logger.bind(...)
    extra = record["extra"]
    for key in ["request_id", "method", "path", "status_code", "duration_ms", "client_ip"]:
        if key in extra:
            log_dict[key] = extra[key]

    # Include any remaining metadata
    remaining = {k: v for k, v in extra.items() if k not in log_dict and k != "serialized"}
    if remaining:
        log_dict["extra"] = remaining

    # Exception serialization
    if record["exception"]:
        log_dict["exception"] = {
            "type": record["exception"].type.__name__ if record["exception"].type else "Exception",
            "value": str(record["exception"].value)
        }

    # Store serialized string in extra and return format template
    record["extra"]["serialized"] = json.dumps(log_dict)
    return "{extra[serialized]}\n"

def setup_logging(
    log_file_path: str = "/app/logs/app.jsonl",
    log_level: str = "INFO"
):
    """
    Configure Loguru handlers:
      1. Colored stdout for development / terminal inspection.
      2. Asynchronous JSONL file sink with 500MB rotation & 7-day retention.
      3. Interception of standard library loggers (Uvicorn, FastAPI, SQLAlchemy).
    """
    log_dir = os.path.dirname(log_file_path)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    # Reset loguru handlers
    logger.remove()

    # 1. Console Output (colored, human readable)
    logger.add(
        sys.stdout,
        level=log_level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
        colorize=True,
        enqueue=True
    )

    # 2. JSONL File Sink (Machine parsable)
    logger.add(
        log_file_path,
        level=log_level,
        format=json_formatter,
        rotation="500 MB",
        retention="7 days",
        compression="zip",
        enqueue=True,
        backtrace=True,
        diagnose=False
    )

    # 3. Intercept standard loggers
    logging.root.handlers = [InterceptHandler()]
    logging.root.setLevel(log_level)

    for logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error", "fastapi", "sqlalchemy.engine"):
        std_logger = logging.getLogger(logger_name)
        std_logger.handlers = [InterceptHandler()]
        std_logger.propagate = False

    logger.info(f"Structured JSONL logging initialized -> {log_file_path}")
    return logger

if __name__ == "__main__":
    test_log = "/tmp/test.jsonl"
    if os.path.exists(test_log):
        os.remove(test_log)
    log = setup_logging(log_file_path=test_log, log_level="DEBUG")
    log.bind(
        request_id="test-req-001",
        method="GET",
        path="/v1/users/42/notes",
        status_code=200,
        duration_ms=4.12,
        client_ip="127.0.0.1"
    ).info("Note fetched successfully")
    
    # Wait for enqueue thread to flush
    logger.complete()

    print(f"Test completed. Inspecting {test_log}:")
    with open(test_log) as f:
        print(f.read())
