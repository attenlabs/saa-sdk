#!/usr/bin/env python3
"""Runs saa_client_demo against the mock server, one scenario per API key, and
checks the demo's JSON lines, its exit code, and what the mock received.

usage: conformance.py DEMO_BINARY [--only NAME,...] [--keep DIR] [--require-tls]

The TLS scenarios need the openssl command to make a throwaway CA; without it
they are skipped, unless --require-tls is given.
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
THRESHOLD = 0.7                      # the demo's default --threshold


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


# ── helpers for the checks ────────────────────────────────────────────

def events_of(run, name):
    return [e for e in run["events"] if e["event"] == name]


def first(run, name):
    found = events_of(run, name)
    return found[0] if found else {}


def index_of(run, name):
    return next((i for i, e in enumerate(run["events"]) if e["event"] == name), None)


def order_ok(run, *names):
    """True if the first occurrences of names appear in this order."""
    idx = [index_of(run, n) for n in names]
    return None not in idx and idx == sorted(idx)


def order_after(run, start, *names):
    """True if, after the first `start`, the first occurrences of names appear in this order."""
    i = index_of(run, start)
    return i is not None and order_ok({"events": run["events"][i:]}, *names)


def only_final_stop(run):
    disc = events_of(run, "disconnected")
    return len(disc) == 1 and disc[0]["code"] == 1000 and disc[0]["reason"] == "client stop"


def session(run, n):
    """The mock's summary of the run's nth WebSocket session (from 1), or {}."""
    return next((s for s in run["sessions"] if s["session"] == n), {})


def sent(summary, action):
    return [a for a in summary.get("actions", []) if a.get("action") == action]


def resynced(summary):
    return any(abs(a.get("value", -1) - THRESHOLD) < 1e-6 for a in sent(summary, "set_threshold"))


def gap(a, b):
    """Milliseconds from event a to event b; NaN, which fails every comparison, if either is missing."""
    return b["ts_ms"] - a["ts_ms"] if a and b else float("nan")


# ── scenarios ─────────────────────────────────────────────────────────

def check_ok(r):
    turns = events_of(r, "turn_ready")
    t = turns[0] if turns else {}
    frames = t.get("frames", [])
    states = [e["state"] for e in events_of(r, "state")]
    idle = next((i for i, e in enumerate(r["events"]) if e["event"] == "state" and e["state"] == "idle"), -1)
    s = session(r, 1)
    return [
        (r["exit"] == 0, f"exit {r['exit']}"),
        (order_ok(r, "connected", "started", "warmup_complete", "turn_ready", "interrupt", "disconnected"),
         "events out of order"),
        (len(turns) == 1 and t["samples"] == 32000 and t["samples"] == round(t["duration_sec"] * 16000),
         f"turn_ready {turns}"),
        ([f["bytes"] for f in frames] == [102, 206] and [f["ts_offset_s"] for f in frames] == [-0.5, 0.25],
         f"turn frames {frames}"),
        (states == ["listening", "sending", "idle"], f"states {states}"),
        (idle > (index_of(r, "turn_ready") or len(r["events"])), "state idle should follow the turn"),
        (len(events_of(r, "interrupt")) == 1 and first(r, "interrupt")["fade_ms"] == 300,
         f"interrupt {events_of(r, 'interrupt')}"),
        (bool(events_of(r, "config")), "no config echo of the threshold"),
        (only_final_stop(r), f"disconnected {events_of(r, 'disconnected')}"),
        (not events_of(r, "error"), f"errors {events_of(r, 'error')}"),
        (s.get("audio_frames", 0) >= 70 and s.get("bad_frames") == 0, f"mock saw {s}"),
        ("server_profile=audio_only" in s.get("path", ""), "inferred profile missing"),
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
        (len(errs) == 1 and errs[0]["kind"] == "auth" and not errs[0]["retriable"]
         and errs[0]["detail"] == "auth rejected", f"errors {errs}"),
        (not events_of(r, "reconnecting"), "1008 must not reconnect"),
    ]


def check_drop(r):
    disc = events_of(r, "disconnected")
    rec = events_of(r, "reconnecting")
    return [
        (r["exit"] == 0, f"exit {r['exit']}"),
        (bool(disc) and disc[0]["code"] == 1006 and not disc[0]["was_clean"], f"disconnected {disc}"),
        (bool(rec) and rec[0]["last_code"] == 1006, f"reconnecting {rec}"),
        # every session got started, so the attempt count starts over each time
        (len(rec) >= 2 and all(e["attempt"] == 1 for e in rec), f"attempts {[e['attempt'] for e in rec]}"),
        (order_after(r, "disconnected", "reconnecting", "connected", "reconnected", "started"),
         "reconnect events out of order"),
        (len(events_of(r, "started")) >= 2, "no second started"),
        (resynced(session(r, 2)), f"threshold not re-sent in session 2: {session(r, 2).get('actions')}"),
        (not events_of(r, "error"), f"a reconnect should suppress the error: {events_of(r, 'error')}"),
    ]


def check_silent(r):
    errs = events_of(r, "error")
    disc = events_of(r, "disconnected")
    stall = [e for e in errs if e["title"] == "Connection Stalled"]
    t_conn = first(r, "connected")
    return [
        (bool(stall) and stall[0]["retriable"], f"errors {errs}"),
        (bool(stall) and 15000 <= gap(t_conn, stall[0]) <= 16000,
         f"stall after {gap(t_conn, stall[0]) if stall else None} ms"),
        (bool(disc) and disc[0]["code"] == 4000 and disc[0]["reason"] == "stall", f"disconnected {disc}"),
        (bool(events_of(r, "reconnecting")), "no reconnect after the stall"),
    ]


def check_blackhole(r):
    errs = events_of(r, "error")
    stall = [e for e in errs if e["title"] == "Connection Stalled"]
    st = stall[0] if stall else {}
    disc = events_of(r, "disconnected")
    rec = events_of(r, "reconnecting")
    return [
        (r["exit"] == 0, f"exit {r['exit']}"),
        (bool(stall) and st["retriable"] and len(errs) == 1, f"errors {errs}"),
        (bool(stall) and 15000 <= gap(first(r, "connected"), st) <= 16000,
         f"stall after {gap(first(r, 'connected'), st) if stall else None} ms"),
        (bool(disc) and disc[0]["code"] == 4000 and disc[0]["reason"] == "stall", f"disconnected {disc}"),
        # the close handshake cannot finish, so the kill timer takes the socket down
        (bool(stall and disc) and 900 <= gap(st, disc[0]) <= 1500,
         f"socket gone {gap(st, disc[0]) if stall and disc else None} ms after the stall"),
        (bool(rec) and rec[0]["attempt"] == 1 and rec[0]["last_code"] == 4000, f"reconnecting {rec}"),
        (len(events_of(r, "connected")) >= 2 and bool(events_of(r, "reconnected")), "no reconnect"),
        (len(events_of(r, "started")) >= 2, "no second started"),
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


def check_upgrade401(r):
    errs = events_of(r, "error")
    e = errs[0] if errs else {}
    return [
        (r["exit"] == 2, f"exit {r['exit']}"),
        (len(errs) == 1 and e["kind"] == "auth" and e["code"] == 401 and not e["retriable"]
         and "upgrade" in e["message"], f"errors {errs}"),
        (not events_of(r, "connected") and not events_of(r, "reconnecting"), "should not connect or retry"),
    ]


def check_upgrade503(r):
    errs = events_of(r, "error")
    e = errs[0] if errs else {}
    return [
        (r["exit"] == 3, f"exit {r['exit']}"),
        (len(errs) == 1 and e["kind"] == "transport" and e["code"] == 503 and e["retriable"]
         and "upgrade" in e["message"], f"errors {errs}"),
        (not events_of(r, "connected") and not events_of(r, "reconnecting"), "the first connect must fail fast"),
    ]


def check_auth_flap(r):
    errs = events_of(r, "error")
    rec = events_of(r, "reconnecting")
    return [
        (r["exit"] == 0, f"exit {r['exit']}"),
        (len(errs) == 2 and all(e["kind"] == "auth" and e["code"] == 401 and e["retriable"] for e in errs),
         f"errors {errs}"),
        ([(e["attempt"], e["last_code"]) for e in rec] == [(1, 1011), (2, 401), (3, 401)],
         f"reconnecting {[(e['attempt'], e['last_code']) for e in rec]}"),
        (first(r, "reconnected").get("attempts") == 3, f"reconnected {events_of(r, 'reconnected')}"),
        (order_after(r, "disconnected", "reconnecting", "error", "connected", "reconnected", "started"),
         "events out of order"),
        (len(events_of(r, "started")) == 2, "expected exactly two started"),
        (resynced(session(r, 2)), f"threshold not re-sent in session 2: {session(r, 2).get('actions')}"),
    ]


def check_ratelimit_reconnect(r):
    rec = events_of(r, "reconnecting")
    conn = events_of(r, "connected")
    after = rec[1] if len(rec) > 1 else {}
    return [
        (r["exit"] == 0, f"exit {r['exit']}"),
        (not events_of(r, "error"), f"errors {events_of(r, 'error')}"),
        (len(rec) == 2 and rec[0]["last_code"] == 1011 and after["last_code"] == 429 and after["delay_ms"] >= 2000,
         f"reconnecting {rec}"),
        # Retry-After is honoured: nothing reconnects before it has passed
        (len(conn) == 2 and gap(after, conn[1]) >= 1900,
         f"reconnected {gap(after, conn[1]) if len(conn) > 1 else None} ms after the 429"),
        (first(r, "reconnected").get("attempts") == 2, f"reconnected {events_of(r, 'reconnected')}"),
        (len(events_of(r, "started")) == 2, "expected exactly two started"),
        (resynced(session(r, 2)), f"threshold not re-sent in session 2: {session(r, 2).get('actions')}"),
    ]


def check_garbage(r):
    cfgs = [e["model_class2_threshold"] for e in events_of(r, "config")]
    turns = events_of(r, "turn_ready")
    return [
        (r["exit"] == 0, f"exit {r['exit']}"),
        (not events_of(r, "error"), f"errors {events_of(r, 'error')}"),
        (only_final_stop(r), f"disconnected {events_of(r, 'disconnected')}"),
        (bool(cfgs) and all(abs(v - THRESHOLD) < 1e-6 for v in cfgs), f"config values {cfgs}"),
        (not [e for e in events_of(r, "state") if e["state"] not in ("listening", "sending", "idle")],
         f"states {events_of(r, 'state')}"),
        (len(turns) == 1 and turns[0]["samples"] == 32000, f"turn_ready {turns}"),
    ]


def check_bigturn(r):
    turns = events_of(r, "turn_ready")
    return [
        (r["exit"] == 0, f"exit {r['exit']}"),
        (len(turns) == 1 and turns[0]["samples"] == 3_000_000, f"turn_ready {turns}"),
        (not events_of(r, "error"), f"errors {events_of(r, 'error')}"),
        (only_final_stop(r), f"disconnected {events_of(r, 'disconnected')}"),
    ]


def check_toobig(r):
    errs = events_of(r, "error")
    return [
        (r["exit"] == 0, f"exit {r['exit']}"),
        (len(errs) == 1 and errs[0]["title"] == "Message Too Large" and errs[0]["kind"] == "transport"
         and errs[0]["retriable"], f"errors {errs}"),
        (only_final_stop(r), f"disconnected {events_of(r, 'disconnected')}"),
        (order_ok(r, "error", "turn_ready"), "the session should continue after the dropped message"),
    ]


def check_utterance(r):
    cfg = first(r, "utterance_config")
    ue = first(r, "utterance_ended")
    s = session(r, 1)
    want_ue = {"seq": 1, "text": "two burgers please", "prediction": 2, "respond": True,
               "reason": "addressed_to_device", "start_s": 1.25, "end_s": 3.5, "truncated": False,
               "assistant_turns": 0, "preview": True, "latency_ms": 140, "samples": 16000}
    return [
        (r["exit"] == 0, f"exit {r['exit']}"),
        ((s.get("allocate_body") or {}).get("utterance_handling") is True,
         f"allocate body {s.get('allocate_body')}"),
        ("utterance_handling=1" in s.get("path", ""), f"path {s.get('path')}"),
        (cfg.get("enabled") is True and cfg.get("class1_threshold") == 0.9 and cfg.get("preview") is True
         and cfg.get("reason") is None, f"utterance_config {cfg}"),
        (all(ue.get(k) == v for k, v in want_ue.items()) and abs(ue.get("confidence", 0) - 0.88) < 1e-6,
         f"utterance_ended {ue}"),
        (order_ok(r, "started", "utterance_config", "utterance_ended"), "utterance events out of order"),
        (not events_of(r, "error"), f"errors {events_of(r, 'error')}"),
    ]


def check_slowlink(r):
    stats = events_of(r, "stats")
    s = session(r, 1)
    window = s.get("audio_per_s", [])[-12:-2]           # ten full seconds before the end
    if sys.platform == "darwin":
        # The proxy cannot build a small-buffer bottleneck on macOS (see mock_server.py). The
        # queue then grows past the client, which cannot see it, and audio loses throughput
        # too: the limit of the skip rules. Only the rules themselves are checked here.
        r["note"] = "audio throughput not checked on macOS"
        return [
            (r["exit"] == 0, f"exit {r['exit']}"),
            (not events_of(r, "error"), f"errors {events_of(r, 'error')}"),
            (only_final_stop(r), f"disconnected {events_of(r, 'disconnected')}"),
            (bool(stats) and stats[-1]["sent_video"] > 0 and stats[-1]["skipped_video"] > 0,
             f"video sent and skipped: {[(e['sent_video'], e['skipped_video']) for e in stats]}"),
        ]
    return [
        (r["exit"] == 0, f"exit {r['exit']}"),
        (not events_of(r, "error"), f"errors {events_of(r, 'error')}"),
        (only_final_stop(r), f"disconnected {events_of(r, 'disconnected')}"),
        (len(stats) >= 2, f"{len(stats)} stats events"),
        (bool(stats) and stats[-1]["sent_video"] > 0, f"no video got through: {stats[-1:]}"),
        (bool(stats) and stats[-1]["skipped_video"] > 0,
         f"video never skipped: {[(e['sent_video'], e['skipped_video']) for e in stats]}"),
        (all(e["skipped_audio"] == 0 for e in stats), f"audio skipped: {[e['skipped_audio'] for e in stats]}"),
        (len(window) == 10 and sum(window) >= 90,
         f"server audio frames per second near the end: {window} (all: {s.get('audio_per_s')})"),
    ]


def check_tls(r):
    turns = events_of(r, "turn_ready")
    return [
        (r["exit"] == 0, f"exit {r['exit']}"),
        (bool(events_of(r, "started")) and session(r, 1).get("tls") is True, "no started over TLS"),
        (len(turns) == 1 and turns[0]["samples"] == 3_000_000, f"turn_ready {turns}"),
        (not events_of(r, "error"), f"errors {events_of(r, 'error')}"),
        (only_final_stop(r), f"disconnected {events_of(r, 'disconnected')}"),
    ]


def check_tls_noca(r):
    errs = events_of(r, "error")
    return [
        (r["exit"] == 4, f"exit {r['exit']}"),
        (len(errs) == 1 and errs[0]["kind"] == "transport", f"errors {errs}"),
        (not events_of(r, "connected"), "connected without trusting the server's CA"),
    ]


# name: (key, how the demo reaches the mock, demo arguments, check)
SCENARIOS = {
    "ok":                  ("ok-conformance", "broker", ["--tail", "2"], check_ok),
    "auth":                ("auth", "broker", ["--duration", "3"], check_auth),
    "ratelimit":           ("ratelimit", "broker", ["--duration", "3"], check_ratelimit),
    "close1008":           ("close1008", "broker", ["--duration", "3"], check_close1008),
    "drop":                ("drop", "broker", ["--duration", "8"], check_drop),
    "silent":              ("silent", "broker", ["--duration", "18", "--tail", "12"], check_silent),
    "noecho":              ("noecho", "broker", ["--duration", "3"], check_noecho),
    "acceptclose":         ("acceptclose", "broker", ["--duration", "20", "--max-reconnects", "3"], check_acceptclose),
    "upgrade401":          ("upgrade401", "broker", ["--duration", "3"], check_upgrade401),
    "upgrade503":          ("upgrade503", "direct", ["--duration", "3"], check_upgrade503),
    "auth_flap":           ("auth_flap", "broker", ["--duration", "9"], check_auth_flap),
    "ratelimit_reconnect": ("ratelimit_reconnect", "broker", ["--duration", "8"], check_ratelimit_reconnect),
    "blackhole":           ("blackhole", "blackhole", ["--duration", "22", "--tail", "15", "--jpeg-dir", "{jpeg}"],
                            check_blackhole),
    "garbage":             ("garbage", "broker", ["--duration", "9"], check_garbage),
    "bigturn":             ("bigturn", "broker", ["--duration", "9"], check_bigturn),
    "toobig":              ("toobig", "broker", ["--duration", "9"], check_toobig),
    "utterance":           ("utterance", "broker", ["--duration", "8", "--utterance"], check_utterance),
    "slowlink":            ("slowlink", "slowlink", ["--duration", "32", "--tail", "25", "--jpeg-dir", "{jpeg}"],
                            check_slowlink),
    "tls":                 ("tls", "tls", ["--duration", "9", "--ca", "{ca}"], check_tls),
    "tls_noca":            ("tls_noca", "tls", ["--duration", "3"], check_tls_noca),
}
TLS_SCENARIOS = {"tls", "tls_noca"}


def make_ca(d):
    """A throwaway CA and a localhost certificate. Returns (ca, cert, key) or None."""
    openssl = shutil.which("openssl")
    if not openssl:
        return None
    d = os.path.abspath(d)
    cnf = os.path.join(d, "openssl.cnf")
    with open(cnf, "w") as f:
        f.write("[req]\ndistinguished_name = dn\n[dn]\n"
                "[v3_ca]\nbasicConstraints = critical,CA:TRUE\nkeyUsage = critical,keyCertSign,cRLSign\n"
                "subjectKeyIdentifier = hash\n"
                "[v3_srv]\nbasicConstraints = CA:FALSE\nkeyUsage = critical,digitalSignature,keyEncipherment\n"
                "extendedKeyUsage = serverAuth\nsubjectAltName = DNS:localhost,IP:127.0.0.1\n"
                "authorityKeyIdentifier = keyid\n")
    p = {n: os.path.join(d, n) for n in ("ca.pem", "ca.key", "server.pem", "server.key", "server.csr")}
    steps = [
        ["req", "-x509", "-new", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=saa-c test CA",
         "-config", cnf, "-extensions", "v3_ca", "-keyout", p["ca.key"], "-out", p["ca.pem"]],
        ["req", "-new", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=localhost", "-config", cnf,
         "-keyout", p["server.key"], "-out", p["server.csr"]],
        ["x509", "-req", "-days", "2", "-in", p["server.csr"], "-CA", p["ca.pem"], "-CAkey", p["ca.key"],
         "-CAcreateserial", "-extfile", cnf, "-extensions", "v3_srv", "-out", p["server.pem"]],
    ]
    for args in steps:
        res = subprocess.run([openssl] + args, capture_output=True, text=True, cwd=d)
        if res.returncode:
            print(f"openssl {args[0]} failed: {res.stderr.strip()[:300]}")
            return None
    return p["ca.pem"], p["server.pem"], p["server.key"]


def make_jpegs(d, count=4, size=20_000):
    """Stand-ins for 640x480 frames: the client checks nothing but the length."""
    os.makedirs(d, exist_ok=True)
    for i in range(count):
        with open(os.path.join(d, f"frame{i}.jpg"), "wb") as f:
            f.write(b"\xff\xd8\xff\xe0" + os.urandom(size - 6) + b"\xff\xd9")
    return d


def run_scenario(name, demo, urls, wav, tmp, fill):
    key, via, extra, _ = SCENARIOS[name]
    out = os.path.join(tmp, f"{name}.jsonl")
    cmd = [demo, "--url", urls[via], "--wav", wav, "--events", out] + [a.format(**fill) for a in extra]
    env = dict(os.environ, SAA_API_KEY=key)
    started = time.monotonic()
    try:
        p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
        code, stderr = p.returncode, p.stderr
    except subprocess.TimeoutExpired:
        code, stderr = "timeout", ""
    with open(os.path.join(tmp, f"{name}.stderr"), "w") as f:
        f.write(stderr)
    events = []
    if os.path.exists(out):
        with open(out) as f:
            events = [json.loads(line) for line in f if line.strip()]
    return {"name": name, "key": key, "exit": code, "events": events, "stderr": stderr,
            "seconds": time.monotonic() - started}


def read_summaries(paths):
    rows = []
    for path in paths:
        if os.path.exists(path):
            with open(path) as f:
                rows += [json.loads(line) for line in f if line.strip()]
    return rows


def settle(paths, quiet_s=1.0, limit_s=8.0):
    """Waits until the mocks stop writing summaries: they write one when a session closes."""
    end = time.monotonic() + limit_s
    last, since = -1, time.monotonic()
    while time.monotonic() < end:
        n = len(read_summaries(paths))
        if n != last:
            last, since = n, time.monotonic()
        elif time.monotonic() - since >= quiet_s:
            return
        time.sleep(0.1)


def start_mock(tmp, name, args, summary):
    log = open(os.path.join(tmp, f"{name}.log"), "w")
    return subprocess.Popen([sys.executable, os.path.join(HERE, "mock_server.py"), "--summary", summary] + args,
                            stdout=log, stderr=subprocess.STDOUT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("demo")
    ap.add_argument("--only", help="comma-separated scenario names")
    ap.add_argument("--keep", help="copy the JSON lines, stderr, and mock logs here")
    ap.add_argument("--require-tls", action="store_true", help="fail, not skip, without openssl")
    args = ap.parse_args()
    names = args.only.split(",") if args.only else list(SCENARIOS)
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        print(f"unknown scenarios: {', '.join(unknown)}")
        return 2

    tmp = tempfile.mkdtemp(prefix="saa-conformance-")
    mocks = []
    try:
        subprocess.run([sys.executable, os.path.join(HERE, "gen_audio.py"), os.path.join(tmp, "audio")],
                       check=True, stdout=subprocess.DEVNULL)
        wav = os.path.join(tmp, "audio", "speech_48k_stereo.wav")
        fill = {"jpeg": make_jpegs(os.path.join(tmp, "jpeg"))}
        skipped = []

        # The main mock serves the scenarios that need no proxy. The proxies get a process of
        # their own, so that the other scenarios' big messages cannot slow them down. Only the
        # mocks' own ports are probed: a probe would count as a proxy's first connection.
        http, ws, proxied_ws, slow, dark = (free_port() for _ in range(5))
        summaries = [os.path.join(tmp, "mock_summary.jsonl"), os.path.join(tmp, "proxied_summary.jsonl")]
        main_args = ["--http-port", str(http), "--ws-port", str(ws)]
        listening = [http, ws]
        urls = {"broker": f"http://127.0.0.1:{http}", "direct": f"ws://127.0.0.1:{ws}/ws",
                "slowlink": f"ws://127.0.0.1:{slow}/ws", "blackhole": f"ws://127.0.0.1:{dark}/ws"}
        if TLS_SCENARIOS & set(names):
            certs = make_ca(tmp)
            if certs:
                https, wss = free_port(), free_port()
                fill["ca"] = certs[0]
                main_args += ["--tls-cert", certs[1], "--tls-key", certs[2],
                              "--https-port", str(https), "--wss-port", str(wss)]
                listening += [https, wss]
                urls["tls"] = f"https://localhost:{https}"
            elif args.require_tls:
                print("the TLS scenarios need the openssl command")
                return 1
            else:
                skipped = [n for n in names if n in TLS_SCENARIOS]
                names = [n for n in names if n not in TLS_SCENARIOS]
        mocks.append(start_mock(tmp, "mock", main_args, summaries[0]))
        if {"slowlink", "blackhole"} & set(names):
            mocks.append(start_mock(tmp, "proxied", ["--http-port", str(free_port()), "--ws-port", str(proxied_ws),
                                                     "--slowlink-port", str(slow), "--blackhole-port", str(dark)],
                                    summaries[1]))
            listening.append(proxied_ws)                   # its proxies listen before it does
        if not all(wait_listening(p) for p in listening):
            print("mock server did not start")
            return 1

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(names)) as pool:
            futs = [pool.submit(run_scenario, n, args.demo, urls, wav, tmp, fill) for n in names]
            runs = [f.result() for f in futs]
        settle(summaries)
        rows = read_summaries(summaries)
        for r in runs:
            r["sessions"] = sorted((s for s in rows if s["scenario"] == r["key"]), key=lambda s: s["session"])

        failed = 0
        for r in runs:
            try:
                problems = [msg for ok, msg in SCENARIOS[r["name"]][3](r) if not ok]
            except Exception as e:                   # a missing field: report it, check the rest
                problems = [f"check raised {type(e).__name__}: {e}"]
            status = "ok" if not problems else "FAIL"
            note = f"  ({r['note']})" if r.get("note") else ""
            print(f"{r['name']:<20} {status:<4} {r['seconds']:5.1f}s{note}")
            for msg in problems:
                print(f"    - {msg}")
            if problems:
                failed += 1
                if r["stderr"]:
                    print("    stderr:", r["stderr"].strip()[:600])
        for n in skipped:
            print(f"{n:<20} skip (no openssl)")
        if args.keep:
            shutil.copytree(tmp, args.keep, dirs_exist_ok=True)
        return 1 if failed else 0
    finally:
        for m in mocks:
            m.terminate()
            try:
                m.wait(timeout=5)
            except subprocess.TimeoutExpired:
                m.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
