#!/usr/bin/env python3
"""Runs capture_harness against the mock server and checks what the mock
received from the client's own (fake) microphone and camera; the harness itself
checks the callbacks and return codes.

usage: run_capture.py CAPTURE_HARNESS_BINARY
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
                                 capture_output=True, text=True, timeout=90)
            time.sleep(0.5)                              # the mock writes when a session ends
            rows = []
            if os.path.exists(summary):
                with open(summary) as f:
                    rows = [json.loads(line) for line in f if line.strip()]
        finally:
            mock.terminate()
            mock.wait(timeout=5)

    sys.stdout.write(run.stdout)
    sys.stderr.write(run.stderr)

    def sessions(key):
        return sorted((r for r in rows if r["scenario"] == key), key=lambda r: r["session"])

    nocam, av, loss = sessions("capture-nocam"), sessions("capture-av"), sessions("capture-loss")
    lo = loss[0] if loss else {}
    audio_s, peak_s, video_s = lo.get("audio_per_s", []), lo.get("audio_peak_per_s", []), lo.get("video_per_s", [])
    # during the outage: audio still at 10 a second, but silent; video stopped
    quiet = [i for i in range(1, len(peak_s) - 1) if peak_s[i] == 0]
    dark = [i for i in range(1, len(video_s) - 1) if video_s[i] == 0]
    checks = [
        (run.returncode == 0, f"capture_harness exit status {run.returncode}"),
        (not sessions("capture-devfail"), "a microphone that will not open must not connect"),
        (len(nocam) == 1 and "server_profile=audio_only" in nocam[0]["path"],
         f"without a camera the profile falls back to audio_only: {[s['path'] for s in nocam]}"),
        (bool(nocam) and nocam[0]["video_frames"] == 0 and nocam[0]["audio_frames"] >= 10,
         f"no-camera session {nocam[:1]}"),
        (len(av) == 2 and all("audio_only" not in s["path"] for s in av), f"sessions {[s['path'] for s in av]}"),
        (len(av) == 2 and 25 <= av[0]["audio_frames"] <= 35 and 9 <= av[0]["video_frames"] <= 14,
         f"3 s of audio and video: {[(s['audio_frames'], s['video_frames']) for s in av]}"),
        (all(s.get("bad_frames") == 0 for s in nocam + av + loss), "malformed frames"),
        (len(loss) == 1 and all(n >= 9 for n in audio_s[1:6]), f"audio per second through the loss: {audio_s}"),
        (bool(quiet) and peak_s[0] > 4000 and peak_s[-2] > 4000,
         f"loudest sample per second: {peak_s} (want silence in the middle, the tone around it)"),
        (bool(dark) and video_s[0] >= 3 and video_s[-2] >= 3,
         f"video frames per second: {video_s} (want a gap in the middle)"),
    ]
    failed = [msg for ok, msg in checks if not ok]
    for msg in failed:
        print("FAIL:", msg)
    if not failed:
        print(f"mock: capture sessions as expected; loss: audio {audio_s}, peaks {peak_s}, video {video_s}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
