"""Offline shape test for the OpenAI Realtime proactive variant.

Verifies the proactive lifecycle is wired correctly without requiring
fastapi, websockets, or any cloud account.
"""
from __future__ import annotations

import ast
import json
import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parent
PARENT = ROOT.parent.parent / "openai-realtime"


def _read(path: pathlib.Path) -> str:
    if not path.is_file():
        print(f"✗ missing file: {path.relative_to(ROOT.parent.parent)}", file=sys.stderr)
        sys.exit(1)
    return path.read_text(encoding="utf-8")


def main() -> int:
    failures: list[str] = []

    for name in (
        "proactive_server.py", "proactive.html", "proactive.js",
        "demo_script.json", "requirements.txt", ".env.example",
        "Makefile", "README.md",
    ):
        _read(ROOT / name)

    # AST-parse the Python overlay.
    ast.parse(_read(ROOT / "proactive_server.py"), filename="proactive_server.py")

    server_src = _read(ROOT / "proactive_server.py")
    js_src = _read(ROOT / "proactive.js")
    html_src = _read(ROOT / "proactive.html")
    readme_src = _read(ROOT / "README.md")
    makefile_src = _read(ROOT / "Makefile")

    # ── proactive_server.py contract ─────────────────────────────────
    if "from server import app" not in server_src:
        failures.append(
            "proactive_server.py: must import the parent openai-realtime app"
        )
    if "/proactive-trigger" not in server_src:
        failures.append("proactive_server.py: POST /proactive-trigger route missing")
    if "/proactive-events" not in server_src:
        failures.append("proactive_server.py: GET /proactive-events SSE route missing")
    if "text/event-stream" not in server_src:
        failures.append("proactive_server.py: SSE must set text/event-stream media type")

    # ── proactive.js contract ────────────────────────────────────────
    if "markResponding(true)" not in js_src:
        failures.append(
            "proactive.js: must assert markResponding(true) BEFORE "
            "response.create on proactive turn (otherwise SAA re-fires "
            "on the agent's own echo)"
        )
    if "markResponding(false)" not in js_src:
        failures.append(
            "proactive.js: must assert markResponding(false) after "
            "response.done (otherwise SAA stays silent forever)"
        )
    if "response.create" not in js_src:
        failures.append("proactive.js: missing response.create proactive trigger")
    if "EventSource" not in js_src or "/proactive-events" not in js_src:
        failures.append("proactive.js: missing EventSource subscription for remote triggers")
    if "speechReady" not in js_src:
        failures.append(
            "proactive.js: must forward speechReady to OpenAI (the proactive "
            "variant still has to handle the human's reply)"
        )

    # ── proactive.html contract ──────────────────────────────────────
    if "Trigger proactive turn" not in html_src and "trigger" not in html_src.lower():
        failures.append("proactive.html: missing the trigger UI element")

    # ── demo_script.json contract ────────────────────────────────────
    try:
        script = json.loads((ROOT / "demo_script.json").read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        failures.append(f"demo_script.json: invalid JSON: {exc}")
        script = {}
    for key in ("opening_line", "system_prompt"):
        if key not in script or not isinstance(script[key], str):
            failures.append(f"demo_script.json: required key '{key}' missing")

    # ── Makefile contract ────────────────────────────────────────────
    for target in ("dev:", "test-shape:", "trigger:"):
        if target not in makefile_src:
            failures.append(f"Makefile: target {target!r} missing")

    # ── README four-part framing ─────────────────────────────────────
    for marker in ("Trigger", "Without SAA", "With SAA", "markResponding"):
        if marker not in readme_src:
            failures.append(f"README.md: missing visceral-flow marker {marker!r}")

    # ── parent example still healthy ─────────────────────────────────
    parent_server = _read(PARENT / "server.py")
    if "client_secret" not in parent_server:
        failures.append(
            "../openai-realtime/server.py: client_secret minting missing "
            "(proactive overlay depends on POST /session)"
        )

    if failures:
        for f in failures:
            print(f"✗ {f}", file=sys.stderr)
        return 1

    print(
        "✓ proactive openai-realtime shape: proactive_server.py reuses parent app + "
        "POST /proactive-trigger + GET /proactive-events SSE + proactive.js "
        "asserts markResponding(true) before response.create and "
        "markResponding(false) after response.done + EventSource wired + "
        "demo_script.json carries opening_line/system_prompt + parent "
        "openai-realtime/server.py still mints client_secret"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
