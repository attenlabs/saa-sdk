#!/usr/bin/env python3
"""Runs client_harness against the mock server and checks what the mock
received in each of its four sessions: the harness itself checks the callbacks.

usage: run_harness.py CLIENT_HARNESS_BINARY
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


def read(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def same(actions, want):
    """actions (from the mock) match want, a list of (action, value or text or None)."""
    if len(actions) != len(want):
        return False
    for a, (name, arg) in zip(actions, want):
        if a.get("action") != name:
            return False
        if isinstance(arg, float) and abs(a.get("value", -1) - arg) > 1e-6:
            return False
        if isinstance(arg, str) and a.get("text") != arg:
            return False
    return True


def show(actions):
    return [(a.get("action"), a.get("value", a.get("text"))) for a in actions]


def main():
    binary = sys.argv[1]
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
            run = subprocess.run(emulator + [binary, f"ws://127.0.0.1:{ws_port}/ws"],
                                 capture_output=True, text=True, timeout=60)
            sessions = []
            for _ in range(50):                         # the mock writes when a session ends
                sessions = [s for s in read(summary) if s["scenario"] == "harness"]
                if len(sessions) >= 4:
                    break
                time.sleep(0.1)
        finally:
            mock.terminate()
            mock.wait(timeout=5)

    sys.stdout.write(run.stdout)
    sys.stderr.write(run.stderr)
    by = {s["session"]: s for s in sessions}
    s1, s2, s3, s4 = (by.get(n, {}) for n in (1, 2, 3, 4))
    a = {n: s.get("actions", []) for n, s in ((1, s1), (2, s2), (3, s3), (4, s4))}

    def closed(s, code, reason):
        return s.get("close_code") == code and s.get("close_reason") == reason

    checks = [
        (run.returncode == 0, f"client_harness exit status {run.returncode}"),
        (len(sessions) == 4, f"{len(sessions)} sessions, expected 4"),
        (all(s.get("bad_frames") == 0 for s in sessions), "malformed frames"),
        (s1.get("audio_frames", 0) > 0 and s2.get("audio_frames", 0) > 0, "no audio in sessions 1 and 2"),
        # 1: the resync of the initial threshold, then every call, in call order
        (same(a[1], [("set_threshold", 0.6), ("set_threshold", 0.55), ("mute", None),
                     ("responding_start", None), ("utterance_set_threshold", 0.4),
                     ("utterance_assistant_turn", "hello"), ("utterance_assistant_turn", "drop")]),
         f"session 1 actions {show(a[1])}"),
        (closed(s1, 1011, "mock drop"), f"session 1 close {s1.get('close_code')} {s1.get('close_reason')!r}"),
        # 2: the state after the calls made while closed (unmute, threshold 0.65) and nothing else
        (same(a[2], [("set_threshold", 0.65), ("responding_start", None), ("utterance_set_threshold", 0.4)]),
         f"session 2 actions {show(a[2])}"),
        (closed(s2, 1000, "client stop"), f"session 2 close {s2.get('close_code')} {s2.get('close_reason')!r}"),
        # 3: after stop() and start(): the thresholds survive, mute and responding do not
        (same(a[3], [("set_threshold", 0.65), ("utterance_set_threshold", 0.4),
                     ("utterance_assistant_turn", "again")]),
         f"session 3 actions {show(a[3])}"),
        (closed(s3, 1000, "client stop"), f"session 3 close {s3.get('close_code')} {s3.get('close_reason')!r}"),
        # 4: stop() from on_started may or may not let the resync out first
        (all(x.get("action") in ("set_threshold", "utterance_set_threshold") for x in a[4]),
         f"session 4 actions {show(a[4])}"),
        (closed(s4, 1000, "client stop"), f"session 4 close {s4.get('close_code')} {s4.get('close_reason')!r}"),
    ]
    failed = [msg for ok, msg in checks if not ok]
    for msg in failed:
        print("FAIL:", msg)
    if not failed:
        print(f"mock: {len(sessions)} sessions as expected")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
