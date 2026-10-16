#!/usr/bin/env python3
"""Wait for a live CI child process to serve a successful HTTP response."""
from __future__ import annotations

import argparse
import http.client
import math
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def bounded_seconds(limit: float):
    def parse(value: str) -> float:
        seconds = float(value)
        if not math.isfinite(seconds) or not 0 < seconds <= limit:
            raise argparse.ArgumentTypeError(f"seconds must be greater than 0 and at most {limit:g}")
        return seconds
    return parse


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    if sys.platform.startswith("linux"):
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except FileNotFoundError:
            return False
        # An exited child can remain a zombie until its shell/test parent reaps it.
        return state not in {"Z", "X"}
    return True


def failure(reason: str, detail: str, log: Path) -> int:
    print(f"HTTP_STARTUP=FAIL reason={reason} last_error={detail}", file=sys.stderr)
    print(f"STARTUP_LOG={log}", file=sys.stderr)
    try:
        text = log.read_text(errors="replace")
    except OSError as error:
        print(f"Startup log unavailable: {error}", file=sys.stderr)
    else:
        print(text, end="" if text.endswith("\n") else "\n", file=sys.stderr)
    return 1


def wait_for_http(pid: int, url: str, log: Path, timeout: float,
                  request_timeout: float, interval: float) -> int:
    started = time.monotonic()
    deadline = started + timeout
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    attempts = 0
    last_error = "no response"
    while True:
        try:
            alive = process_alive(pid)
        except (OSError, ValueError, IndexError) as error:
            return failure("process_check_failed", str(error), log)
        if not alive:
            return failure("process_exited", last_error, log)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return failure("timeout", last_error, log)
        attempts += 1
        try:
            with opener.open(url, timeout=min(request_timeout, remaining)) as response:
                status = response.status
            if 200 <= status < 300:
                if time.monotonic() >= deadline:
                    return failure("timeout", "response arrived after deadline", log)
                if not process_alive(pid):
                    return failure("process_exited", "process exited during HTTP probe", log)
                print(f"HTTP_STARTUP=PASS attempts={attempts} elapsed={time.monotonic() - started:.3f}s")
                return 0
            last_error = f"HTTP {status}"
        except urllib.error.HTTPError as error:
            last_error = f"HTTP {error.code}"
            error.close()
        except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
            last_error = f"{type(error).__name__}: {error}"
        if attempts == 1:
            print(f"HTTP_STARTUP=WAIT pid={pid} timeout={timeout:g}s last_error={last_error}", flush=True)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(interval, remaining))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", required=True, type=int)
    parser.add_argument("--url", required=True)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--timeout", type=bounded_seconds(120), default=120.0)
    parser.add_argument("--request-timeout", type=bounded_seconds(2), default=2.0)
    parser.add_argument("--interval", type=bounded_seconds(1), default=1.0)
    args = parser.parse_args(argv)
    if args.pid <= 0:
        parser.error("pid must be positive")
    if urllib.parse.urlsplit(args.url).scheme not in {"http", "https"}:
        parser.error("url must use HTTP or HTTPS")
    return wait_for_http(args.pid, args.url, args.log, args.timeout,
                         args.request_timeout, args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
