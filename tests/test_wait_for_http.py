"""Exercise the startup waiter against real local child processes and HTTP."""
import contextlib
from pathlib import Path
import select
import socket
import subprocess
import sys
import tempfile
import time
import unittest


HELPER = Path(__file__).resolve().parents[1] / "scripts" / "wait_for_http.py"
HELPER_LAUNCHER = r'''
import runpy, sys
helper = runpy.run_path(sys.argv[1])
print("HELPER_LOADED", flush=True)
if sys.stdin.readline() != "GO\n":
    raise SystemExit("helper launch was not released")
raise SystemExit(helper["main"](sys.argv[2:]))
'''
CHILD = r'''
import http.server, os, pathlib, socket, sys, time
fd, mode, control, started = sys.argv[1:]
listener = socket.socket(fileno=int(fd))
pathlib.Path(started).touch()
print("CHILD_DIAGNOSTIC_SENTINEL", flush=True)
if mode == "exit":
    time.sleep(0.1)
    sys.exit(7)
if mode == "never":
    time.sleep(60)
    sys.exit(0)
while mode == "delayed" and not pathlib.Path(control).exists():
    time.sleep(0.01)
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if mode == "stall":
            time.sleep(5)
            return
        code = 503 if mode == "bad-status" else 200
        self.send_response(code)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")
    def log_message(self, *args):
        pass
server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler,
                                         bind_and_activate=False)
server.socket.close()
server.socket = listener
server.server_address = listener.getsockname()
server.server_activate()
server.serve_forever()
'''


class StartupWaiterSubprocessTests(unittest.TestCase):
    @contextlib.contextmanager
    def child(self, mode):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "application-startup.txt"
            started = root / "started"
            control = root / "serve"
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
                with log.open("w") as output:
                    process = subprocess.Popen(
                        [sys.executable, "-u", "-c", CHILD, str(listener.fileno()),
                         mode, str(control), str(started)],
                        pass_fds=(listener.fileno(),), stdout=output, stderr=output,
                    )
            try:
                deadline = time.monotonic() + 30
                while not started.exists() and time.monotonic() < deadline:
                    if process.poll() is not None:
                        self.fail(log.read_text())
                    time.sleep(0.01)
                self.assertTrue(started.exists(), "fixture child did not initialize")
                yield process, f"http://127.0.0.1:{port}/healthz", log, control
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)

    def command(self, process, url, log, timeout):
        return [sys.executable, "-u", "-c", HELPER_LAUNCHER, str(HELPER),
                "--pid", str(process.pid), "--url", url, "--log", str(log),
                "--timeout", str(timeout), "--request-timeout", "0.15",
                "--interval", "0.03"]

    @contextlib.contextmanager
    def waiter(self, child, url, log, timeout):
        process = subprocess.Popen(self.command(child, url, log, timeout),
                                   text=True, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            ready, _, _ = select.select([process.stdout], [], [], 30)
            self.assertTrue(ready, "helper did not finish loading")
            self.assertEqual(process.stdout.readline().strip(), "HELPER_LOADED")
            # Import cost is outside the measured runtime deadline. The real
            # helper still parses its CLI arguments and performs every probe.
            started = time.monotonic()
            process.stdin.write("GO\n")
            process.stdin.flush()
            yield process, started
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=2)

    def test_delayed_listener_succeeds_after_connection_refused(self):
        with self.child("delayed") as (child, url, log, control):
            with self.waiter(child, url, log, 3) as (waiter, _):
                ready, _, _ = select.select([waiter.stdout], [], [], 5)
                self.assertTrue(ready, "waiter did not report its first failed probe")
                first = waiter.stdout.readline()
                self.assertIn("HTTP_STARTUP=WAIT", first)
                control.touch()
                out, err = waiter.communicate(timeout=5)
                self.assertEqual(waiter.returncode, 0, err)
                self.assertIn("HTTP_STARTUP=PASS", out)
                self.assertIsNone(child.poll())

    def test_live_never_listener_times_out_and_emits_startup_log(self):
        with self.child("never") as (child, url, log, _):
            with self.waiter(child, url, log, 0.4) as (waiter, started):
                out, err = waiter.communicate(timeout=5)
                elapsed = time.monotonic() - started
                self.assertNotEqual(waiter.returncode, 0, out)
                self.assertIn("reason=timeout", err)
                self.assertIn("CHILD_DIAGNOSTIC_SENTINEL", err)
                self.assertLess(elapsed, 2)
                self.assertIsNone(child.poll())

    def test_early_child_exit_fails_promptly_and_emits_startup_log(self):
        with self.child("exit") as (child, url, log, _):
            with self.waiter(child, url, log, 3) as (waiter, started):
                out, err = waiter.communicate(timeout=5)
                elapsed = time.monotonic() - started
                self.assertNotEqual(waiter.returncode, 0, out)
                self.assertIn("reason=process_exited", err)
                self.assertIn("CHILD_DIAGNOSTIC_SENTINEL", err)
                self.assertLess(elapsed, 2)

    def test_non_success_http_status_never_counts_as_ready(self):
        with self.child("bad-status") as (child, url, log, _):
            with self.waiter(child, url, log, 0.4) as (waiter, _):
                out, err = waiter.communicate(timeout=5)
                self.assertNotEqual(waiter.returncode, 0)
                self.assertIn("reason=timeout", err)
                self.assertIn("HTTP 503", err)
                self.assertNotIn("HTTP_STARTUP=PASS", out)

    def test_stalled_http_request_cannot_outlive_deadline(self):
        with self.child("stall") as (child, url, log, _):
            with self.waiter(child, url, log, 0.4) as (waiter, started):
                out, err = waiter.communicate(timeout=5)
                elapsed = time.monotonic() - started
                self.assertNotEqual(waiter.returncode, 0, out)
                self.assertIn("reason=timeout", err)
                self.assertIn("CHILD_DIAGNOSTIC_SENTINEL", err)
                self.assertLess(elapsed, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
