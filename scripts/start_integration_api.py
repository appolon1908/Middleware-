#!/usr/bin/env python3
from __future__ import annotations
import os
import sys
from app.core.bootstrap import SERVICE_INTEGRATION_API
from app.entrypoints.runtime import configure_logging, validate_runtime

def main() -> None:
    configure_logging()
    validate_runtime(SERVICE_INTEGRATION_API)
    workers = max(1, int(os.getenv("UVICORN_WORKERS", "2")))
    port = int(os.getenv("PORT", "8095"))
    forwarded = os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1")
    os.execv(sys.executable, [
        sys.executable, "-m", "uvicorn", "app.entrypoints.integration_api:app",
        "--host=0.0.0.0", f"--port={port}", f"--workers={workers}",
        "--proxy-headers", f"--forwarded-allow-ips={forwarded}",
    ])

if __name__ == "__main__":
    main()
