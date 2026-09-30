#!/usr/bin/env python3
"""soak.py - runs the demo against the mock server's soak scenario for a long time and
checks that it does not grow. Linux only: it reads /proc.

The demo streams a live WAV (a speech clip on a loop, paced in real time, as arecord would
send it) and JPEGs at 4 fps. The mock answers with a turn every 30 s and drops the
connection every 10 minutes, alternating a TCP reset and a 1011 close. Every minute the
script samples the demo's resident memory (from /proc/PID/smaps_rollup), its open file
descriptors, and its threads, and it counts the demo's JSON lines: turns, reconnects, and
errors.

It passes when the demo ran to the end and exited 0, and, between the first hour of the run
and the last, resident memory grew by at most 1 MB (the medians of each hour's samples) and
the open file descriptors did not grow. In runs shorter than two hours the first and last
quarters stand in for the hours. It also expects turns and reconnects at about the rates the
mock sets, and no error that is not retriable.

usage: soak.py DURATION [--demo PATH] [--out DIR] [--wav FILE]
  DURATION is seconds, or a number with s, m, or h: 600, 90m, 24h.
  --out DIR keeps samples.jsonl, events.jsonl.gz, mock.log and report.json there
  (default: a new directory under /tmp).
"""
import argparse
import gzip
import io
import json
import os
import shlex
import signal
import socket
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SAMPLE_S = 60
MAX_GROWTH_KB = 1024


def parse_duration(text):
    units = {"s": 1, "m": 60, "h": 3600}
    if text[-1:] in units:
        return float(text[:-1]) * units[text[-1]]
    return float(text)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_listening(port, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return True
        except OSError:
            time.sleep(0.05)
    return False


def make_jpegs(d, count=4, size=20_000):
    """Stand-ins for 640x480 frames: the client checks nothing but the length."""
    os.makedirs(d, exist_ok=True)
    for i in range(count):
        with open(os.path.join(d, f"frame{i}.jpg"), "wb") as f:
            f.write(b"\xff\xd8\xff\xe0" + os.urandom(size - 6) + b"\xff\xd9")
    return d


def live_header(rate, ch, width):
    """A 44-byte WAV header with the placeholder sizes a live writer sends. Built, not copied:
    a file's own header can carry other chunks, such as ffmpeg's LIST, before its data."""
    block = ch * width
    return (b"RIFF\xff\xff\xff\xffWAVEfmt " + struct.pack("<IHHIIHH", 16, 1, ch, rate, rate * block, block, 8 * width)
            + b"data\xff\xff\xff\xff")


def stream_loop(src, dst, stop):
    """A live WAV of unknown length: the header with placeholder sizes, then src's PCM on a
    loop, 10 ms every 10 ms on an absolute schedule."""
    with wave.open(src, "rb") as w:
        rate, ch, width, pcm = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.readframes(w.getnframes())
    header = live_header(rate, ch, width)
    step = rate // 100 * ch * width
    try:
        dst.write(bytes(header))
        t, i = time.monotonic(), 0
        while not stop.is_set():
            dst.write(pcm[i:i + step])
            dst.flush()
            i = (i + step) % (len(pcm) - len(pcm) % step)
            t += 0.01
            time.sleep(max(0.0, t - time.monotonic()))
    except (BrokenPipeError, OSError, ValueError):
        pass


def proc_sample(pid):
    """Resident memory in KB (total and anonymous), open fds, and threads; None once gone."""
    try:
        rss = anon = 0
        path = f"/proc/{pid}/smaps_rollup"
        if not os.path.exists(path):
            path = f"/proc/{pid}/smaps"
        with open(path) as f:
            for line in f:
                if line.startswith("Rss:"):
                    rss += int(line.split()[1])
                elif line.startswith("Anonymous:"):
                    anon += int(line.split()[1])
        fds = len(os.listdir(f"/proc/{pid}/fd"))
        with open(f"/proc/{pid}/status") as f:
            threads = next(int(line.split()[1]) for line in f if line.startswith("Threads:"))
        return {"rss_kb": rss, "anon_kb": anon, "fds": fds, "threads": threads}
    except (OSError, StopIteration, ValueError):
        return None


class Events:
    """Counts the demo's JSON lines as they arrive, and keeps them compressed."""

    def __init__(self, path):
        self.lock = threading.Lock()
        self.counts = {}
        self.errors = []
        self.summary = None
        self.out = gzip.open(path, "wt")

    def feed(self, stream):
        for line in stream:
            with self.lock:
                self.out.write(line)
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                name = ev.get("event", "?")
                self.counts[name] = self.counts.get(name, 0) + 1
                if name == "error":
                    self.errors.append({k: ev.get(k) for k in ("ts_ms", "kind", "title", "message", "retriable")})
                    del self.errors[:-50]
                elif name == "summary":
                    self.summary = ev
        with self.lock:
            self.out.close()

    def snapshot(self):
        with self.lock:
            return dict(self.counts)


def window_median(samples, lo, hi, key):
    vals = [s[key] for s in samples if lo <= s["t_s"] < hi]
    return statistics.median(vals) if vals else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("duration")
    ap.add_argument("--demo", default=os.path.join(ROOT, "build", "saa_client_demo"))
    ap.add_argument("--out")
    ap.add_argument("--wav", default=os.path.join(ROOT, "examples", "demo", "sample_drive_thru.wav"))
    a = ap.parse_args()
    if not sys.platform.startswith("linux"):
        print("soak.py reads /proc/PID/smaps, so it runs on Linux only")
        return 2
    duration = parse_duration(a.duration)
    out = a.out or tempfile.mkdtemp(prefix="saa-soak-")
    os.makedirs(out, exist_ok=True)
    jpegs = make_jpegs(os.path.join(out, "jpeg"))
    http_port, ws_port = free_port(), free_port()
    mock_log = open(os.path.join(out, "mock.log"), "w")
    mock = subprocess.Popen([sys.executable, os.path.join(HERE, "mock_server.py"), "--http-port", str(http_port),
                             "--ws-port", str(ws_port), "--summary", os.path.join(out, "mock_summary.jsonl")],
                            stdout=mock_log, stderr=subprocess.STDOUT)
    if not wait_listening(http_port) or not wait_listening(ws_port):
        print("the mock server did not start; see", os.path.join(out, "mock.log"))
        mock.kill()
        return 1

    # the demo stops itself at the end; a little extra lets its last turn and summary out
    cmd = [a.demo, "--url", f"http://127.0.0.1:{http_port}", "--wav", "-", "--jpeg-dir", jpegs,
           "--duration", str(duration), "--stats"]
    env = dict(os.environ, SAA_API_KEY="soak")
    print(f"soak: {duration / 3600:.2f} h into {out}", flush=True)
    print("soak:", " ".join(shlex.quote(c) for c in cmd), flush=True)
    demo_err = open(os.path.join(out, "demo.stderr"), "w")
    demo = subprocess.Popen(cmd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=demo_err)
    stop = threading.Event()
    threading.Thread(target=stream_loop, args=(a.wav, demo.stdin, stop), daemon=True).start()
    events = Events(os.path.join(out, "events.jsonl.gz"))
    reader = threading.Thread(target=events.feed, args=(io.TextIOWrapper(demo.stdout, encoding="utf-8"),),
                              daemon=True)
    reader.start()

    samples = []
    t0 = time.monotonic()
    next_at = t0 + SAMPLE_S
    with open(os.path.join(out, "samples.jsonl"), "w") as sf:
        while demo.poll() is None:
            time.sleep(max(0.0, min(1.0, next_at - time.monotonic())))
            if time.monotonic() < next_at:
                continue
            next_at += SAMPLE_S
            s = proc_sample(demo.pid)
            if not s:
                continue
            s["t_s"] = round(time.monotonic() - t0)
            s.update({k: v for k, v in events.snapshot().items()
                      if k in ("turn_ready", "reconnected", "disconnected", "error")})
            samples.append(s)
            sf.write(json.dumps(s) + "\n")
            sf.flush()
            if len(samples) % 60 == 0 or len(samples) <= 2:
                print(f"soak: {s['t_s'] / 3600:.2f} h  rss {s['rss_kb']} KB  anon {s['anon_kb']} KB  "
                      f"fds {s['fds']}  threads {s['threads']}  turns {s.get('turn_ready', 0)}  "
                      f"reconnects {s.get('reconnected', 0)}", flush=True)
    stop.set()
    reader.join(timeout=10)
    code = demo.wait()
    elapsed = time.monotonic() - t0
    mock.send_signal(signal.SIGTERM)
    try:
        mock.wait(timeout=5)
    except subprocess.TimeoutExpired:
        mock.kill()

    # the first hour against the last, or the first quarter against the last in short runs
    win = 3600 if elapsed >= 2 * 3600 else elapsed / 4
    first = (SAMPLE_S, win + SAMPLE_S)
    last = (elapsed - win, elapsed + 1)
    rss0, rss1 = window_median(samples, *first, "rss_kb"), window_median(samples, *last, "rss_kb")
    anon0, anon1 = window_median(samples, *first, "anon_kb"), window_median(samples, *last, "anon_kb")
    fds0 = max((s["fds"] for s in samples if first[0] <= s["t_s"] < first[1]), default=None)
    fds1 = max((s["fds"] for s in samples if last[0] <= s["t_s"] < last[1]), default=None)
    counts = events.snapshot()
    turns, reconnects = counts.get("turn_ready", 0), counts.get("reconnected", 0)
    fatal = [e for e in events.errors if not e.get("retriable")]
    checks = [
        (code == 0, f"the demo exited {code}"),
        (elapsed >= duration * 0.98, f"the demo ran {elapsed:.0f} s of {duration:.0f}"),
        (len(samples) >= 3, f"{len(samples)} samples"),
        (rss0 is not None and rss1 is not None and rss1 - rss0 <= MAX_GROWTH_KB,
         f"resident memory {rss0} -> {rss1} KB (at most +{MAX_GROWTH_KB})"),
        (fds0 is not None and fds1 is not None and fds1 <= fds0, f"open fds {fds0} -> {fds1}"),
        (turns >= 0.8 * duration / 30, f"{turns} turns (one every 30 s expected)"),
        (duration < 1200 or reconnects >= 0.8 * duration / 600, f"{reconnects} reconnects (one every 10 min)"),
        (not fatal, f"errors that are not retriable: {fatal[:3]}"),
    ]
    failed = [msg for ok, msg in checks if not ok]
    report = {"duration_s": duration, "elapsed_s": round(elapsed), "exit_code": code, "window_s": round(win),
              "rss_kb": [rss0, rss1], "anon_kb": [anon0, anon1], "fds": [fds0, fds1],
              "threads_max": max((s["threads"] for s in samples), default=None),
              "events": counts, "errors": events.errors[-10:], "summary": events.summary,
              "failed": failed, "passed": not failed}
    with open(os.path.join(out, "report.json"), "w") as f:
        json.dump(report, f, indent=2)
    for msg in failed:
        print("FAIL:", msg)
    print(f"soak: {'passed' if not failed else 'FAILED'} after {elapsed / 3600:.2f} h: rss {rss0} -> {rss1} KB, "
          f"anon {anon0} -> {anon1} KB, fds {fds0} -> {fds1}, {turns} turns, {reconnects} reconnects, "
          f"{counts.get('error', 0)} errors; {out}/report.json")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
