#!/usr/bin/env python3
"""Runs slice_feed against the mock server and checks what the mock received.

usage: run_slice.py SLICE_FEED_BINARY [SECONDS]
"""
import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# a cross build's binaries run under an emulator, e.g. SAA_TEST_EMULATOR=qemu-arm
emulator = shlex.split(os.environ.get("SAA_TEST_EMULATOR", ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_listening(port, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return True
        except OSError:
            time.sleep(0.05)
    return False


def main():
    binary = sys.argv[1]
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
    with tempfile.TemporaryDirectory() as tmp:
        summary = os.path.join(tmp, "summary.jsonl")
        http_port, ws_port = free_port(), free_port()
        mock = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "mock_server.py"), "--http-port", str(http_port),
             "--ws-port", str(ws_port), "--summary", summary],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        try:
            if not wait_listening(ws_port):
                print("mock server did not start")
                return 1
            run = subprocess.run(emulator + [binary, f"ws://127.0.0.1:{ws_port}/ws", str(seconds)],
                                 capture_output=True, text=True, timeout=seconds + 30)
            sessions = []
            for _ in range(50):                         # the mock writes when the session ends
                if os.path.exists(summary):
                    with open(summary) as f:
                        sessions = [json.loads(line) for line in f if line.strip()]
                    if sessions:
                        break
                time.sleep(0.1)
        finally:
            mock.terminate()
            mock.wait(timeout=5)

    sys.stdout.write(run.stdout)
    sys.stderr.write(run.stderr)
    if not sessions:
        print("FAIL: the mock recorded no session")
        return 1
    s = sessions[-1]
    expected = seconds * 10
    checks = [
        (run.returncode == 0, f"client exit status {run.returncode}"),
        (len(sessions) == 1, f"{len(sessions)} sessions, expected 1"),
        (s["bad_frames"] == 0, f"{s['bad_frames']} malformed frames"),
        (abs(s["audio_frames"] - expected) <= 2, f"{s['audio_frames']} audio frames, expected ~{expected:g}"),
        (s["close_code"] == 1000 and s["close_reason"] == "client stop",
         f"close {s['close_code']} {s['close_reason']!r}"),
        ("server_profile=audio_only" in s["path"], f"path {s['path']!r} lacks the inferred profile"),
        (s["user_agent"].startswith("saa-c/"), f"user agent {s['user_agent']!r}"),
    ]
    failed = [msg for ok, msg in checks if not ok]
    print(json.dumps(s))
    for msg in failed:
        print("FAIL:", msg)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
