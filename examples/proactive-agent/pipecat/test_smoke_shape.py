"""Offline shape test for the Pipecat proactive variant.

Verifies the proactive HTTP sidecar wiring and the dispatcher
mechanism without requiring pipecat/cartesia/deepgram/openai installs.
"""
from __future__ import annotations

import ast
import json
import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parent
PARENT = ROOT.parent.parent / "pipecat"


def _read(path: pathlib.Path) -> str:
    if not path.is_file():
        print(f"✗ missing file: {path.relative_to(ROOT.parent.parent)}", file=sys.stderr)
        sys.exit(1)
    return path.read_text(encoding="utf-8")


def main() -> int:
    failures: list[str] = []

    for name in (
        "proactive_bot.py", "demo_script.json", "requirements.txt",
        ".env.example", "Makefile", "README.md",
    ):
        _read(ROOT / name)

    ast.parse(_read(ROOT / "proactive_bot.py"), filename="proactive_bot.py")

    src = _read(ROOT / "proactive_bot.py")
    readme_src = _read(ROOT / "README.md")
    makefile_src = _read(ROOT / "Makefile")

    # ── proactive_bot.py contract ────────────────────────────────────
    if "from bot import" not in src:
        failures.append("proactive_bot.py: must reuse the parent bot module")
    if "LLMRunFrame" not in src:
        failures.append(
            "proactive_bot.py: proactive turn must queue an LLMRunFrame "
            "into the parent PipelineTask"
        )
    if "/trigger" not in src:
        failures.append("proactive_bot.py: missing HTTP /trigger sidecar route")
    if "_proactive_queue" not in src or "asyncio.Queue" not in src:
        failures.append(
            "proactive_bot.py: missing async queue connecting HTTP "
            "sidecar to pipeline dispatcher"
        )
    if "on_client_connected" not in src:
        failures.append(
            "proactive_bot.py: must preserve parent's on_client_connected "
            "opening turn"
        )
    if "demo_script.json" not in src:
        failures.append(
            "proactive_bot.py: must load demo_script.json so operators can "
            "edit the campaign without redeploying"
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
    for target in ("dev:", "test-shape:", "trigger:"):
        if target not in makefile_src:
            failures.append(f"Makefile: target {target!r} missing")
    if "pipecat-runner" not in makefile_src:
        failures.append("Makefile: dev target should invoke pipecat-runner")

    # ── README four-part framing ─────────────────────────────────────
    for marker in ("Trigger", "Without SAA", "With SAA", "mark_responding"):
        if marker not in readme_src:
            failures.append(f"README.md: missing visceral-flow marker {marker!r}")

    # ── parent example still healthy ─────────────────────────────────
    parent_bot = _read(PARENT / "bot.py")
    if "on_client_connected" not in parent_bot:
        failures.append(
            "../../pipecat/bot.py: on_client_connected hook missing "
            "(proactive overlay reuses it)"
        )
    if "LLMRunFrame" not in parent_bot:
        failures.append(
            "../../pipecat/bot.py: LLMRunFrame not used "
            "(proactive overlay reuses the same opening turn shape)"
        )

    if failures:
        for f in failures:
            print(f"✗ {f}", file=sys.stderr)
        return 1

    print(
        "✓ proactive pipecat shape: proactive_bot.py reuses parent run_bot + "
        "HTTP /trigger sidecar + asyncio queue dispatcher + LLMRunFrame "
        "injection + demo_script.json + on_client_connected preserved + "
        "parent bot.py still exposes the canonical opening-turn shape"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
