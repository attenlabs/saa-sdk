#!/usr/bin/env python3
"""Starts the mock server, runs a test binary with its WebSocket URL, and exits
with the binary's status. The binary checks everything itself.

usage: run_with_mock.py BINARY [ARGS...]
"""
import os
import shlex
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
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
    http_port, ws_port = free_port(), free_port()
    mock = subprocess.Popen([sys.executable, os.path.join(HERE, "mock_server.py"), "--http-port", str(http_port),
                             "--ws-port", str(ws_port)], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        if not wait_listening(ws_port):
            print("mock server did not start")
            return 1
        return subprocess.run(emulator + [sys.argv[1], f"ws://127.0.0.1:{ws_port}/ws"] + sys.argv[2:],
                              timeout=120).returncode
    finally:
        mock.terminate()
        mock.wait(timeout=5)


if __name__ == "__main__":
    sys.exit(main())
