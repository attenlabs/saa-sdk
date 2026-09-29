#!/usr/bin/env python3
"""live_smoke.py - streams a speech WAV to the real service and checks that a
turn_ready comes back. Needs an API key in SAA_API_KEY; it is not run in CI.

usage: live_smoke.py DEMO_BINARY WAV [--url URL] [--ca FILE] [--timeout S] [--json]

The demo streams silence until warmup_complete, then the WAV, then a few seconds
of silence, so the service hears the whole utterance. Use a recording of one
short request spoken to the device, such as "Can I get two burgers, please?".
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("demo")
    ap.add_argument("wav")
    ap.add_argument("--url", help="broker or direct URL (default: the demo's)")
    ap.add_argument("--ca", help="CA bundle, if the system roots do not cover the service")
    ap.add_argument("--timeout", type=float, default=60.0, help="give up after this many seconds")
    ap.add_argument("--json", action="store_true", help="print one JSON object instead of text")
    args = ap.parse_args()
    if not os.environ.get("SAA_API_KEY"):
        print("live_smoke: set SAA_API_KEY to an API key", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "live.jsonl")
        cmd = [args.demo, "--wav", args.wav, "--wait-warmup", "--tail", "6", "--stats",
               "--events", out, "--duration", str(args.timeout)]
        if args.url:
            cmd += ["--url", args.url]
        if args.ca:
            cmd += ["--ca", args.ca]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout + 30)
            code, stderr = p.returncode, p.stderr
        except subprocess.TimeoutExpired:
            code, stderr = "timeout", ""
        events = []
        if os.path.exists(out):
            with open(out) as f:
                events = [json.loads(line) for line in f if line.strip()]

    def of(name):
        return [e for e in events if e["event"] == name]

    def first(name):
        found = of(name)
        return found[0] if found else {}

    turns, stats, summary = of("turn_ready"), of("stats"), first("summary")
    started, warm, wav_end = first("started"), first("warmup_complete"), first("wav_end")
    result = {
        "ok": code == 0 and len(turns) >= 1,
        "exit": code,
        "turns": len(turns),
        "turn_seconds": [t["duration_sec"] for t in turns],
        "warmup_ms": warm["ts_ms"] - started["ts_ms"] if warm and started else None,
        "turn_after_wav_end_ms": turns[0]["ts_ms"] - wav_end["ts_ms"] if turns and wav_end else None,
        "rtt_ms": [s["rtt_ms"] for s in stats if s.get("rtt_ms") is not None and s["rtt_ms"] >= 0],
        "rss_kb_at_turn": turns[0].get("rss_kb") if turns else None,
        "rss_anon_kb_at_turn": turns[0].get("rss_anon_kb") if turns else None,
        "peak_rss_kb": summary.get("peak_rss_kb"),
        "errors": [f"{e['kind']}: {e['title']}: {e['message']}" for e in of("error")],
        "version": first("demo_start").get("version"),
    }
    if not result["ok"] and stderr:
        result["stderr"] = stderr.strip()[-600:]
    if args.json:
        print(json.dumps(result))
    else:
        print(f"live smoke: {'ok' if result['ok'] else 'FAIL'} (exit {code}, {len(turns)} turn_ready)")
        for k, v in result.items():
            if k not in ("ok", "stderr"):
                print(f"  {k}: {v}")
        if result.get("stderr"):
            print("  stderr:", result["stderr"])
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
