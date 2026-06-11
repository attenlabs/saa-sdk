# SAA-gated ElevenLabs Conversational AI agent.
#
# ElevenLabs owns its own (sealed) WebRTC room, so SAA cannot join as a hidden
# participant the way it does on LiveKit / Daily. Instead this taps ElevenLabs'
# AudioInterface — the clean both-directions PCM seam in their Python SDK —
# feeds the user mic to the SAA cloud via attenlabs-saa's feed_audio(), and
# gates the agent by only forwarding device-directed audio onward. The SAA
# classifier runs on Attention Labs' infra; no model ships here.
import logging
import os
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from elevenlabs import ElevenLabs
from elevenlabs.conversational_ai.conversation import AudioInterface, Conversation
from elevenlabs.conversational_ai.default_audio_interface import DefaultAudioInterface

from saa import AttentionClient

# auto-load the shared examples/elevenlabs/.env
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

logger = logging.getLogger("elevenlabs-saa")


class SAAFeedAudioInterface(AudioInterface):
    """Wraps an ElevenLabs AudioInterface and wires SAA in two ways:

    - tees the user mic into AttentionClient.feed_audio (so SAA classifies it),
      then forwards to ElevenLabs *only when the gate is open*, only audio
      SAA judged device-directed (class 2) reaches the agent.
    - watches agent TTS on output() to drive saa.mark_responding, so SAA knows
      when the agent itself is speaking (arms barge-in, suppresses noise).

    The SDK's own mic is disabled (enable_audio=False); this is the only audio
    source, fed frame-by-frame.
    """

    def __init__(self, base: AudioInterface, saa: AttentionClient, *,
                 gate: bool = True, responding_idle_ms: int = 500):
        self._base = base
        self._saa = saa
        self._gate = gate
        # when gating, start closed and open on the first device-directed tick
        self._gate_open = not gate
        self._user_cb = None
        self._responding_idle_s = responding_idle_ms / 1000.0
        self._responding = False
        self._last_output_ts = 0.0
        self._lock = threading.Lock()
        self._wd_stop = threading.Event()
        self._wd: threading.Thread | None = None

    # ── gate control (driven by SAA predictions) ───────────────────────
    def set_gate_open(self, is_open: bool) -> None:
        self._gate_open = is_open

    # ── AudioInterface contract ─────────────────────────────────────────
    def start(self, input_callback):
        self._user_cb = input_callback
        self._wd_stop.clear()
        self._wd = threading.Thread(target=self._watchdog, name="saa-responding", daemon=True)
        self._wd.start()
        self._base.start(self._tee)

    def stop(self):
        self._wd_stop.set()
        if self._wd is not None:
            self._wd.join(timeout=1.0)
            self._wd = None
        self._base.stop()

    def output(self, audio: bytes):
        with self._lock:
            self._last_output_ts = time.monotonic()
            if not self._responding:
                self._responding = True
                self._saa.mark_responding(True)
        self._base.output(audio)

    def interrupt(self):
        self._base.interrupt()
        self._set_responding(False)

    # ── internals ───────────────────────────────────────────────────────
    def _tee(self, audio: bytes):
        # feed SAA continuously (it must see every frame to classify), but only
        # forward to ElevenLabs when SAA says the speech is device-directed
        try:
            self._saa.feed_audio(audio)
        except Exception:
            logger.exception("feed_audio failed")
        if self._gate_open and self._user_cb is not None:
            self._user_cb(audio)

    def _set_responding(self, value: bool):
        with self._lock:
            self._last_output_ts = 0.0 if not value else self._last_output_ts
            if self._responding != value:
                self._responding = value
                self._saa.mark_responding(value)

    def _watchdog(self):
        # ElevenLabs has no clean end-of-turn callback in output(); flip
        # responding back off after a short gap with no TTS chunks
        while not self._wd_stop.is_set():
            self._wd_stop.wait(0.1)
            if self._wd_stop.is_set():
                break
            with self._lock:
                idle = (self._responding and self._last_output_ts > 0
                        and time.monotonic() - self._last_output_ts > self._responding_idle_s)
            if idle:
                self._set_responding(False)


def main() -> int:
    token = os.environ.get("ATTENLABS_TOKEN")
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    agent_id = os.environ.get("ELEVENLABS_AGENT_ID")
    if not (token and api_key and agent_id):
        print("set ATTENLABS_TOKEN, ELEVENLABS_API_KEY, ELEVENLABS_AGENT_ID")
        return 2

    # streaming SDK in feed mode — no self-capture; we feed the ElevenLabs tap.
    # audio-only: ElevenLabs gives no video, so SAA runs on audio alone.
    saa = AttentionClient(token=token, enable_audio=False, enable_video=False)
    attn = SAAFeedAudioInterface(DefaultAudioInterface(), saa, gate=True)

    @saa.on_prediction
    def _(ev):
        # the gate: only device-directed speech (class 2) reaches the agent.
        # cls is the server's smoothed/aligned class.
        attn.set_gate_open(ev.cls == 2)

    @saa.on_interrupt
    def _(ev):
        # informational — ElevenLabs runs its own barge-in detection
        logger.info("SAA interrupt (conf=%.2f)", ev.confidence)

    @saa.on_error
    def _(ev):
        logger.warning("SAA error: %s — %s", ev.title, ev.message)

    conversation = Conversation(
        client=ElevenLabs(api_key=api_key),
        agent_id=agent_id,
        requires_auth=True,
        audio_interface=attn,
        callback_agent_response=lambda r: print(f"agent: {r}"),
        callback_user_transcript=lambda t: print(f"user:  {t}"),
    )

    # SAA first so feed_audio is live before the ElevenLabs mic tee starts
    saa.start()
    conversation.start_session()
    print("SAA-gated ElevenLabs session started — Ctrl+C to end")
    try:
        conversation.wait_for_session_end()
    except KeyboardInterrupt:
        pass
    finally:
        conversation.end_session()
        saa.stop()
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
