"""Smoke test for the SAA × LiveKit Agents reference adapter.

This test runs in CI (see ``.github/workflows/adapter-smoke.yml``). It must
stay no-network and not require ``livekit-agents`` to be installed, we
only ``ast.parse`` the source files and walk the tree.

It asserts the public shape of:

  - saa_gate.py
      class SAAAudioBridge       (pre-ASR gate, upstream mode)
          feed_audio_frame, feed_video_jpeg, iter_speech_frames,
          run_stt_node, start, stop, set_threshold, snapshot
      class SAAGate              (response gate, legacy)
          start, stop, is_open, mark_responding, set_threshold, snapshot
      class SAAGatedSTT          wraps an stt.STT plugin
      class SAAAudioStream       yields rtc.AudioFrame at 16 kHz
      registers on_speech_ready + on_prediction handlers on the AttentionClient
      helper: _to_mono_16k_pcm16 (resampler used by the bridge + pipecat parity)

  - agent.py
      class SAAPreSTTAssistant(Agent)       , default mode, overrides stt_node
      class SAAResponseGatedAssistant(Agent), legacy on_user_turn_completed
      async def entrypoint(ctx: JobContext) -> None
      def main()                             , console-script entry
      session.on('agent_state_changed') handler (the 1.x replacement
          for the deprecated agent_started/stopped_speaking pair)
      track_subscribed listener for camera forwarding
      cli.run_app(WorkerOptions(entrypoint_fnc=...))
      function_tool wiring for runtime SAA control

Failure messages name the missing surface so a future regression is easy
to diagnose.
"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
GATE = ROOT / 'saa_gate.py'
AGENT = ROOT / 'agent.py'


def _classes(tree: ast.AST) -> list[ast.ClassDef]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]


def _async_funcs(tree: ast.AST) -> list[ast.AsyncFunctionDef]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]


def _funcs(tree: ast.AST) -> list[ast.FunctionDef]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]


def _has_method(cls: ast.ClassDef, name: str, *, is_async: bool = False) -> bool:
    target = ast.AsyncFunctionDef if is_async else ast.FunctionDef
    if any(isinstance(n, target) and n.name == name for n in cls.body):
        return True
    # Allow either sync or async, some pipeline overrides are coroutine
    # functions, others return an async iterable from a sync def.
    other = ast.FunctionDef if is_async else ast.AsyncFunctionDef
    return any(isinstance(n, other) and n.name == name for n in cls.body)


def _has_class(classes: list[ast.ClassDef], name: str) -> bool:
    return any(c.name == name for c in classes)


def _check_gate(failures: list[str]) -> None:
    src = GATE.read_text()
    try:
        tree = ast.parse(src, filename=str(GATE))
    except SyntaxError as e:
        failures.append(f'saa_gate.py syntax error: {e}')
        return

    classes = _classes(tree)
    func_names = {f.name for f in _funcs(tree)} | {f.name for f in _async_funcs(tree)}

    for cls_name in ('SAAGate', 'SAAGatedSTT', 'SAAAudioStream', 'SAAAudioBridge'):
        if not _has_class(classes, cls_name):
            failures.append(f'saa_gate: class {cls_name} missing')

    saa_gate_cls = next((c for c in classes if c.name == 'SAAGate'), None)
    if saa_gate_cls is not None:
        for method in ('start', 'stop', 'is_open', 'mark_responding', 'set_threshold', 'snapshot'):
            if not _has_method(saa_gate_cls, method):
                failures.append(f'saa_gate: SAAGate.{method}() missing')

    bridge_cls = next((c for c in classes if c.name == 'SAAAudioBridge'), None)
    if bridge_cls is not None:
        for method in (
            'start',
            'stop',
            'feed_audio_frame',
            'feed_video_jpeg',
            'set_threshold',
            'mark_responding',
            'mute',
            'unmute',
            'snapshot',
            'iter_speech_frames',
            'run_stt_node',
        ):
            # Some methods are async generators / coroutines, try both.
            if not (_has_method(bridge_cls, method) or _has_method(bridge_cls, method, is_async=True)):
                failures.append(f'saa_gate: SAAAudioBridge.{method}() missing')

    # Wire-shape sanity (LiveKit AudioFrame, AudioTrack, 16 kHz).
    for needle, why in (
        ('rtc.AudioFrame', 'rtc.AudioFrame not referenced'),
        ('rtc.AudioTrack', 'rtc.AudioTrack not referenced'),
        ('sample_rate=SAA_SAMPLE_RATE', 'audio frame must specify sample_rate=SAA_SAMPLE_RATE (16000) for SAA path'),
        ('on_speech_ready', 'on_speech_ready handler not registered on the AttentionClient'),
        ('on_prediction', 'on_prediction handler not registered on the AttentionClient'),
        ('upstream_mode=True', 'SAAAudioBridge must open the AttentionClient in upstream_mode=True'),
        ('feed_audio(', 'pre-ASR gate must call AttentionClient.feed_audio(...)'),
        ('feed_video(', 'multi-modal path must call AttentionClient.feed_video(...)'),
        ('SpeechEventType.FINAL_TRANSCRIPT', 'SAAGatedSTT must reference SpeechEventType.FINAL_TRANSCRIPT'),
    ):
        if needle not in src:
            failures.append(f'saa_gate: {why}')

    if '_to_mono_16k_pcm16' not in func_names:
        failures.append('saa_gate: _to_mono_16k_pcm16 resampler helper missing')


def _check_agent(failures: list[str]) -> None:
    if not AGENT.exists():
        failures.append('agent.py: missing (industry-standard LiveKit examples ship a runnable agent.py)')
        return
    src = AGENT.read_text()
    try:
        tree = ast.parse(src, filename=str(AGENT))
    except SyntaxError as e:
        failures.append(f'agent.py syntax error: {e}')
        return

    classes = _classes(tree)
    async_funcs = _async_funcs(tree)
    sync_funcs = _funcs(tree)
    sync_func_names = {f.name for f in sync_funcs}

    # Two agent classes: production pre-STT + legacy response-gate.
    for cls_name in ('SAAPreSTTAssistant', 'SAAResponseGatedAssistant'):
        if not _has_class(classes, cls_name):
            failures.append(f'agent: class {cls_name} missing')

    pre_stt = next((c for c in classes if c.name == 'SAAPreSTTAssistant'), None)
    if pre_stt is not None:
        if not any(
            (isinstance(b, ast.Name) and b.id == 'Agent')
            or (isinstance(b, ast.Attribute) and b.attr == 'Agent')
            for b in pre_stt.bases
        ):
            failures.append('agent: SAAPreSTTAssistant must inherit from Agent')
        if not (_has_method(pre_stt, 'stt_node') or _has_method(pre_stt, 'stt_node', is_async=True)):
            failures.append('agent: SAAPreSTTAssistant.stt_node override missing (pre-ASR gate is the whole point)')
        if not _has_method(pre_stt, 'on_user_turn_completed', is_async=True):
            failures.append('agent: SAAPreSTTAssistant.on_user_turn_completed (async) missing')

    response_gated = next((c for c in classes if c.name == 'SAAResponseGatedAssistant'), None)
    if response_gated is not None:
        if not _has_method(response_gated, 'on_user_turn_completed', is_async=True):
            failures.append('agent: SAAResponseGatedAssistant.on_user_turn_completed (async) missing')

    # Entrypoint coroutine + worker options + main() entry.
    if not any(f.name == 'entrypoint' for f in async_funcs):
        failures.append('agent: async def entrypoint(ctx) missing')
    if 'main' not in sync_func_names:
        failures.append('agent: def main() (console-script entry) missing')

    for needle, why in (
        ('AgentSession', 'AgentSession (livekit-agents 1.0+) not used'),
        ('JobContext', 'JobContext not referenced'),
        ('WorkerOptions', 'WorkerOptions not referenced'),
        ('StopResponse', 'response-gate path must raise StopResponse to skip a turn'),
        # Bot-speech suppression must use the canonical 1.x event, the
        # 0.x ``agent_started_speaking`` / ``agent_stopped_speaking``
        # names were removed.
        ('agent_state_changed', 'must use agent_state_changed (1.x event) for mark_responding wiring'),
        ('mark_responding', 'mark_responding hook missing (own-voice feedback loop guard)'),
        ('cli.run_app', 'cli.run_app(WorkerOptions(...)) entry missing'),
        ('SAAAudioBridge', 'agent.py must wire SAAAudioBridge for the pre-ASR path'),
        ('SAAGate', 'agent.py must keep SAAGate available for response-gate mode'),
        ('track_subscribed', 'agent.py must subscribe to participant tracks for camera forwarding'),
        ('feed_video_jpeg', 'agent.py must forward camera JPEGs to SAA (multi-modal)'),
        ('function_tool', 'agent.py must expose runtime SAA controls via function_tool'),
        ('set_attention_sensitivity', 'agent.py must expose a threshold-tuning function tool'),
        ('SAA_GATE_MODE', 'agent.py must honour SAA_GATE_MODE env var'),
        ('RoomInputOptions', 'agent.py must pass RoomInputOptions to session.start()'),
    ):
        if needle not in src:
            failures.append(f'agent: {why}')

    # The deprecated 0.x event names must be gone.
    for stale in ('agent_started_speaking', 'agent_stopped_speaking'):
        if stale in src:
            failures.append(
                f'agent: deprecated event "{stale}" still referenced, '
                'livekit-agents 1.x uses agent_state_changed with new_state == "speaking"'
            )


def main() -> int:
    failures: list[str] = []
    _check_gate(failures)
    _check_agent(failures)

    if failures:
        for f in failures:
            print(f'✗ {f}', file=sys.stderr)
        return 1

    print(
        '✓ saa_gate shape: SAAAudioBridge (pre-ASR) + SAAGate + SAAGatedSTT + '
        'SAAAudioStream + AudioTrack/AudioFrame + 16 kHz + on_speech_ready/on_prediction'
    )
    print(
        '✓ agent shape: SAAPreSTTAssistant.stt_node + SAAResponseGatedAssistant + '
        'agent_state_changed + track_subscribed + function_tool + cli.run_app(WorkerOptions) + main()'
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
