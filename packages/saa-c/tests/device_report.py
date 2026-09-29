#!/usr/bin/env python3
"""device_report.py - builds saa-c on this machine, runs its tests, and measures
its footprint against the mock server. It prints a Markdown report and writes
it, with the raw numbers as JSON, to build/device-report/.

usage: python3 tests/device_report.py [--quick] [--skip-tests] [--jobs N]
                                      [--live-wav FILE] [--out DIR]

Run it with a Python that has websockets 13 or newer: the mock server and the
tests use the same interpreter. It also needs cmake, a C compiler, pkg-config,
libwebsockets, and the openssl command.

What it measures:
  - size: text + data of libsaaclient.a built at -Os (MinSizeRel);
  - CPU: user + system time while streaming in real time from memory
    (feed_bench, so no file reading is counted), as a percentage of one core:
    audio only, 48 kHz stereo in 10 ms blocks so the resampler runs, then with
    30 KB JPEGs at 4 fps, the size of 640x480 frames. The same loop on an open
    connection without feeding is measured too, and subtracted to give the
    library's own share;
  - memory: resident memory while streaming audio, and while the demo holds a
    decoded 25 s turn received over TLS. On Linux it reads /proc/PID/smaps, to
    give the figure without the pages of the TLS libraries;
  - latency: from a feed call to the mock receiving the frame (feed_bench), for
    the first sample of each 100 ms frame and for the call that completes it.
With --live-wav, and an API key in SAA_API_KEY, it also runs live_smoke.py, on the
package's sample recording or on the WAV given.
"""
import argparse
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import conformance  # noqa: E402  (free_port, make_ca, make_jpegs, start_mock, ...)

TLS_LIB = re.compile(r"^lib(ssl|crypto|mbedtls|mbedcrypto|mbedx509|gnutls)[.-]")
TARGETS = {                       # from the footprint targets; see the report's notes
    "size_kb": 150, "rss_kb": 8 * 1024, "cpu_audio_pct": 2.0, "cpu_video_pct": 5.0, "latency_ms": 110,
}


def run(cmd, timeout=None, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


def first_line(cmd):
    try:
        r = run(cmd, timeout=15)
        text = (r.stdout or r.stderr).strip()
        return text.splitlines()[0] if r.returncode == 0 and text else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def pct(values, p):
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1))))] if s else None


# ── the machine ───────────────────────────────────────────────────────

def machine():
    m = {"arch": platform.machine(), "cores": os.cpu_count(), "python": platform.python_version()}
    if sys.platform.startswith("linux"):
        osr = dict(line.split("=", 1) for line in read("/etc/os-release").splitlines() if "=" in line)
        m["os"] = osr.get("PRETTY_NAME", "Linux").strip('"')
        m["kernel"] = platform.release()
        m["model"] = (read("/proc/device-tree/model").strip("\x00\n ")
                      or read("/sys/class/dmi/id/product_name").strip())
        cpu = ""
        if shutil.which("lscpu"):
            for line in run(["lscpu"]).stdout.splitlines():
                if line.startswith("Model name:"):
                    cpu = line.split(":", 1)[1].strip()
                    break
        for line in read("/proc/cpuinfo").splitlines():
            if not cpu and line.lower().startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
        m["cpu"] = cpu
        khz = read("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq").strip()
        m["cpu_max_mhz"] = int(khz) // 1000 if khz.isdigit() else None
    elif sys.platform == "darwin":
        m["os"] = "macOS " + first_line(["sw_vers", "-productVersion"])
        m["model"] = first_line(["sysctl", "-n", "hw.model"])
        m["cpu"] = first_line(["sysctl", "-n", "machdep.cpu.brand_string"])
    m["lws"] = first_line(["pkg-config", "--modversion", "libwebsockets"])
    m["compiler"] = first_line(["cc", "--version"])
    m["openssl"] = first_line(["openssl", "version"])
    try:
        import websockets
        m["websockets"] = websockets.__version__
    except ImportError:
        m["websockets"] = None
    hdr = read(os.path.join(PKG, "include", "saa", "saa_client.h"))
    ver = re.search(r'SAA_CLIENT_VERSION_STRING\s+"([^"]+)"', hdr)
    m["saa_c"] = ver.group(1) if ver else "?"
    rev = first_line(["git", "-C", PKG, "rev-parse", "--short", "HEAD"])
    dirty = run(["git", "-C", PKG, "status", "--porcelain", "--", "."]).stdout.strip() if rev else ""
    m["git"] = rev + ("+changes" if dirty else "")
    return m


# ── building and testing ──────────────────────────────────────────────

def cmake_build(build_dir, build_type, options, target=None):
    cfg = ["cmake", "-S", PKG, "-B", build_dir, f"-DCMAKE_BUILD_TYPE={build_type}",
           f"-DPython3_EXECUTABLE={sys.executable}"] + options
    r = run(cfg)
    if r.returncode:
        return False, (r.stdout + r.stderr)[-3000:]
    b = ["cmake", "--build", build_dir, "--parallel", str(os.cpu_count() or 2)]
    if target:
        b += ["--target", target]
    r = run(b)
    return r.returncode == 0, (r.stdout + r.stderr)[-3000:]


def warnings_in(log):
    return [line.strip() for line in log.splitlines() if "warning:" in line or "error:" in line][:20]


def run_ctest(build_dir):
    r = run(["ctest", "--test-dir", build_dir, "-E", "^conformance$", "--output-on-failure"], timeout=1800)
    m = re.search(r"(\d+)% tests passed, (\d+) tests? failed out of (\d+)", r.stdout)
    failed = re.findall(r"^\s*\d+ - (\S+) \(", r.stdout, re.M)
    total = int(m.group(3)) if m else None
    return {"passed": total - int(m.group(2)) if m else None, "total": total, "failed": failed,
            "output": "" if r.returncode == 0 else r.stdout[-4000:]}


def run_conformance(demo, jobs):
    r = run([sys.executable, os.path.join(HERE, "conformance.py"), demo, "--jobs", str(jobs), "--require-tls"],
            timeout=1800)
    rows = re.findall(r"^(\S+)\s+(ok|FAIL|skip)\b", r.stdout, re.M)
    return {"passed": sum(1 for _, s in rows if s == "ok"), "total": len(rows),
            "failed": [n for n, s in rows if s == "FAIL"], "exit": r.returncode,
            "output": "" if r.returncode == 0 else (r.stdout + r.stderr)[-4000:]}


def lib_size(archive):
    r = run(["size", archive])
    text = data = 0
    for line in r.stdout.splitlines():         # GNU: text data bss ...; macOS: __TEXT __DATA ...
        cols = line.split()
        if len(cols) >= 2 and cols[0].isdigit() and cols[1].isdigit():
            text, data = text + int(cols[0]), data + int(cols[1])
    return {"text": text, "data": data, "kb": round((text + data) / 1024.0, 1)} if text else None


# ── measuring ─────────────────────────────────────────────────────────

def parse_smaps(text):
    """Resident KB from /proc/PID/smaps: all of it, anonymous, and the TLS libraries' pages."""
    total = anon = tls = 0
    path = ""
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        if not fields[0].endswith(":"):                   # a mapping: addr perms offset dev inode [path]
            path = os.path.basename(fields[5]) if len(fields) > 5 else ""
        elif fields[0] == "Rss:":
            total += int(fields[1])
            tls += int(fields[1]) if TLS_LIB.search(path) else 0
        elif fields[0] == "Anonymous:":
            anon += int(fields[1])
    return {"rss_kb": total, "anon_kb": anon, "tls_lib_kb": tls}


def smaps(pid):
    try:
        with open(f"/proc/{pid}/smaps") as f:
            return parse_smaps(f.read())
    except (OSError, ValueError, IndexError):
        return None


def run_sampled(cmd, tmp, name, timeout, env=None):
    """Runs cmd to completion, reading its /proc/PID/smaps every 2 s on Linux. Returns its exit
    status, stdout, stderr, and the samples."""
    out, err = (open(os.path.join(tmp, f"{name}.{x}"), "w+") for x in ("out", "err"))
    t0 = time.monotonic()
    p = subprocess.Popen(cmd, env=env, stdout=out, stderr=err)
    samples = []
    while True:
        try:
            p.wait(timeout=2.0)
            break
        except subprocess.TimeoutExpired:
            if time.monotonic() - t0 > timeout:
                p.kill()
            elif sys.platform.startswith("linux"):
                s = smaps(p.pid)
                if s:
                    samples.append(s)
    out.seek(0)
    err.seek(0)
    r = {"exit": p.returncode, "stdout": out.read(), "stderr": err.read()[-600:], "smaps": samples}
    out.close()
    err.close()
    return r


def bench(feeder, url, seconds, args, tmp, name):
    """feed_bench's numbers, plus the memory samples taken while it ran."""
    r = run_sampled([feeder, url, str(seconds)] + args, tmp, name, seconds + 60)
    try:
        d = json.loads(r["stdout"].strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"exit": r["exit"], "stderr": r["stderr"], "smaps": r["smaps"]}
    d.update({"exit": r["exit"], "smaps": r["smaps"],
              "cpu_pct": round(100.0 * d["cpu_s"] / d["wall_s"], 2) if d.get("wall_s") else None})
    if r["exit"]:
        d["stderr"] = r["stderr"]
    return d


def run_demo(demo, url, key, args, tmp, name, timeout):
    """Runs the demo to completion. Returns its exit status, events, and memory samples."""
    out = os.path.join(tmp, f"{name}.jsonl")
    r = run_sampled([demo, "--url", url, "--events", out] + args, tmp, name, timeout,
                    env=dict(os.environ, SAA_API_KEY=key))
    events = []
    if os.path.exists(out):
        with open(out) as f:
            events = [json.loads(line) for line in f if line.strip()]
    return {"exit": r["exit"], "events": events, "smaps": r["smaps"], "stderr": r["stderr"]}


def of(events, name):
    return [e for e in events if e["event"] == name]


def memory_of(r):
    """Memory figures for one run, in KB (None where unknown)."""
    events = r.get("events", [])
    summary = (of(events, "summary") or [{}])[-1]
    turn = (of(events, "turn_ready") or [{}])[0]
    mem = {"peak_rss_kb": summary.get("peak_rss_kb"),
           "turn_rss_kb": turn.get("rss_kb"), "turn_anon_kb": turn.get("rss_anon_kb")}
    if r["smaps"]:
        mem["max_rss_kb"] = max(s["rss_kb"] for s in r["smaps"])
        mem["max_anon_kb"] = max(s["anon_kb"] for s in r["smaps"])
        mem["tls_lib_kb"] = max(s["tls_lib_kb"] for s in r["smaps"])
        mem["max_rss_without_tls_libs_kb"] = max(s["rss_kb"] - s["tls_lib_kb"] for s in r["smaps"])
    return mem


def latency(feed_times, arrivals):
    """Per 100 ms frame: from the first sample's feed call, and from the call that completed the
    frame, to the mock receiving it. The first two frames are skipped."""
    first, added = [], []
    for k, t in enumerate(arrivals):
        if k < 2 or 10 * k + 10 >= len(feed_times):
            continue
        first.append(1000.0 * (t - feed_times[10 * k]))
        added.append(1000.0 * (t - feed_times[10 * k + 10]))

    def summary(v):
        return {"frames": len(v), "p50_ms": round(pct(v, 50), 2), "p95_ms": round(pct(v, 95), 2),
                "p99_ms": round(pct(v, 99), 2), "max_ms": round(max(v), 2),
                "mean_ms": round(statistics.fmean(v), 2)} if v else None
    return {"first_sample": summary(first), "frame_complete": summary(added),
            "frames_received": len(arrivals), "blocks_fed": len(feed_times)}


# ── the report ────────────────────────────────────────────────────────

def kb(v):
    return "?" if v is None or v < 0 else (f"{v / 1024.0:.1f} MB" if v >= 1024 else f"{v} KB")


def verdict(value, target):
    return "?" if value is None else ("ok" if value <= target else "**over**")


def markdown(rep):
    m, t, f = rep["machine"], rep.get("tests", {}), rep.get("footprint", {})
    cpu_name = m.get("cpu") or "?"
    if m.get("cpu_max_mhz"):
        cpu_name += f" at {m['cpu_max_mhz'] / 1000.0:.2g} GHz"
    lines = [
        "# saa-c device report", "",
        "| | |", "|---|---|",
        f"| Machine | {m.get('model') or '?'} ({m['arch']}, {m['cores']} cores, {cpu_name}) |",
        f"| OS | {m.get('os', '?')}{', kernel ' + m['kernel'] if m.get('kernel') else ''} |",
        f"| libwebsockets | {m.get('lws') or '?'} |",
        f"| Compiler | {m.get('compiler') or '?'} |",
        f"| TLS tooling | {m.get('openssl') or 'no openssl command'} |",
        f"| Python, websockets | {m['python']}, {m.get('websockets') or 'missing'} |",
        f"| saa-c | {m['saa_c']} ({m.get('git') or 'no git'}) |",
        f"| Date | {rep['date']} |", "",
    ]
    if t:
        lines += ["## Tests", ""]
        b = t.get("build", {})
        lines.append(f"- Build with `-Werror`: {'ok' if b.get('werror_ok') else 'FAILED, so the rest ran without it'}")
        for w in b.get("warnings", []):
            lines.append(f"  - `{w}`")
        for name in ("ctest", "conformance"):
            r = t.get(name)
            if r:
                label = "CTest (all but conformance)" if name == "ctest" else "Conformance"
                fails = f"; failed: {', '.join(r['failed'])}" if r.get("failed") else ""
                lines.append(f"- {label}: {r['passed']}/{r['total']} passed{fails}")
        lines.append("")
    if rep.get("live"):
        lv = rep["live"]
        lines += ["## Live smoke", "",
                  f"- {'ok' if lv.get('ok') else 'FAILED'}: exit {lv.get('exit')}, {lv.get('turns')} `turn_ready` "
                  f"({', '.join(f'{s:.2f} s' for s in lv.get('turn_seconds', []))}), warmup {lv.get('warmup_ms')} ms, "
                  f"turn {lv.get('turn_after_wav_end_ms')} ms after the WAV ended, RTT {lv.get('rtt_ms')} ms, "
                  f"RSS at the turn {kb(lv.get('rss_kb_at_turn'))} (anonymous {kb(lv.get('rss_anon_kb_at_turn'))})", ""]
        for e in lv.get("errors", []):
            lines.append(f"  - error: {e}")
    if f:
        s, lat = f.get("size"), f.get("latency", {})
        linux = sys.platform.startswith("linux")
        ma, mt = f.get("mem_audio", {}), f.get("mem_turn", {})
        rss_audio = ma.get("max_rss_without_tls_libs_kb")
        rss_turn = None
        if mt.get("turn_rss_kb") is not None and mt["turn_rss_kb"] >= 0:
            rss_turn = mt["turn_rss_kb"] - (mt.get("tls_lib_kb") or 0)
        fs, fc = lat.get("first_sample") or {}, lat.get("frame_complete") or {}
        idle_pct = (f.get("cpu_idle") or {}).get("cpu_pct")

        def share(r):                              # the library's own part: minus the loop alone
            if r.get("cpu_pct") is None or idle_pct is None:
                return r.get("cpu_pct")
            return round(max(0.0, r["cpu_pct"] - idle_pct), 2)
        lines += [
            "## Footprint", "",
            "| Metric | Target | Measured | |", "|---|---|---|---|",
            f"| `libsaaclient.a` text+data at `-Os` | ≤ 150 KB (arm64) | "
            f"{s['kb'] if s else '?'} KB | {verdict(s['kb'] if s else None, TARGETS['size_kb'])} |",
            f"| RSS, audio-only streaming, without TLS library pages | ≤ 8 MB | "
            f"{kb(rss_audio)} (anonymous {kb(ma.get('max_anon_kb'))}, TLS libraries {kb(ma.get('tls_lib_kb'))}) | "
            f"{verdict(rss_audio, TARGETS['rss_kb']) if linux else 'n/a'} |",
            f"| RSS holding a decoded 25 s turn{' over TLS' if f.get('turn_tls') else ''}, "
            f"without TLS library pages | ≤ 8 MB | "
            f"{kb(rss_turn)} (anonymous {kb(mt.get('turn_anon_kb'))}, peak {kb(mt.get('peak_rss_kb'))}) | "
            f"{verdict(rss_turn, TARGETS['rss_kb']) if linux else 'n/a'} |",
            f"| CPU, audio only (48 kHz stereo in 10 ms blocks) | ≤ 2 % of one core (Cortex-A53) | "
            f"{share(f['cpu_audio'])} % ({f['cpu_audio']['cpu_pct']} % with the loop) | "
            f"{verdict(share(f['cpu_audio']), TARGETS['cpu_audio_pct'])} |",
            f"| CPU, audio + 30 KB JPEGs at 4 fps | ≤ 5 % of one core (Cortex-A53) | "
            f"{share(f['cpu_video'])} % ({f['cpu_video']['cpu_pct']} % with the loop) | "
            f"{verdict(share(f['cpu_video']), TARGETS['cpu_video_pct'])} |",
            f"| Latency, feed call to frame received (frame's first sample) | ≤ 110 ms | "
            f"p50 {fs.get('p50_ms')}, p99 {fs.get('p99_ms')}, max {fs.get('max_ms')} ms | "
            f"{verdict(fs.get('p99_ms'), TARGETS['latency_ms'])} |",
            f"| Latency added by the client (frame complete to received) | | "
            f"p50 {fc.get('p50_ms')}, p99 {fc.get('p99_ms')}, max {fc.get('max_ms')} ms | |",
            "",
            f"CPU is over {f['cpu_audio']['wall_s']:.0f} s of real-time streaming from memory: the library's share, "
            f"then the whole process, whose feeding loop alone takes {idle_pct} % on an open connection. The latency "
            "runs over "
            f"{lat.get('frames_received')} frames. The CPU targets are for a Cortex-A53 and the size target for arm64; "
            "elsewhere they are for comparison. The 25 s turn is held by the demo"
            f"{', over TLS' if f.get('turn_tls') else ''}.",
            "",
        ]
        if not linux:
            lines += ["Not Linux: without /proc/PID/smaps the memory rows cannot separate the TLS libraries, "
                      "and the demo's peak counts every system library it touches, so they carry no verdict.", ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--quick", action="store_true", help="shorter runs (15 s instead of 60 s)")
    ap.add_argument("--skip-tests", action="store_true", help="measure only")
    ap.add_argument("--jobs", type=int, default=max(4, 2 * (os.cpu_count() or 2)),
                    help="conformance demos at once (default: twice the cores, at least 4)")
    ap.add_argument("--live-wav", nargs="?", const=os.path.join(PKG, "examples", "demo", "sample_drive_thru.wav"),
                    help="also stream a speech WAV, by default the sample, to the real service (needs SAA_API_KEY)")
    ap.add_argument("--out", default=os.path.join(PKG, "build", "device-report"), help="where to build and write")
    args = ap.parse_args()
    if args.live_wav and not os.environ.get("SAA_API_KEY"):
        print("--live-wav needs an API key in SAA_API_KEY", file=sys.stderr)
        return 2
    cpu_s, lat_s = (15, 10) if args.quick else (60, 30)
    os.makedirs(args.out, exist_ok=True)
    rep = {"date": time.strftime("%Y-%m-%d %H:%M %Z"), "machine": machine()}
    print(f"machine: {rep['machine'].get('model') or rep['machine']['arch']}, lws {rep['machine'].get('lws')}",
          flush=True)

    # 1. the normal build, with -Werror when it passes
    default = os.path.join(args.out, "default")
    print("building (RelWithDebInfo, -Werror) ...", flush=True)
    ok, log = cmake_build(default, "RelWithDebInfo", ["-DSAA_WERROR=ON", "-DSAA_REQUIRE_MOCK_TESTS=ON"])
    build = {"werror_ok": ok, "warnings": [] if ok else warnings_in(log)}
    if not ok:
        print("  failed with -Werror; retrying without it", flush=True)
        ok, log = cmake_build(default, "RelWithDebInfo", ["-DSAA_WERROR=OFF", "-DSAA_REQUIRE_MOCK_TESTS=ON"])
        if not ok:
            print(log)
            return 1
    demo = os.path.join(default, "saa_client_demo")
    feeder = os.path.join(default, "tests", "feed_bench")

    # 2. the tests
    rep["tests"] = {"build": build}
    if not args.skip_tests:
        print("running CTest ...", flush=True)
        rep["tests"]["ctest"] = run_ctest(default)
        print(f"running conformance ({args.jobs} at once) ...", flush=True)
        rep["tests"]["conformance"] = run_conformance(demo, args.jobs)

    # 3. size at -Os
    print("building the library at -Os ...", flush=True)
    minsize = os.path.join(args.out, "minsize")
    ok, log = cmake_build(minsize, "MinSizeRel", ["-DSAA_BUILD_DEMO=OFF", "-DSAA_BUILD_TESTS=OFF"], target="saaclient")
    foot = {"size": lib_size(os.path.join(minsize, "libsaaclient.a")) if ok else None}

    # 4. CPU, memory, and latency against a local mock
    tmp = tempfile.mkdtemp(prefix="saa-device-report-")
    mock = None
    try:
        run([sys.executable, os.path.join(HERE, "gen_audio.py"), os.path.join(tmp, "audio")], check=True)
        wav = os.path.join(tmp, "audio", "speech_48k_stereo.wav")
        http, ws = conformance.free_port(), conformance.free_port()
        mock_args = ["--http-port", str(http), "--ws-port", str(ws)]
        certs = conformance.make_ca(tmp)
        if certs:
            https, wss = conformance.free_port(), conformance.free_port()
            mock_args += ["--tls-cert", certs[1], "--tls-key", certs[2], "--https-port", str(https),
                          "--wss-port", str(wss)]
        summary = os.path.join(tmp, "summary.jsonl")
        mock = conformance.start_mock(tmp, "mock", mock_args, summary)
        if not (conformance.wait_listening(http) and conformance.wait_listening(ws)):
            print("mock server did not start")
            return 1
        broker, direct = f"http://127.0.0.1:{http}", f"ws://127.0.0.1:{ws}/ws"

        print(f"measuring the feeding loop alone ({cpu_s // 2} s) ...", flush=True)
        idle = bench(feeder, direct, cpu_s // 2, ["--idle"], tmp, "cpu-idle")
        print(f"measuring CPU and memory, audio only ({cpu_s} s) ...", flush=True)
        a = bench(feeder, direct, cpu_s, ["--stereo48"], tmp, "cpu-audio")
        print(f"measuring CPU, audio and video ({cpu_s} s) ...", flush=True)
        v = bench(feeder, direct, cpu_s, ["--stereo48", "--jpeg", "30000"], tmp, "cpu-video")
        print(f"measuring feed-to-wire latency ({lat_s} s) ...", flush=True)
        lt = bench(feeder, direct, lat_s, ["--times"], tmp, "latency")
        arrivals = []
        for _ in range(50):                             # the mock writes when the session closes
            rows = [s for s in conformance.read_summaries([summary]) if s["scenario"] == "latency"]
            if rows:
                arrivals = rows[-1].get("audio_arrivals", [])
                break
            time.sleep(0.1)
        print("measuring memory across a 25 s turn ...", flush=True)
        turn_url, turn_args = broker, []
        if certs:
            turn_url, turn_args = f"https://localhost:{https}", ["--ca", certs[0]]
        tr = run_demo(demo, turn_url, "longturn", ["--wav", wav, "--tail", "9999", "--duration", "12", "--stats"]
                      + turn_args, tmp, "longturn", 60)

        def cpu(r):
            keep = {k: r.get(k) for k in ("exit", "cpu_s", "wall_s", "cpu_pct", "blocks", "errors")}
            return keep | ({"stderr": r.get("stderr")} if r.get("exit") else {})
        foot.update({
            "cpu_idle": cpu(idle), "cpu_audio": cpu(a), "cpu_video": cpu(v),
            "mem_audio": memory_of(a), "mem_turn": memory_of(tr), "turn_tls": bool(certs),
            "turn_samples": ((of(tr["events"], "turn_ready") or [{}])[0]).get("samples"),
            "turn_exit": tr["exit"],
            "latency": latency(lt.get("feed_times", []), arrivals) if lt.get("feed_times") and arrivals
                       else {"error": lt.get("stderr", "no data")},
        })
    finally:
        if mock:
            mock.terminate()
            mock.wait(timeout=5)
        shutil.rmtree(tmp, ignore_errors=True)
    rep["footprint"] = foot

    # 5. the live smoke
    if args.live_wav:
        print("live smoke against the real service ...", flush=True)
        r = run([sys.executable, os.path.join(HERE, "live_smoke.py"), demo, args.live_wav, "--json"], timeout=180)
        try:
            rep["live"] = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            rep["live"] = {"ok": False, "errors": [(r.stdout + r.stderr).strip()[-600:]]}

    text = markdown(rep)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    with open(os.path.join(args.out, f"report-{stamp}.md"), "w") as f:
        f.write(text + "\n")
    with open(os.path.join(args.out, f"report-{stamp}.json"), "w") as f:
        json.dump(rep, f, indent=2)
    print()
    print(text)
    print(f"written to {os.path.join(args.out, f'report-{stamp}.md')} (and .json)")
    t = rep.get("tests", {})
    bad = any(t.get(n, {}).get("failed") for n in ("ctest", "conformance")) or \
        (args.live_wav and not rep.get("live", {}).get("ok"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
