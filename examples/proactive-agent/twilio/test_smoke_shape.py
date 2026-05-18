"""Offline smoke test for examples/proactive-agent/.

Runs in CI on every push. AST-parses each module and asserts the
contracts the README and the canonical doc promise. No FastAPI,
Twilio, OpenAI, NumPy, or websockets installs required.

The proactive-agent example is intentionally a thin overlay on
examples/twilio/, so this test focuses on:

1. The proactive bridge subclasses OpenAIRealtimeBridge and fires a
   ``response.create`` event in ``open()`` (the proactive opening turn).
2. ``main.py`` registers the proactive bridge via ``set_bridge_factory``
   and exposes ``POST /place-proactive-call``.
3. ``mark_responding`` is reachable through the proactive path (we
   don't re-implement it; we rely on the parent adapter's
   ``mark_responding(True)`` auto-fire on outbound bytes).
4. The CLI wrapper calls into ``examples/twilio/outbound.py``.
5. The demo script JSON exists and has the required keys.
"""
from __future__ import annotations

import ast
import json
import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parent
TWILIO = ROOT.parent.parent / "twilio"


def _read(path: pathlib.Path) -> str:
    if not path.is_file():
        print(f"✗ missing file: {path.relative_to(ROOT.parent)}", file=sys.stderr)
        sys.exit(1)
    return path.read_text(encoding="utf-8")


def _parse(path: pathlib.Path) -> ast.AST:
    src = _read(path)
    try:
        return ast.parse(src, filename=str(path))
    except SyntaxError as e:
        print(f"✗ syntax error in {path}: {e}", file=sys.stderr)
        sys.exit(1)


def main() -> int:
    failures: list[str] = []

    # ── files present ────────────────────────────────────────────────
    for name in (
        "ai_sdr_bridge.py", "main.py", "place_proactive_call.py",
        "demo_script.json", "requirements.txt", ".env.example",
        "Makefile", "Dockerfile", "README.md",
    ):
        _read(ROOT / name)

    # AST-parse Python sources so syntax errors fail fast.
    for name in ("ai_sdr_bridge.py", "main.py", "place_proactive_call.py"):
        _parse(ROOT / name)

    bridge_src = _read(ROOT / "ai_sdr_bridge.py")
    main_src = _read(ROOT / "main.py")
    cli_src = _read(ROOT / "place_proactive_call.py")
    readme_src = _read(ROOT / "README.md")
    makefile_src = _read(ROOT / "Makefile")

    # ── proactive bridge contract ────────────────────────────────────
    if "class AISDRBridge" not in bridge_src:
        failures.append("ai_sdr_bridge.py: class AISDRBridge missing")
    if "OpenAIRealtimeBridge" not in bridge_src:
        failures.append(
            "ai_sdr_bridge.py: AISDRBridge must subclass OpenAIRealtimeBridge "
            "(no duplicated Realtime client; reuse the Twilio adapter's bridge)"
        )
    if "async def open" not in bridge_src:
        failures.append("ai_sdr_bridge.py: open() override missing (proactive turn lives here)")
    if "response.create" not in bridge_src:
        failures.append(
            "ai_sdr_bridge.py: open() must emit a response.create event "
            "so the agent speaks first (the proactive opening turn)"
        )
    if "from_env" not in bridge_src:
        failures.append("ai_sdr_bridge.py: from_env() factory constructor missing")
    if "demo_script.json" not in bridge_src:
        failures.append("ai_sdr_bridge.py: demo_script.json not referenced (operators edit the script)")
    if "opening_line" not in bridge_src:
        failures.append("ai_sdr_bridge.py: opening_line not threaded into the proactive turn")

    # ── main.py overlay contract ─────────────────────────────────────
    if "from server import app" not in main_src:
        failures.append(
            "main.py: must import the Twilio adapter's app (no duplicated server)"
        )
    if "set_bridge_factory" not in main_src:
        failures.append("main.py: must register AISDRBridge via set_bridge_factory")
    if "AISDRBridge" not in main_src:
        failures.append("main.py: must register AISDRBridge (the proactive bridge)")
    if "/place-proactive-call" not in main_src:
        failures.append(
            "main.py: HTTP POST /place-proactive-call route missing "
            "(webhook trigger for CRM/scheduling integrations)"
        )
    if "@app.post" not in main_src:
        failures.append("main.py: @app.post decorator not used for /place-proactive-call")

    # ── CLI wrapper contract ─────────────────────────────────────────
    if "from outbound import" not in cli_src:
        failures.append(
            "place_proactive_call.py: must reuse examples/twilio/outbound.py "
            "(no duplicated Twilio REST dialer)"
        )

    # ── demo script JSON contract ────────────────────────────────────
    try:
        script = json.loads((ROOT / "demo_script.json").read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        failures.append(f"demo_script.json: invalid JSON: {exc}")
        script = {}
    for key in ("opening_line", "system_prompt"):
        if key not in script or not isinstance(script[key], str) or not script[key].strip():
            failures.append(f"demo_script.json: required key '{key}' missing or empty")

    # ── Makefile contract ────────────────────────────────────────────
    for target in ("dev:", "run:", "test-shape:", "place-call:"):
        if target not in makefile_src:
            failures.append(f"Makefile: target {target!r} missing")
    if "main:app" not in makefile_src:
        failures.append(
            "Makefile: must run main:app (proactive overlay), not server:app"
        )

    # ── parent adapter still healthy ────────────────────────────────
    # The overlay depends on examples/twilio/'s set_bridge_factory and
    # mark_responding wiring. If those break, the proactive flow breaks.
    parent_server = _read(TWILIO / "server.py")
    if "set_bridge_factory" not in parent_server:
        failures.append(
            "../twilio/server.py: set_bridge_factory hook missing "
            "(proactive overlay depends on it)"
        )
    if "mark_responding" not in parent_server:
        failures.append(
            "../twilio/server.py: mark_responding wiring missing "
            "(proactive overlay relies on auto-fire on outbound bytes)"
        )

    # ── README four-part flow framing ───────────────────────────────
    # The README is the centerpiece of the example; it must carry the
    # visceral framing (Trigger / Without SAA / With SAA / Existing in
    # the repo) for the AI-SDR flow. Check that all four are named.
    for marker in ("Trigger", "Without SAA", "With SAA", "mark_responding"):
        if marker not in readme_src:
            failures.append(f"README.md: missing visceral-flow marker {marker!r}")

    if failures:
        for f in failures:
            print(f"✗ {f}", file=sys.stderr)
        return 1

    print(
        "✓ proactive-agent shape: AISDRBridge subclasses OpenAIRealtimeBridge + "
        "open() fires response.create proactive turn + main.py registers via "
        "set_bridge_factory + POST /place-proactive-call + demo_script.json "
        "carries opening_line/system_prompt + Makefile runs main:app + "
        "parent twilio/server.py still exposes set_bridge_factory and "
        "mark_responding + README carries Trigger/Without SAA/With SAA framing"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
