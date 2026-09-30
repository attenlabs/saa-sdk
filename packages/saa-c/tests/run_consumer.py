#!/usr/bin/env python3
"""run_consumer.py - installs saa-c into a temporary prefix and builds
examples/minimal against it the two ways an integrator would: with pkg-config
and with CMake's find_package. With --cxx, examples/cpp is built both ways
too. With --run, every build then streams three seconds of audio to the mock
server.

usage: run_consumer.py BUILD_DIR C_COMPILER [--cxx CXX_COMPILER] [--run]
"""
import glob
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

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode:
        print("$", " ".join(shlex.quote(c) for c in cmd))
        print((r.stdout + r.stderr).strip()[-3000:])
    return r


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
    build_dir, cc = sys.argv[1], sys.argv[2]
    do_run = "--run" in sys.argv[3:]
    cxx = sys.argv[sys.argv.index("--cxx") + 1] if "--cxx" in sys.argv[3:] else None
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "prefix")
        if run(["cmake", "--install", build_dir, "--prefix", prefix]).returncode:
            print("FAIL: cmake --install")
            return 1
        pcs = glob.glob(os.path.join(prefix, "**", "saaclient.pc"), recursive=True)
        if not pcs:
            print("FAIL: no saaclient.pc installed")
            return 1

        # 1. pkg-config, as in the README
        env = dict(os.environ)
        env["PKG_CONFIG_PATH"] = os.pathsep.join(filter(None, [os.path.dirname(pcs[0]),
                                                               env.get("PKG_CONFIG_PATH")]))
        flags = run(["pkg-config", "--cflags", "--libs", "saaclient"], env=env)
        via_pc = os.path.join(tmp, "minimal_pc")
        if flags.returncode or run([cc, os.path.join(PKG, "examples", "minimal", "main.c"), "-o", via_pc]
                                   + shlex.split(flags.stdout)).returncode:
            failures.append("building examples/minimal with pkg-config")

        # 2. find_package(saaclient)
        cm_build = os.path.join(tmp, "cmake-build")
        via_cmake = os.path.join(cm_build, "minimal")
        if run(["cmake", "-S", os.path.join(PKG, "examples", "minimal"), "-B", cm_build,
                f"-DCMAKE_PREFIX_PATH={prefix}", f"-DCMAKE_C_COMPILER={cc}"]).returncode or \
           run(["cmake", "--build", cm_build]).returncode:
            failures.append("building examples/minimal with find_package(saaclient)")
        builds = [("pkg-config", via_pc), ("find_package", via_cmake)]

        # 3. the C++ example, both ways
        if cxx:
            cpp_pc = os.path.join(tmp, "minimal_cpp_pc")
            if flags.returncode or run([cxx, "-std=c++11", os.path.join(PKG, "examples", "cpp", "minimal.cpp"),
                                        "-o", cpp_pc] + shlex.split(flags.stdout)).returncode:
                failures.append("building examples/cpp with pkg-config")
            cpp_build = os.path.join(tmp, "cmake-build-cpp")
            if run(["cmake", "-S", os.path.join(PKG, "examples", "cpp"), "-B", cpp_build,
                    f"-DCMAKE_PREFIX_PATH={prefix}", f"-DCMAKE_CXX_COMPILER={cxx}"]).returncode or \
               run(["cmake", "--build", cpp_build]).returncode:
                failures.append("building examples/cpp with find_package(saaclient)")
            builds += [("cpp-pkg-config", cpp_pc), ("cpp-find_package", os.path.join(cpp_build, "minimal_cpp"))]

        # 4. every binary streams to the mock server
        if do_run:
            pcm = os.path.join(tmp, "tone.raw")
            with open(pcm, "wb") as f:
                f.write(struct.pack("<48000h", *(int(8000 * math.sin(2 * math.pi * 220 * i / 16000))
                                                 for i in range(48000))))
            summary = os.path.join(tmp, "summary.jsonl")
            http, ws = free_port(), free_port()
            mock = subprocess.Popen([sys.executable, os.path.join(HERE, "mock_server.py"), "--http-port",
                                     str(http), "--ws-port", str(ws), "--summary", summary],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
            try:
                if not wait_listening(ws):
                    failures.append("the mock server did not start")
                for name, binary in builds:
                    if not os.path.exists(binary):
                        continue
                    with open(pcm, "rb") as stdin:
                        r = subprocess.run([binary, "--url", f"ws://127.0.0.1:{ws}/ws"], stdin=stdin,
                                           capture_output=True, text=True, timeout=30,
                                           env=dict(os.environ, SAA_API_KEY=f"consumer-{name}"))
                    if r.returncode or "connected" not in r.stdout:
                        failures.append(f"running the {name} build: exit {r.returncode}, {r.stdout + r.stderr!r}")
                time.sleep(0.5)                          # the mock writes a summary when a session ends
                sessions = []
                if os.path.exists(summary):
                    with open(summary) as f:
                        sessions = [json.loads(line) for line in f if line.strip()]
                for name, binary in builds:
                    if not os.path.exists(binary):
                        continue
                    got = [s for s in sessions if s["scenario"] == f"consumer-{name}"]
                    if not got or got[0]["audio_frames"] < 25 or got[0]["bad_frames"]:
                        failures.append(f"the mock saw {got} from the {name} build (want about 30 frames)")
            finally:
                mock.terminate()
                mock.wait(timeout=5)

    for msg in failures:
        print("FAIL:", msg)
    if not failures:
        print("ok: installed, built with pkg-config and find_package" + (", in C and C++" if cxx else "")
              + (", and streamed" if do_run else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
