#!/usr/bin/env python3
"""run_voice_agent.py - the voice agent against both mocks, with no hardware and no keys:
mock_server.py's voiceagent scenario stands in for SAA, and mock_realtime.py for OpenAI. The
agent reads a WAV, writes its speaker to another, and logs its events. This checks what reached
each mock, what the speaker played, and when:

  turn 1  played out, then the tail, then responding_stop
  turn 2  interrupted a second into playback: faded, truncated at what was played
  turn 3  its reply held back by the Realtime mock; turn 4 arrives meanwhile, so reply 3 is
          cancelled before any of it plays
  turn 4  played out

usage: run_voice_agent.py SAA_VOICE_AGENT_BINARY
"""
import json
import math
import os
import shlex
import socket
import struct
import subprocess
import sys
import tempfile
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
emulator = shlex.split(os.environ.get("SAA_TEST_EMULATOR", ""))

TONE_S = 2.5                 # the Realtime mock's reply length
GAIN_DB = 6.0                # the agent's default
TAIL_MS = 300
FADE_MS = 300                # the SAA mock's interrupt
AMPLITUDE = 0.25 * 32767     # the Realtime mock's tone, before the gain
DURATION_S = 23


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


def write_input(path):
    """Half a second of a quiet 300 Hz tone at 16 kHz; the agent streams silence after it."""
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<8000h", *(int(2000 * math.sin(2 * math.pi * 300 * i / 16000))
                                              for i in range(8000))))


def read_wav(path):
    with wave.open(path, "rb") as w:
        rate, n = w.getframerate(), w.getnframes()
        assert w.getnchannels() == 1 and w.getsampwidth() == 2
        return rate, struct.unpack(f"<{n}h", w.readframes(n))


def segments(rate, x, block_s=0.02, floor=500):
    """Runs of 20 ms blocks with a peak above floor: (start_s, end_s, peak, freq_hz)."""
    b = int(rate * block_s)
    out, cur = [], None
    for i in range(0, len(x) - b + 1, b):
        blk = x[i:i + b]
        loud = max(abs(v) for v in blk) > floor
        if loud and cur is None:
            cur = i
        elif not loud and cur is not None:
            out.append((cur, i))
            cur = None
    if cur is not None:
        out.append((cur, len(x)))
    res = []
    for a, e in out:
        seg = x[a:e]
        zc = sum(1 for k in range(1, len(seg)) if (seg[k - 1] < 0) != (seg[k] < 0))
        res.append((a / rate, e / rate, max(abs(v) for v in seg), zc / 2 / (len(seg) / rate)))
    return res


def main():
    binary = sys.argv[1]
    http, ws, rt = free_port(), free_port(), free_port()
    failures = []

    def check(ok, what):
        print(("ok    " if ok else "FAIL  ") + what)
        if not ok:
            failures.append(what)

    with tempfile.TemporaryDirectory() as tmp:
        saa_sum, rt_sum = os.path.join(tmp, "saa.jsonl"), os.path.join(tmp, "rt.jsonl")
        wav_in, wav_out, events = (os.path.join(tmp, n) for n in ("in.wav", "speaker.wav", "events.jsonl"))
        write_input(wav_in)
        mocks = [
            subprocess.Popen([sys.executable, os.path.join(HERE, "mock_server.py"), "--http-port", str(http),
                              "--ws-port", str(ws), "--summary", saa_sum],
                             stdout=open(os.path.join(tmp, "saa.log"), "w"), stderr=subprocess.STDOUT),
            subprocess.Popen([sys.executable, os.path.join(HERE, "mock_realtime.py"), "--port", str(rt),
                              "--summary", rt_sum, "--hold", "3:2", "--tone-s", str(TONE_S)],
                             stdout=open(os.path.join(tmp, "rt.log"), "w"), stderr=subprocess.STDOUT),
        ]
        try:
            if not (wait_listening(ws) and wait_listening(http) and wait_listening(rt)):
                print("the mocks did not start")
                return 1
            env = dict(os.environ, SAA_API_KEY="voiceagent", OPENAI_API_KEY="test-key")
            cmd = emulator + [binary, "--url", f"http://127.0.0.1:{http}",
                              "--openai-url", f"ws://127.0.0.1:{rt}/v1/realtime", "--wav", wav_in,
                              "--speaker", "file:" + wav_out, "--events", events, "--tail-ms", str(TAIL_MS),
                              "--duration", str(DURATION_S)]
            r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=DURATION_S + 40)
            print(r.stdout.rstrip())
            if r.stderr.strip():
                print(r.stderr.rstrip())
        finally:
            for m in mocks:
                m.terminate()
            for m in mocks:
                m.wait(timeout=5)
        check(r.returncode == 0, f"the agent exited 0 (got {r.returncode})")
        if not os.path.exists(events) or not os.path.exists(saa_sum):
            print("no events or no SAA session to check")
            return 1

        ev = [json.loads(line) for line in open(events)]
        start = next(e for e in ev if e["event"] == "agent_start")
        mono = lambda e: start["mono_t0"] + e["ts_ms"] / 1000.0   # noqa: E731
        by = lambda name: [e for e in ev if e["event"] == name]   # noqa: E731
        saa = [json.loads(line) for line in open(saa_sum)]
        saa = [s for s in saa if s["scenario"] == "voiceagent"]
        rts = [json.loads(line) for line in open(rt_sum)] if os.path.exists(rt_sum) else []
        check(len(saa) == 1 and len(rts) == 1, f"one session at each mock (SAA {len(saa)}, Realtime {len(rts)})")
        if len(saa) != 1 or len(rts) != 1:
            return 1
        saa, rts = saa[0], rts[0]

        # what reached the Realtime mock
        check(not rts["problems"], f"the Realtime mock found no problems {rts['problems']}")
        check(rts["user_agent"].startswith("saa-c-voice-agent/"), f"User-Agent {rts['user_agent']!r}")
        turns = saa["va_turns"]
        items = [i for i in rts["items"] if i["role"] == "user"]
        check(len(turns) == 4 and len(items) == 4, f"4 turns sent, 4 user items received ({len(turns)}, {len(items)})")
        for k, (t, i) in enumerate(zip(turns, items), 1):
            check(i["input_audio_samples"] == t["samples"] * 3 // 2 and i["content"] == ["input_audio"]
                  and i["via"] == "input_audio_buffer.commit",
                  f"turn {k}: {t['samples']} samples at 16 kHz arrived as {i['input_audio_samples']} at 24 kHz, "
                  f"through {i['via']}, parts {i['content']}")
        resp = rts["responses"]
        check([x["status"] for x in resp] == ["completed", "completed", "cancelled", "completed"],
              f"replies completed, completed, cancelled, completed: {[x['status'] for x in resp]}")
        check(all((x.get("metadata") or {}).get("turn") for x in resp), "each response.create tagged its turn")

        # turn 3: cancelled while held, before turn 4's item
        cancels = rts["cancels"]
        check(len(cancels) == 1 and cancels[0]["response"] == resp[2]["id"] and cancels[0]["audio_samples_sent"] == 0,
              f"one response.cancel, for reply 3, before any of its audio: {cancels}")
        if cancels and len(items) == 4:
            check(cancels[0]["t"] < items[3]["t"], "the cancel went before turn 4's item")

        # turn 2: the truncate
        stopping = by("playback_stopping")
        truncs = rts["truncates"]
        played_ms = stopping[0]["played_ms"] if stopping else None
        check(len(truncs) == 1 and truncs[0]["ok"] and truncs[0]["item_id"] == resp[1].get("item_id")
              and truncs[0]["audio_end_ms"] == played_ms,
              f"one truncate, of reply 2 at the {played_ms} ms played: {truncs}")
        if truncs:
            check(1000 <= truncs[0]["audio_end_ms"] <= 2000, f"reply 2 cut between 1 and 2 s ({truncs[0]['audio_end_ms']} ms)")

        # what reached the SAA mock, and when
        acts = [(a["action"], t) for a, t in zip(saa["actions"], saa["action_times"])
                if a["action"] in ("responding_start", "responding_stop", "mute", "unmute")]
        names = [a for a, _ in acts]
        check(names == ["responding_start", "responding_stop"] * 3, f"responding started and stopped 3 times, no mute: {names}")
        starts, ends = by("playback_start"), [e for e in by("playback_end")]
        rs = [t for a, t in acts if a == "responding_start"]
        re_ = [t for a, t in acts if a == "responding_stop"]
        if len(starts) == 3 and len(rs) == 3:
            gaps = [round(r - mono(p), 3) for r, p in zip(rs, starts)]
            check(all(-0.05 <= g <= 0.15 for g in gaps), f"responding_start reached SAA as playback began: {gaps} s")
        full = [e for e in ends if not e["interrupted"]]
        if len(full) == 2 and len(re_) == 3:
            tails = [round(re_[i] - mono(e), 3) for i, e in zip((0, 2), full)]
            check(all(TAIL_MS / 1000 - 0.05 <= g <= TAIL_MS / 1000 + 0.2 for g in tails),
                  f"responding_stop came the tail after the drain: {tails} s")
        if saa["va_interrupts"] and len(re_) >= 2:
            g = round(re_[1] - saa["va_interrupts"][0], 3)
            check(0 <= g <= 0.2, f"responding_stop came at once after the interrupt: {g} s")
        intr, cut = by("interrupt"), [e for e in ends if e["interrupted"]]
        if intr and cut:
            g = round((cut[0]["ts_ms"] - intr[0]["ts_ms"]) / 1000.0, 3)
            check(g <= (FADE_MS + 100) / 1000.0, f"playback stopped {g} s after the interrupt, within fade_ms + 100 ms")

        # what the speaker played
        rate, x = read_wav(wav_out)
        segs = segments(rate, x)
        want_peak = AMPLITUDE * 10 ** (GAIN_DB / 20)
        pitches = [round((f - 300) / 100) for _, _, _, f in segs]
        check(pitches == [1, 2, 4], f"the speaker played replies 1, 2, and 4, and nothing of 3: {pitches} "
              f"({[round(f) for *_, f in segs]} Hz)")
        if len(segs) == 3:
            peaks = [p for _, _, p, _ in segs]
            check(all(abs(p - want_peak) / want_peak < 0.03 for p in peaks),
                  f"at +{GAIN_DB:g} dB: peaks {peaks}, expected {want_peak:.0f}")
            lens = [round(e - a, 2) for a, e, _, _ in segs]
            check(abs(lens[0] - TONE_S) < 0.05 and abs(lens[2] - TONE_S) < 0.05,
                  f"replies 1 and 4 played whole: {lens[0]} and {lens[2]} s of {TONE_S}")
            if played_ms is not None:
                check(abs(lens[1] - played_ms / 1000.0) < 0.06,
                      f"reply 2 played {lens[1]} s, as the truncate says ({played_ms} ms)")
            a, e = int(segs[1][0] * rate), int(segs[1][1] * rate)
            tail = max(abs(v) for v in x[e - rate // 50:e]) if e - a > rate // 25 else 0
            check(tail < want_peak * 0.2, f"reply 2 faded out: its last 20 ms peak at {tail}")

        # the agent's own account
        t_ev = by("turn")
        outcomes = [t["outcome"] for t in t_ev]
        check(outcomes == ["played", "interrupted", "cancelled", "played"], f"turn outcomes {outcomes}")
        check([t["heard"] for t in t_ev] == [f"turn {k}" for k in range(1, 5)],
              f"each line has what the model heard: {[t['heard'] for t in t_ev]}")
        check([t["said"] for t in t_ev if t["outcome"] != "cancelled"] == ["reply 1", "reply 2", "reply 4"],
              f"and what the replies said: {[t['said'] for t in t_ev]}")
        summary = by("summary")
        check(bool(summary) and summary[0]["played"] == 2 and summary[0]["interrupted"] == 1
              and summary[0]["cancelled"] == 1 and summary[0]["lost"] == 0, f"summary {summary}")
    print(f"{len(failures)} check(s) failed" if failures else "all checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
