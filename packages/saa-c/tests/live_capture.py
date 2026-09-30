#!/usr/bin/env python3
"""live_capture.py - a guided five-minute check of capture against the real service.

The demo captures a microphone, and a camera if given, through the library's own
capture module and streams them to the service. Meanwhile this script prompts
whoever is at the device through five phases: talk to the device, talk to someone
else, stay quiet, talk to the device facing the camera, and stay quiet again.
Turns are shown as they arrive.

At the end it reports, phase by phase, what the model predicted and the turns it
sent back. Overall it reports the warmup, the round-trip time, audio and video
sent and skipped, errors, CPU, and memory. It checks that the turn audio is not
silent, and that the frames the service returns decode, which shows the service
took the camera's own JPEGs.

It needs a capture build of the demo and an API key in SAA_API_KEY, and is not run
in CI. --mock runs it against the local mock server with no key instead, to check
the script and the devices.

usage: live_capture.py DEMO --alsa DEV [--channel N] [--v4l2 DEV] [--seconds S]
                       [--out DIR] [--url URL] [--mock]
"""
import argparse
import json
import math
import os
import socket
import statistics
import struct
import subprocess
import sys
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))

# (seconds in a 300 s run, name, prompt); the last phase runs to the end
PHASES = [
    (75, "talk", "Talk TO the device, as a customer would: order something, then pause for a few seconds. "
                 "Two or three requests."),
    (60, "other", "Now talk to someone else in the room, or read something aloud facing away from the device. "
                  "Don't address the device."),
    (30, "quiet", "Stay quiet."),
    (90, "camera", "Face the camera and talk to the device again: ask it a question or two."),
    (None, "end", "Stay quiet until the end."),
]
BASE_SPAN = sum(p[0] for p in PHASES[:-1])        # 255 s after warmup, in a 300 s run


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


def say(t0, text):
    print(f"{time.monotonic() - t0:6.1f} s  {text}", flush=True)


def wav_levels(path):
    """Duration, and peak and RMS in dBFS, of a 16-bit mono WAV."""
    with wave.open(path, "rb") as w:
        n = w.getnframes()
        data = w.readframes(n)
        rate = w.getframerate()
    samples = struct.unpack(f"<{len(data) // 2}h", data)
    if not samples:
        return {"seconds": 0.0, "peak_dbfs": None, "rms_dbfs": None}
    peak = max(abs(v) for v in samples)
    rms = math.sqrt(sum(v * v for v in samples) / len(samples))
    db = lambda v: round(20 * math.log10(v / 32768.0), 1) if v > 0 else None
    return {"seconds": round(n / rate, 2), "peak_dbfs": db(peak), "rms_dbfs": db(rms)}


def jpeg_decodes(path):
    """(width, height) if the JPEG decodes, None if it does not, "?" without Pillow."""
    try:
        from PIL import Image
    except ImportError:
        return "?"
    try:
        with Image.open(path) as im:
            im.load()
            return im.size
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("demo", help="saa_client_demo from a capture build")
    ap.add_argument("--alsa", required=True, help="the microphone, such as hw:CARD=Lite")
    ap.add_argument("--channel", type=int, default=0, help="the channel to keep (default 0)")
    ap.add_argument("--v4l2", help="an MJPEG camera, such as /dev/video2")
    ap.add_argument("--seconds", type=float, default=300.0, help="how long to run (default 300)")
    ap.add_argument("--out", help="where to keep the events, turns, and report (default: a new directory here)")
    ap.add_argument("--url", help="broker or direct URL (default: the demo's)")
    ap.add_argument("--mock", action="store_true", help="run against the local mock server, with no key")
    a = ap.parse_args()
    key = "capture-live-check" if a.mock else os.environ.get("SAA_API_KEY")
    if not key:
        print("live_capture: set SAA_API_KEY to an API key, or pass --mock", file=sys.stderr)
        return 2
    out = a.out or os.path.abspath(time.strftime("live-capture-%Y%m%d-%H%M%S"))
    os.makedirs(os.path.join(out, "turns"), exist_ok=True)
    events_path = os.path.join(out, "events.jsonl")

    mock, url = None, a.url
    if a.mock:
        http, ws = free_port(), free_port()
        mock = subprocess.Popen([sys.executable, os.path.join(HERE, "mock_server.py"), "--http-port", str(http),
                                 "--ws-port", str(ws)], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        if not wait_listening(http) or not wait_listening(ws):
            print("live_capture: the mock server did not start", file=sys.stderr)
            mock.kill()
            return 1
        url = f"http://127.0.0.1:{http}"
    cmd = [a.demo, "--alsa", a.alsa, "--channel", str(a.channel)] + (["--v4l2", a.v4l2] if a.v4l2 else []) + \
          ["--duration", str(a.seconds), "--stats", "--events", events_path, "--record-turns", os.path.join(out, "turns")] + \
          (["--url", url] if url else [])
    print(f"live_capture: {a.seconds:.0f} s from {a.alsa}{' and ' + a.v4l2 if a.v4l2 else ''} to "
          f"{'the mock' if a.mock else url or 'the service'}; results in {out}", flush=True)
    t0 = time.monotonic()
    with open(os.path.join(out, "demo.stderr"), "w") as err:
        demo = subprocess.Popen(cmd, env=dict(os.environ, SAA_API_KEY=key), stdout=subprocess.DEVNULL, stderr=err)
    events, pos, warm_wall, warm_ms, phase, turns = [], 0, None, None, -1, 0
    span = None                                   # the phases' lengths, once warmup is known
    say(t0, "connecting; stay quiet until the prompts start (the model warms up for about 12 s)")
    try:
        while True:
            done = demo.poll() is not None
            if os.path.exists(events_path):
                with open(events_path) as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
                for line in chunk.splitlines():
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    events.append(ev)
                    name = ev.get("event")
                    if name == "started":
                        say(t0, f"session {ev.get('session_id')} started")
                    elif name == "warmup_complete" and warm_ms is None:
                        warm_wall, warm_ms = time.monotonic(), ev["ts_ms"]
                        scale = max(0.2, (a.seconds - warm_ms / 1000.0 - 15.0) / BASE_SPAN)
                        span = [round(p[0] * scale) for p in PHASES[:-1]]
                        say(t0, f"warmup complete after {warm_ms / 1000.0:.1f} s")
                    elif name == "turn_ready":
                        turns += 1
                        say(t0, f"  turn {turns}: {ev.get('duration_sec', 0):.2f} s of audio, "
                                f"{len(ev.get('frames') or [])} frame(s)")
                    elif name == "error":
                        say(t0, f"  error: {ev.get('title')}: {ev.get('message')}"
                                f"{' (retriable)' if ev.get('retriable') else ''}")
                    elif name in ("disconnected", "reconnecting", "reconnected"):
                        say(t0, f"  {name} {({k: v for k, v in ev.items() if k not in ('ts_ms', 'event')})}")
            if warm_wall is not None:
                elapsed, edge, now_phase = time.monotonic() - warm_wall, 0, len(PHASES) - 1
                for i, s in enumerate(span):
                    edge += s
                    if elapsed < edge:
                        now_phase = i
                        break
                if now_phase != phase:
                    phase = now_phase
                    length = f" ({span[phase]} s)" if phase < len(span) else ""
                    say(t0, f">>> {phase + 1}/{len(PHASES)}{length}  {PHASES[phase][2]}")
            if done:
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        demo.terminate()
        demo.wait()
    finally:
        if mock:
            mock.terminate()
            mock.wait(timeout=5)
    return report(a, out, events, warm_ms, span, demo.returncode)


def report(a, out, events, warm_ms, span, exit_code):
    of = lambda n: [e for e in events if e.get("event") == n]
    summary = (of("summary") or [{}])[-1]
    stats = of("stats")
    started = (of("started") or [{}])[0]
    bounds = []
    if warm_ms is not None:
        edge = warm_ms
        for (_, name, _), s in zip(PHASES, span + [None]):
            end = edge + s * 1000 if s is not None else float("inf")
            bounds.append((name, edge, end))
            edge = end
    rows = []
    for name, lo, hi in bounds:
        inside = lambda e: lo <= e["ts_ms"] < hi
        preds = [e for e in of("prediction") if inside(e)]
        vads = [e for e in of("vad") if inside(e)]
        n = len(preds) or 1
        cls = [sum(1 for p in preds if p.get("cls") == k) for k in (0, 1, 2)]
        conf2 = [p["confidence"] for p in preds if p.get("cls") == 2 and p.get("confidence") is not None]
        rows.append({
            "phase": name, "predictions": len(preds),
            "cls_pct": [round(100.0 * c / n) for c in cls],
            "cls2_confidence": round(statistics.fmean(conf2), 2) if conf2 else None,
            "faces_pct": round(100.0 * sum(1 for p in preds if (p.get("num_faces") or 0) > 0) / n),
            "speech_pct": round(100.0 * sum(1 for v in vads if v.get("is_speech")) / (len(vads) or 1)),
            "turns": [round(t.get("duration_sec", 0), 2) for t in of("turn_ready") if inside(t)],
        })
    turn_files = []
    for t in of("turn_ready"):
        entry = {"duration_sec": t.get("duration_sec"), "frames": []}
        if t.get("wav") and os.path.exists(os.path.join(out, "turns", t["wav"])):
            entry.update(wav_levels(os.path.join(out, "turns", t["wav"])))
        for fr in t.get("frames") or []:
            path = os.path.join(out, "turns", fr["file"]) if fr.get("file") else None
            entry["frames"].append({"bytes": fr.get("bytes"), "decodes": jpeg_decodes(path) if path else None})
        turn_files.append(entry)
    rtts = [s["rtt_ms"] for s in stats if s.get("rtt_ms") is not None and s["rtt_ms"] >= 0]
    last = stats[-1] if stats else {}
    up = (last.get("uptime_ms") or 0) / 1000.0
    audio_rate = last.get("sent_audio", 0) / up if up else None
    video_rate = last.get("sent_video", 0) / up if up else None
    errors = [{k: e.get(k) for k in ("ts_ms", "kind", "title", "message", "retriable")} for e in of("error")]
    frames = [f for t in turn_files for f in t["frames"]]
    checks = [
        ("the demo exited 0", exit_code == 0, f"exit {exit_code}"),
        ("the session started and warmed up", bool(started) and warm_ms is not None,
         f"warmup {warm_ms / 1000.0:.1f} s" if warm_ms is not None else "no warmup_complete"),
        ("no errors that end the session", not [e for e in errors if not e["retriable"]], f"{len(errors)} error(s)"),
        # the first connection's count includes what the mic captured while it came up
        ("audio at 10 frames a second, none skipped once streaming", audio_rate is not None and audio_rate >= 9.5
         and last.get("skipped_audio", 0) == stats[0].get("skipped_audio", 0),
         f"{audio_rate:.2f}/s; {stats[0].get('skipped_audio')} skipped while connecting, "
         f"{last.get('skipped_audio', 0) - stats[0].get('skipped_audio', 0)} after" if audio_rate is not None
         else "no stats"),
    ]
    if a.v4l2:
        checks.append(("video at 4 frames a second, none skipped once streaming",
                       video_rate is not None and video_rate >= 3.9
                       and last.get("skipped_video", 0) == stats[0].get("skipped_video", 0),
                       f"{video_rate:.2f}/s; {stats[0].get('skipped_video')} skipped while connecting, "
                       f"{last.get('skipped_video', 0) - stats[0].get('skipped_video', 0)} after"
                       if video_rate is not None else "no stats"))
        if not a.mock:
            cam = next((r for r in rows if r["phase"] == "camera"), None)
            checks.append(("the service saw faces in the camera phase, so it decoded the camera's frames",
                           bool(cam and cam["faces_pct"] > 0), f"{cam['faces_pct'] if cam else 0}% of predictions"))
    checks.append(("turns came back", bool(turn_files), f"{len(turn_files)} turn(s)"))
    if turn_files and not a.mock:                  # the mock echoes whatever the mic heard, speech or not
        quiet = [t for t in turn_files if t.get("peak_dbfs") is None or t["peak_dbfs"] < -50]
        checks.append(("turn audio is not silent", not quiet, f"{len(quiet)} near-silent"))
    if frames and not a.mock:
        bad = [f for f in frames if f["decodes"] is None]
        unknown = "?" in [f["decodes"] for f in frames]
        checks.append(("the service's turn frames decode", None if unknown else not bad,
                       "not checked: this Python has no Pillow" if unknown
                       else f"{len(frames)} frame(s), {len(bad)} bad"))
    rep = {
        "seconds": a.seconds, "alsa": a.alsa, "v4l2": a.v4l2, "mock": a.mock, "exit_code": exit_code,
        "session_id": started.get("session_id"), "warmup_s": warm_ms / 1000.0 if warm_ms is not None else None,
        "phases": rows, "turns": turn_files, "errors": errors,
        "rtt_ms": {"p50": round(statistics.median(rtts), 1), "max": round(max(rtts), 1)} if rtts else None,
        "audio_per_s": round(audio_rate, 2) if audio_rate else None,
        "video_per_s": round(video_rate, 2) if video_rate else None, "last_stats": last,
        "cpu_pct": round(100.0 * summary["cpu_s"] / summary["wall_s"], 2) if summary.get("wall_s") else None,
        "peak_rss_kb": summary.get("peak_rss_kb"),
        "checks": [{"check": c, "ok": ok, "detail": d} for c, ok, d in checks],
    }
    with open(os.path.join(out, "report.json"), "w") as f:
        json.dump(rep, f, indent=2)

    print()
    print("phase    preds   not/other/device %   dev conf  faces %  speech %  turns (s)")
    for r in rows:
        c = r["cls_pct"]
        print(f"{r['phase']:<8} {r['predictions']:>5}   {c[0]:>3} /{c[1]:>3} /{c[2]:>3}        "
              f"{r['cls2_confidence'] if r['cls2_confidence'] is not None else '-':>5}  {r['faces_pct']:>7}  "
              f"{r['speech_pct']:>8}  {', '.join(str(t) for t in r['turns']) or '-'}")
    print()
    for i, t in enumerate(turn_files, 1):
        fr = ", ".join(f"{f['bytes']} B {'ok ' + 'x'.join(map(str, f['decodes'])) if isinstance(f['decodes'], tuple) else f['decodes']}"
                       for f in t["frames"]) or "no frames"
        print(f"turn {i}: {t.get('seconds', t['duration_sec'])} s, peak {t.get('peak_dbfs')} dBFS, "
              f"RMS {t.get('rms_dbfs')} dBFS; frames: {fr}")
    print(f"RTT p50 {rep['rtt_ms']['p50'] if rep['rtt_ms'] else '?'} ms, max {rep['rtt_ms']['max'] if rep['rtt_ms'] else '?'} ms; "
          f"CPU {rep['cpu_pct']}% of a core; peak RSS {rep['peak_rss_kb']} KB")
    print()
    for c in rep["checks"]:
        print(f"{'skip' if c['ok'] is None else 'ok  ' if c['ok'] else 'FAIL'}  {c['check']}: {c['detail']}")
    if exit_code:
        with open(os.path.join(out, "demo.stderr")) as f:
            tail = [line.rstrip() for line in f if not line.startswith("ALSA lib")][-5:]
        if tail:
            print("\nthe demo's stderr ends:\n  " + "\n  ".join(tail))
    print(f"\nreport: {os.path.join(out, 'report.json')}")
    return 0 if all(c["ok"] is not False for c in rep["checks"]) else 1


if __name__ == "__main__":
    sys.exit(main())
