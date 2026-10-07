#!/usr/bin/env python3
"""Run localhost-only Newman against a disposable, effect-disabled API fixture."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import jwt

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    newman = shutil.which("newman")
    if newman is None:
        raise RuntimeError("Newman must be installed and available on PATH")
    # Never reuse an arbitrary existing listener for a credentialed test.
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", 8095))
    with tempfile.TemporaryDirectory(prefix="mw05-newman-") as directory:
        temporary = Path(directory)
        with (temporary / "server.log").open("w") as log:
            server = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "tests.mw05_newman_app:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8095",
                ],
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                env={
                    **os.environ,
                    "APP_ENV": "test",
                    "ALLOW_IN_MEMORY_STORAGE": "true",
                    "EXTERNAL_EFFECTS": "false",
                },
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                deadline = time.monotonic() + 300
                while True:
                    if server.poll() is not None:
                        raise RuntimeError("disposable API fixture did not start")
                    try:
                        with urlopen(
                            "http://127.0.0.1:8095/health", timeout=1
                        ) as response:
                            if response.status == 200:
                                break
                    except (URLError, TimeoutError):
                        pass
                    if time.monotonic() >= deadline:
                        raise RuntimeError("disposable API fixture startup timed out")
                    time.sleep(0.2)
                issued = int(time.time())
                # This token is accepted only by the fake verifier in tests;
                # production JWT validation and lifetime bounds are untouched.
                bearer = jwt.encode(
                    {
                        "iss": "fake",
                        "aud": "middleware-api",
                        "azp": "middleware-api",
                        "sub": "user-1",
                        "iat": issued,
                        "exp": issued + 1800,
                        "scope": "platform.command platform.command.read",
                        "tenant_ids": ["TEST_SYN"],
                        "realm_access": {"roles": []},
                    },
                    "unit-test-only",
                    algorithm="HS256",
                )
                environment = temporary / "environment.json"
                environment.write_text(
                    json.dumps(
                        {
                            "name": "mw05-disposable",
                            "values": [
                                {
                                    "key": "bearer_token",
                                    "value": bearer,
                                    "enabled": True,
                                }
                            ],
                        }
                    )
                )
                return subprocess.run(
                    [
                        newman,
                        "run",
                        str(
                            ROOT
                            / "postman/collections/Middleware-V3-API-Synthetic.postman_collection.json"
                        ),
                        "-e",
                        str(environment),
                        "--timeout",
                        "300000",
                    ],
                    cwd=ROOT,
                    check=False,
                ).returncode
            finally:
                if os.name == "nt":
                    # The Windows venv launcher has a child interpreter. Stop
                    # our entire fixture tree before removing its open log.
                    subprocess.run(
                        ["taskkill", "/PID", str(server.pid), "/T", "/F"],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                else:
                    server.terminate()
                try:
                    server.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()


if __name__ == "__main__":
    raise SystemExit(main())
