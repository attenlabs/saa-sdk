#!/usr/bin/env python3
"""Runs saa_client_demo against the mock server, one scenario per API key, and
checks the demo's JSON lines and exit code.

usage: conformance.py DEMO_BINARY [--only NAME,...] [--keep DIR]
"""
import argparse
import concurrent.futures
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))


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


def events_of(run, name):
    return [e for e in run["events"] if e["event"] == name]


def order_ok(run, *names):
    """True if the first occurrences of names appear in this order."""
    idx = []
    for n in names:
        pos = next((i for i, e in enumerate(run["events"]) if e["event"] == n), None)
        if pos is None:
            return False
        idx.append(pos)
    return idx == sorted(idx)


# Each scenario: the mock's key, demo arguments, and checks on the run.
def check_ok(r):
    turns = events_of(r, "turn_ready")
    disc = events_of(r, "disconnected")
    return [
        (r["exit"] == 0, f"exit {r['exit']}"),
        (order_ok(r, "connected", "started", "warmup_complete", "turn_ready", "disconnected"),
         "events out of order"),
        (len(turns) == 1 and turns[0]["samples"] == 32000, f"turn_ready {turns}"),
        (bool(events_of(r, "config")), "no config echo of the threshold"),
        (bool(disc) and disc[-1]["code"] == 1000 and disc[-1]["reason"] == "client stop", f"disconnected {disc}"),
        (not events_of(r, "error"), f"errors {events_of(r, 'error')}"),
        (r["summary"].get("audio_frames", 0) >= 60 and r["summary"].get("bad_frames") == 0,
         f"mock saw {r['summary']}"),
        ("server_profile=audio_only" in r["summary"].get("path", ""), "inferred profile missing"),
    ]


def check_auth(r):
    errs = events_of(r, "error")
    return [
        (r["exit"] == 2, f"exit {r['exit']}"),
        (len(errs) == 1 and errs[0]["kind"] == "auth" and errs[0]["code"] == 401 and not errs[0]["retriable"],
         f"errors {errs}"),
        (not events_of(r, "connected") and not events_of(r, "reconnecting"), "should not connect or retry"),
    ]


def check_ratelimit(r):
    errs = events_of(r, "error")
    return [
        (r["exit"] == 3, f"exit {r['exit']}"),
        (len(errs) == 1 and errs[0]["kind"] == "rate_limit" and errs[0]["retriable"], f"errors {errs}"),
        (not events_of(r, "reconnecting"), "the first connect must fail fast"),
    ]


def check_close1008(r):
    errs = events_of(r, "error")
    disc = events_of(r, "disconnected")
    return [
        (r["exit"] == 2, f"exit {r['exit']}"),
        (bool(disc) and disc[0]["code"] == 1008, f"disconnected {disc}"),
        (len(errs) == 1 and errs[0]["kind"] == "auth" and not errs[0]["retriable"], f"errors {errs}"),
        (not events_of(r, "reconnecting"), "1008 must not reconnect"),
    ]


def check_drop(r):
    disc = events_of(r, "disconnected")
    rec = events_of(r, "reconnecting")
    return [
        (r["exit"] == 0, f"exit {r['exit']}"),
        (bool(disc) and disc[0]["code"] == 1011, f"disconnected {disc}"),
        (bool(rec) and rec[0]["attempt"] == 1 and rec[0]["last_code"] == 1011, f"reconnecting {rec}"),
        (order_ok(r, "disconnected", "reconnecting", "reconnected"), "reconnect events out of order"),
        (len(events_of(r, "started")) >= 2, "no second started"),
        (len(events_of(r, "config")) >= 2, "threshold not re-sent after the second started"),
        (not events_of(r, "error"), f"a reconnect should suppress the error: {events_of(r, 'error')}"),
    ]


def check_silent(r):
    errs = events_of(r, "error")
    disc = events_of(r, "disconnected")
    stall = [e for e in errs if e["title"] == "Connection Stalled"]
    t_conn = events_of(r, "connected")[0]["ts_ms"] if events_of(r, "connected") else 0
    return [
        (bool(stall) and stall[0]["retriable"], f"errors {errs}"),
        (bool(stall) and 15000 <= stall[0]["ts_ms"] - t_conn <= 16000,
         f"stall after {stall[0]['ts_ms'] - t_conn if stall else None} ms"),
        (bool(disc) and disc[0]["code"] == 4000 and disc[0]["reason"] == "stall", f"disconnected {disc}"),
        (bool(events_of(r, "reconnecting")), "no reconnect after the stall"),
    ]


def check_acceptclose(r):
    rec = events_of(r, "reconnecting")
    errs = events_of(r, "error")
    final = errs[-1] if errs else {}
    return [
        (r["exit"] == 4, f"exit {r['exit']}"),
        ([e["attempt"] for e in rec] == [1, 2, 3], f"attempts {[e['attempt'] for e in rec]}"),
        (final.get("title") == "Reconnect Failed" and not final.get("retriable"), f"final error {final}"),
        (not events_of(r, "started"), "the mock never sends started"),
    ]


def check_noecho(r):
    return [
        (r["exit"] == 0, f"exit {r['exit']}"),
        (bool(events_of(r, "started")), "no started"),
    ]


SCENARIOS = {
    "ok":        ("ok-conformance",  ["--tail", "2"],                check_ok),
    "auth":      ("auth",            ["--duration", "3"],            check_auth),
    "ratelimit": ("ratelimit",       ["--duration", "3"],            check_ratelimit),
    "close1008": ("close1008",       ["--duration", "3"],            check_close1008),
    "drop":      ("drop",            ["--duration", "8"],            check_drop),
    "silent":    ("silent",          ["--duration", "18", "--tail", "12"], check_silent),
    "noecho":    ("noecho",          ["--duration", "3"],            check_noecho),
    "acceptclose": ("acceptclose",   ["--duration", "20", "--max-reconnects", "3"], check_acceptclose),
}


def run_scenario(name, demo, http_port, ws_port, wav, tmp, summaries):
    key, extra, _ = SCENARIOS[name]
    out = os.path.join(tmp, f"{name}.jsonl")
    cmd = [demo, "--url", f"http://127.0.0.1:{http_port}", "--wav", wav, "--events", out] + extra
    env = dict(os.environ, SAA_API_KEY=key)
    started = time.monotonic()
    try:
        p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
        code, stderr = p.returncode, p.stderr
    except subprocess.TimeoutExpired:
        code, stderr = "timeout", ""
    events = []
    if os.path.exists(out):
        with open(out) as f:
            events = [json.loads(line) for line in f if line.strip()]
    time.sleep(0.3)                                        # the mock writes its summary on close
    summary = {}
    if os.path.exists(summaries):
        with open(summaries) as f:
            mine = [json.loads(line) for line in f if line.strip() and json.loads(line)["scenario"] == key]
        summary = mine[0] if mine else {}
    return {"name": name, "exit": code, "events": events, "summary": summary, "stderr": stderr,
            "seconds": time.monotonic() - started}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("demo")
    ap.add_argument("--only", help="comma-separated scenario names")
    ap.add_argument("--keep", help="copy the JSON lines here")
    args = ap.parse_args()
    names = args.only.split(",") if args.only else list(SCENARIOS)

    tmp = tempfile.mkdtemp(prefix="saa-conformance-")
    try:
        subprocess.run([sys.executable, os.path.join(HERE, "gen_audio.py"), os.path.join(tmp, "audio")],
                       check=True, stdout=subprocess.DEVNULL)
        wav = os.path.join(tmp, "audio", "speech_48k_stereo.wav")
        summaries = os.path.join(tmp, "mock_summary.jsonl")
        http_port, ws_port = free_port(), free_port()
        mock = subprocess.Popen([sys.executable, os.path.join(HERE, "mock_server.py"),
                                 "--http-port", str(http_port), "--ws-port", str(ws_port),
                                 "--summary", summaries],
                                stdout=open(os.path.join(tmp, "mock.log"), "w"), stderr=subprocess.STDOUT)
        try:
            if not wait_listening(http_port) or not wait_listening(ws_port):
                print("mock server did not start")
                return 1
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(names)) as pool:
                futs = [pool.submit(run_scenario, n, args.demo, http_port, ws_port, wav, tmp, summaries)
                        for n in names]
                runs = [f.result() for f in futs]
        finally:
            mock.terminate()
            mock.wait(timeout=5)

        failed = 0
        for r in runs:
            problems = [msg for ok, msg in SCENARIOS[r["name"]][2](r) if not ok]
            status = "ok" if not problems else "FAIL"
            print(f"{r['name']:<10} {status:<4} {r['seconds']:5.1f}s")
            for msg in problems:
                print(f"    - {msg}")
            if problems:
                failed += 1
                if r["stderr"]:
                    print("    stderr:", r["stderr"].strip()[:400])
        if args.keep:
            shutil.copytree(tmp, args.keep, dirs_exist_ok=True)
        return 1 if failed else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
