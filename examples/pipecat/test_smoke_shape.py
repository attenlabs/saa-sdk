"""CI shape check for examples/pipecat/.

Runs in CI without ``pipecat-ai`` or ``attenlabs-saa`` installed (the
``telephony-shape`` job uses a base Python image). It opens each source
file with :mod:`ast` and asserts the public surface stays in shape.

Deeper behavioural tests live under ``tests/`` and require the runtime
dependencies to be installed.
"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
GATE = ROOT / "saa_gate.py"
BOT = ROOT / "bot.py"
OVERLAY = ROOT / "overlay_server.py"
README = ROOT / "README.md"
REQS = ROOT / "requirements.txt"
REQS_OVERLAY = ROOT / "requirements-overlay.txt"
DOCKERFILE = ROOT / "Dockerfile"
PCC_DEPLOY = ROOT / "pcc-deploy.toml"
ENV_EXAMPLE = ROOT / ".env.example"


def _parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _classes(tree: ast.AST) -> list[ast.ClassDef]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]


def _async_funcs(tree: ast.AST) -> list[ast.AsyncFunctionDef]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]


def _check_gate(failures: list[str]) -> None:
    src = GATE.read_text(encoding="utf-8")
    tree = _parse(GATE)

    classes = _classes(tree)
    gate_cls = next((c for c in classes if c.name == "SAAGate"), None)
    if gate_cls is None:
        failures.append("saa_gate.py: SAAGate class missing")
        return

    base_names = {
        b.id if isinstance(b, ast.Name) else getattr(b, "attr", "")
        for b in gate_cls.bases
    }
    if "FrameProcessor" not in base_names:
        failures.append("saa_gate.py: SAAGate must inherit from FrameProcessor")

    process_frame = next(
        (
            f for f in _async_funcs(gate_cls)
            if isinstance(f, ast.AsyncFunctionDef) and f.name == "process_frame"
        ),
        None,
    )
    if process_frame is None:
        failures.append("saa_gate.py: async def SAAGate.process_frame missing")

    required_pipecat_frames = (
        "InputAudioRawFrame",
        "StartFrame",
        "EndFrame",
        "CancelFrame",
        "BotStartedSpeakingFrame",
        "BotStoppedSpeakingFrame",
        "UserStartedSpeakingFrame",
        "UserStoppedSpeakingFrame",
        "InterruptionFrame",
        "DataFrame",
        "ErrorFrame",
    )
    for frame in required_pipecat_frames:
        if frame not in src:
            failures.append(f"saa_gate.py: must reference {frame}")

    required_saa_surfaces = (
        "AttentionClient",
        "on_speech_ready",
        "on_prediction",
        "on_state",
        "on_error",
        "on_vad",
        "on_stats",
        "on_config",
        "on_warmup_complete",
        "on_connecting",
        "on_connected",
        "on_disconnected",
        "on_reconnecting",
        "on_reconnected",
        "on_reconnect_failed",
        "mark_responding",
        "set_threshold",
        "feed_audio",
        "feed_video",
        "upstream_mode",
        "ReconnectConfig",
    )
    for surface in required_saa_surfaces:
        if surface not in src:
            failures.append(f"saa_gate.py: must reference {surface}")

    sidecar_frames = (
        "SAAPredictionFrame",
        "SAADecisionFrame",
        "SAAStatsFrame",
        "SAAConnectionFrame",
    )
    for cls_name in sidecar_frames:
        if not any(c.name == cls_name for c in classes):
            failures.append(f"saa_gate.py: sidecar dataclass {cls_name} missing")

    if "push_frame" not in src:
        failures.append("saa_gate.py: must call push_frame()")
    if "FrameDirection.DOWNSTREAM" not in src or "FrameDirection.UPSTREAM" not in src:
        failures.append("saa_gate.py: must use both DOWNSTREAM and UPSTREAM directions")
    if "asyncio.run_coroutine_threadsafe" not in src:
        failures.append(
            "saa_gate.py: must marshal SAA callbacks onto Pipecat's loop"
        )
    if "SAA_SAMPLE_RATE" not in src or ("16_000" not in src and "16000" not in src):
        failures.append("saa_gate.py: must declare SAA_SAMPLE_RATE = 16 kHz")
    if "start_ttfb_metrics" not in src or "stop_ttfb_metrics" not in src:
        failures.append("saa_gate.py: must integrate Pipecat TTFB metrics")
    if "push_error" not in src:
        failures.append("saa_gate.py: must propagate terminal errors via push_error")

    metrics_cls = next((c for c in classes if c.name == "SAAGateMetrics"), None)
    if metrics_cls is None:
        failures.append("saa_gate.py: SAAGateMetrics dataclass missing")


def _check_bot(failures: list[str]) -> None:
    src = BOT.read_text(encoding="utf-8")
    tree = _parse(BOT)

    required = (
        "SAAGate",
        "DeepgramSTTService",
        "OpenAILLMService",
        "CartesiaTTSService",
        "LLMContextAggregatorPair",
        "PipelineTask",
        "PipelineRunner",
        "transport.input()",
        "transport.output()",
        "create_transport",
    )
    for surface in required:
        if surface not in src:
            failures.append(f"bot.py: must reference {surface}")

    async_names = {f.name for f in _async_funcs(tree)}
    if "run_bot" not in async_names:
        failures.append("bot.py: async def run_bot missing")
    if "bot" not in async_names:
        failures.append("bot.py: async def bot(runner_args) entrypoint missing")

    if "transport_params" not in src:
        failures.append("bot.py: must declare transport_params dict")
    if "LocalAudioTransport" not in src:
        failures.append("bot.py: must offer a LocalAudioTransport path")
    for tp in ("daily", "smallwebrtc", "twilio"):
        if f'"{tp}"' not in src:
            failures.append(f"bot.py: missing transport entry {tp!r}")


def _check_overlay(failures: list[str]) -> None:
    if not OVERLAY.exists():
        failures.append("overlay_server.py missing")
        return
    src = OVERLAY.read_text(encoding="utf-8")
    tree = _parse(OVERLAY)
    classes = {c.name for c in _classes(tree)}

    if "OverlayHub" not in classes:
        failures.append("overlay_server.py: OverlayHub class missing")

    for needle in (
        "SAADecisionFrame",
        "SAAPredictionFrame",
        "SAAStatsFrame",
        "SAAConnectionFrame",
        "/saa/decisions",
        "EventSourceResponse",
        "build_app",
        "decision_listener",
    ):
        if needle not in src:
            failures.append(f"overlay_server.py: missing reference to {needle}")


def _check_docs(failures: list[str]) -> None:
    if not README.exists():
        failures.append("README.md missing")
        return
    text = README.read_text(encoding="utf-8")
    for needle in (
        "SAAGate",
        "transport.input()",
        "`bot.py`",
        "ATTENLABS_TOKEN",
        "`requirements.txt`",
        "feed_video",
        "barge-in",
        "overlay_server.py",
        "SAAPredictionFrame",
        "SAADecisionFrame",
        "SAAStatsFrame",
        "SAAConnectionFrame",
        "upstream mode",
        "pipecat-runner",
    ):
        if needle not in text:
            failures.append(f"README.md: missing reference to {needle}")

    if not REQS.exists():
        failures.append("requirements.txt missing")
        return
    rtxt = REQS.read_text(encoding="utf-8")
    if "pipecat-ai" not in rtxt:
        failures.append("requirements.txt: missing pipecat-ai")
    if "attenlabs-saa" not in rtxt:
        failures.append("requirements.txt: missing attenlabs-saa")

    if not REQS_OVERLAY.exists():
        failures.append("requirements-overlay.txt missing")
    else:
        otxt = REQS_OVERLAY.read_text(encoding="utf-8")
        for needle in ("starlette", "sse-starlette", "uvicorn"):
            if needle not in otxt:
                failures.append(f"requirements-overlay.txt: missing {needle}")

    if not ENV_EXAMPLE.exists():
        failures.append(".env.example missing")
    else:
        envtxt = ENV_EXAMPLE.read_text(encoding="utf-8")
        for var in (
            "ATTENLABS_TOKEN",
            "DEEPGRAM_API_KEY",
            "OPENAI_API_KEY",
            "CARTESIA_API_KEY",
            "SAA_THRESHOLD",
            "SAA_FORWARD_VIDEO",
            "SAA_BARGE_IN",
            "SAA_EMIT_SIDECAR",
        ):
            if var not in envtxt:
                failures.append(f".env.example: missing {var}")

    if not DOCKERFILE.exists():
        failures.append("Dockerfile missing")
    else:
        dtxt = DOCKERFILE.read_text(encoding="utf-8")
        if "pipecat-base" not in dtxt:
            failures.append("Dockerfile: must extend a pipecat base image")
        if "bot.py" not in dtxt:
            failures.append("Dockerfile: must COPY bot.py")
        if "overlay_server.py" not in dtxt:
            failures.append("Dockerfile: must COPY overlay_server.py")

    if not PCC_DEPLOY.exists():
        failures.append("pcc-deploy.toml missing")
    else:
        ptxt = PCC_DEPLOY.read_text(encoding="utf-8")
        if "agent_name" not in ptxt or "secret_set" not in ptxt:
            failures.append("pcc-deploy.toml: missing agent_name/secret_set")


def main() -> int:
    failures: list[str] = []
    try:
        _check_gate(failures)
        _check_bot(failures)
        _check_overlay(failures)
        _check_docs(failures)
    except SyntaxError as e:
        print(f"✗ syntax error: {e}", file=sys.stderr)
        return 1

    if failures:
        for f in failures:
            print(f"✗ {f}", file=sys.stderr)
        return 1
    print(
        "✓ pipecat shape: SAAGate(FrameProcessor) + sidecar frames + bot.py wiring "
        "+ overlay_server.py + README + requirements.txt"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
