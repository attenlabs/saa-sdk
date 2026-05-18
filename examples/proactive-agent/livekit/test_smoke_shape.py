"""Offline shape test for the LiveKit proactive variant."""
from __future__ import annotations

import ast
import json
import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parent
PARENT = ROOT.parent.parent / "livekit"


def _read(path: pathlib.Path) -> str:
    if not path.is_file():
        print(f"✗ missing file: {path.relative_to(ROOT.parent.parent)}", file=sys.stderr)
        sys.exit(1)
    return path.read_text(encoding="utf-8")


def main() -> int:
    failures: list[str] = []

    for name in (
        "proactive_agent.py", "demo_script.json", "requirements.txt",
        ".env.example", "Makefile", "README.md",
    ):
        _read(ROOT / name)

    ast.parse(_read(ROOT / "proactive_agent.py"), filename="proactive_agent.py")
    src = _read(ROOT / "proactive_agent.py")
    readme_src = _read(ROOT / "README.md")
    makefile_src = _read(ROOT / "Makefile")

    # ── proactive_agent.py contract ──────────────────────────────────
    if "from agent import" not in src:
        failures.append("proactive_agent.py: must reuse the parent agent module")
    if "session.generate_reply" not in src:
        failures.append(
            "proactive_agent.py: must call session.generate_reply for "
            "proactive turns (LiveKit canonical agent-speak API)"
        )
    if "/trigger" not in src:
        failures.append("proactive_agent.py: missing HTTP /trigger sidecar")
    if "_proactive_queue" not in src or "asyncio.Queue" not in src:
        failures.append(
            "proactive_agent.py: missing async queue connecting HTTP "
            "sidecar to session dispatcher"
        )
    if "add_shutdown_callback" not in src:
        failures.append(
            "proactive_agent.py: must register shutdown_callback to tear "
            "down the sidecar cleanly"
        )

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
    for target in ("dev:", "console:", "test-shape:", "trigger:"):
        if target not in makefile_src:
            failures.append(f"Makefile: target {target!r} missing")

    # ── README four-part framing ─────────────────────────────────────
    for marker in ("Trigger", "Without SAA", "With SAA", "mark_responding"):
        if marker not in readme_src:
            failures.append(f"README.md: missing visceral-flow marker {marker!r}")

    # ── parent example still healthy ─────────────────────────────────
    parent_agent = _read(PARENT / "agent.py")
    if "generate_reply" not in parent_agent:
        failures.append(
            "../../livekit/agent.py: session.generate_reply missing "
            "(proactive overlay depends on it)"
        )
    if "agent_state_changed" not in parent_agent:
        failures.append(
            "../../livekit/agent.py: agent_state_changed handler missing "
            "(proactive overlay relies on parent's mark_responding wiring)"
        )

    if failures:
        for f in failures:
            print(f"✗ {f}", file=sys.stderr)
        return 1

    print(
        "✓ proactive livekit shape: proactive_agent.py reuses parent "
        "entrypoint + HTTP /trigger sidecar + asyncio queue dispatcher "
        "+ session.generate_reply + shutdown teardown + demo_script.json "
        "+ parent agent.py still exposes generate_reply and agent_state_changed"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
