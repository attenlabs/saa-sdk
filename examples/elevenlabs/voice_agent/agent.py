# SAA-gated ElevenLabs Conversational AI agent
#
# SAA owns the turn boundary. ElevenLabs gets the user's real mic audio only while
# SAA says it's device-directed; on SAA's on_turn_ready a short silence tail is sent
# to trigger ElevenLabs' endpoint, and nothing is sent between turns. No continuous
# silence stream, so the keepalive can no longer race/cancel the endpoint.
# Needs ElevenLabs turn-taking enabled — the silence tail is the only reply trigger.
# Logs prediction / responding / send state so the gating can be observed (no TUI).
import argparse
import logging
import os
import sys
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from elevenlabs import ElevenLabs
from elevenlabs.conversational_ai.conversation import AudioInterface, Conversation
from elevenlabs.conversational_ai.default_audio_interface import DefaultAudioInterface

from saa import AttentionClient

# import the shared SessionLog helper as a sibling module under examples/_shared
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))
from session_log import SessionLog  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

log = logging.getLogger("elevenlabs-saa")

# DefaultAudioInterface plays agent TTS at 16 kHz mono PCM16 → 32000 bytes/sec.
OUTPUT_BYTES_PER_SEC = 16000 * 2


class SAAFeedAudioInterface(AudioInterface):
    """Tees the mic into SAA and feeds ElevenLabs the user's real audio only while
    device-directed; on on_turn_ready it streams silence until the agent replies so
    ElevenLabs' turn model endpoints, and sends nothing between turns (Option B).

    - Gate opens on class-2 (device-directed) and closes at once on class-1
      (human-directed). A class-0 (silence) dip is debounced for
      `close_debounce_ticks` ticks so a pause mid-utterance doesn't chop the turn.

    - `responding` is tracked by agent-TTS *playback* duration, not output() calls:
      DefaultAudioInterface queues output() instantly but plays on a background
      thread, so output()-idle would flip responding off mid-playback and the
      agent's echo (no AEC) would leak back as user audio.

    - `endpoint()` (called on on_turn_ready) opens the awaiting-reply window. The
      watchdog then streams 100 ms silence chunks for the whole window so the
      `turn_v3` model gets the continuous trailing silence it needs to endpoint
      (a single short tail starves it). Silence stops the moment the agent replies
      (output()), the gate reopens, or `awaiting_timeout_ms` elapses.

    - The keepalive ping (`bind_keepalive`) resets ElevenLabs' turn timer so its
      no-input timeout never re-prompts during long idle. It fires only when idle
      and not awaiting a reply, so it can never coincide with an endpoint.
    """

    def __init__(self, base: AudioInterface, saa: AttentionClient, *,
                 close_debounce_ticks: int = 4, responding_tail_ms: int = 300,
                 keepalive_s: float = 5.0, awaiting_timeout_ms: int = 6000):
        self._base = base
        self._saa = saa
        self._close_debounce = close_debounce_ticks
        self._gate_open = False         # start closed; opens on first device-directed tick
        self._misses = 0                # consecutive non-device-directed ticks
        self._user_cb = None
        self._primed = False
        self._tail_s = responding_tail_ms / 1000.0
        self._responding = False
        self._play_until = 0.0          # monotonic deadline: agent audio plays until here
        self._sending_real = False      # last send state (real vs off) for transition logs
        self._silence_chunk = bytes(int(0.1 * 16000) * 2)  # 100 ms zero PCM, streamed while awaiting
        self._awaiting_reply = False    # turn done, agent reply not yet started
        self._awaiting_since = 0.0
        self._awaiting_timeout_s = awaiting_timeout_ms / 1000.0
        self._keepalive_s = keepalive_s # ping period to reset ElevenLabs' turn timer
        self._keepalive_cb = None       # set by bind_keepalive once the session is live
        self._last_keepalive = 0.0
        self._lock = threading.Lock()
        self._wd_stop = threading.Event()
        self._wd: threading.Thread | None = None

    @property
    def responding(self) -> bool:
        return self._responding

    @property
    def gate_open(self) -> bool:
        return self._gate_open

    def bind_keepalive(self, cb) -> None:
        # call once the ElevenLabs session is live: cb resets its turn timer so the
        # no-input timeout never re-prompts while the gate is shut.
        self._keepalive_cb = cb

    def update_gate(self, cls: int) -> None:
        # open on device-directed (2); human-directed (1) closes at once; a
        # class-0 (silence) dip is debounced so a pause mid-utterance doesn't chop.
        if cls == 2:
            self._misses = 0
            self._gate_open = True
        elif cls == 1:
            self._gate_open = False          # human-directed — never the device
        elif self._gate_open:                # cls == 0: silence, maybe a pause
            self._misses += 1
            if self._misses >= self._close_debounce:
                self._gate_open = False

    # ── lifecycle ─────────────────────────────────────────────────────────
    def prime(self) -> None:
        # start the mic feeding SAA before the ElevenLabs session connects
        if self._primed:
            return
        self._primed = True
        self._start_watchdog()
        self._base.start(self._tee)

    def _start_watchdog(self) -> None:
        self._wd_stop.clear()
        self._wd = threading.Thread(target=self._watchdog, name="saa-responding", daemon=True)
        self._wd.start()

    def start(self, input_callback):
        self._user_cb = input_callback
        if self._primed:
            return
        self._start_watchdog()
        self._base.start(self._tee)

    def stop(self):
        self._wd_stop.set()
        if self._wd is not None:
            self._wd.join(timeout=1.0)
            self._wd = None
        self._base.stop()

    # ── agent TTS ─────────────────────────────────────────────────────────
    def output(self, audio: bytes):
        now = time.monotonic()
        first = False
        with self._lock:
            # extend playback deadline by this chunk's duration (queue draining)
            self._play_until = max(self._play_until, now) + len(audio) / OUTPUT_BYTES_PER_SEC
            self._awaiting_reply = False   # reply started
            if not self._responding:
                self._responding = True
                first = True
        if first:
            self._saa.mark_responding(True)
            log.info("RESP -> SPEAKING")
        self._base.output(audio)

    def interrupt(self):
        self._base.interrupt()          # clears the playback queue immediately
        with self._lock:
            self._play_until = time.monotonic()
            self._awaiting_reply = False
        self._set_responding(False, "interrupt")

    def endpoint(self) -> None:
        # SAA reported the turn is done — open the awaiting-reply window. The
        # watchdog streams silence through it so turn_v3 gets continuous trailing
        # silence to endpoint on (keepalive suppressed for the whole window).
        if self._user_cb is None or self._responding:
            return
        self._awaiting_reply = True
        self._awaiting_since = time.monotonic()
        log.info("ENDPOINT — streaming silence until reply (max %.1fs)", self._awaiting_timeout_s)

    # ── internals ─────────────────────────────────────────────────────────
    def _tee(self, audio: bytes):
        # always feed SAA so it classifies every frame
        try:
            self._saa.feed_audio(audio)
        except Exception:
            log.exception("feed_audio failed")
        if self._user_cb is None:
            return  # priming: SAA only, ElevenLabs not connected yet
        # send real audio only while device-directed and the agent isn't speaking;
        # nothing otherwise. the endpoint tail comes from endpoint(), not here.
        send_real = self._gate_open and not self._responding
        if send_real != self._sending_real:
            self._sending_real = send_real
            log.info("SEND %s  (gate=%s resp=%s)",
                     "real" if send_real else "off", self._gate_open, self._responding)
        if send_real:
            self._last_keepalive = time.monotonic()
            self._user_cb(audio)

    def _set_responding(self, value: bool, reason: str):
        fire = False
        with self._lock:
            if self._responding != value:
                self._responding = value
                fire = True
        if fire:
            self._saa.mark_responding(value)
            if not value:
                log.info("RESP -> idle  (%s)", reason)

    def _watchdog(self):
        while not self._wd_stop.wait(0.1):
            now = time.monotonic()
            with self._lock:
                done = (self._responding and now >= self._play_until + self._tail_s)
            if done:
                self._set_responding(False, "playback-done")
            # drop a stale awaiting-reply window if ElevenLabs never replied
            if self._awaiting_reply and now - self._awaiting_since >= self._awaiting_timeout_s:
                self._awaiting_reply = False
            # stream continuous silence through the awaiting window so turn_v3 gets
            # the trailing silence it needs to endpoint (not while the gate reopened
            # for a new turn, or once the agent is replying)
            if (self._awaiting_reply and self._user_cb is not None
                    and not self._gate_open and not self._responding):
                self._user_cb(self._silence_chunk)
            # hold ElevenLabs' turn open during idle; never while awaiting an endpoint
            if (self._keepalive_cb and not self._gate_open and not self._responding
                    and not self._awaiting_reply
                    and now - self._last_keepalive >= self._keepalive_s):
                self._last_keepalive = now
                try:
                    self._keepalive_cb()
                except Exception:
                    pass


def main() -> int:
    parser = argparse.ArgumentParser(description="SAA-gated ElevenLabs agent")
    parser.add_argument("--session-log", action="store_true",
                        help="write a per-session artifact dir (events.jsonl + saa.log + meta.json)")
    parser.add_argument("--artifact-dir", default=None,
                        help="base dir for session logs (implies --session-log)")
    args = parser.parse_args()

    # opt-in: SAA_SESSION_LOG=1 env, or --session-log / --artifact-dir CLI
    log_enabled = (args.session_log or os.environ.get("SAA_SESSION_LOG") == "1"
                   or args.artifact_dir is not None)
    slog = None
    if log_enabled:
        try:
            slog = SessionLog(args.artifact_dir or "./sessions")
            slog.attach_logging()
            log.info("session log -> %s", slog.dir)
        except Exception as e:
            log.warning("session log disabled: %s", e)
            slog = None

    def tee(name, payload=None):
        # mirror an SDK callback into events.jsonl when the session log is active
        if slog is not None:
            slog.append_event(name, payload)

    saa_api_key = os.environ.get("SAA_API_KEY")
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    agent_id = os.environ.get("ELEVENLABS_AGENT_ID")
    if not (saa_api_key and api_key and agent_id):
        print("set SAA_API_KEY, ELEVENLABS_API_KEY, ELEVENLABS_AGENT_ID")
        if slog is not None:
            slog.finalize("config_error")
        return 2

    # class-2 (device-directed) confidence threshold: higher = stricter gate.
    # Set at startup here; change live anytime with saa.set_threshold(v).
    threshold = float(os.environ.get("SAA_CLASS2_THRESHOLD", "0.7"))

    # keep auto_reconnect at the SDK default (True)
    saa = AttentionClient(token=saa_api_key, enable_audio=False, enable_video=False,
                          initial_threshold=threshold)
    attn = SAAFeedAudioInterface(DefaultAudioInterface(), saa)
    if slog is not None:
        slog.write_meta({"threshold": threshold, "agent_id": agent_id})

    warmed = threading.Event()
    state = {"disconnected": False}  # tracks exit cause for finalize

    @saa.on_warmup_complete
    def _():
        log.info("WARMUP complete — SAA predicting")
        tee("warmup_complete")
        warmed.set()

    @saa.on_config
    def _(ev):
        log.info("CONFIG class2_threshold=%.2f", ev.model_class2_threshold)
        tee("config", ev)

    @saa.on_prediction
    def _(ev):
        attn.update_gate(ev.cls)               # 2 opens, 1 closes now, 0 debounced
        resp, gate = attn.responding, attn.gate_open
        log.info("PRED cls=%d conf=%.2f src=%s | resp=%s gate=%s send=%s",
                 ev.cls, ev.confidence or 0.0, ev.source,
                 resp, "open" if gate else "shut",
                 "real" if (gate and not resp) else "muted")
        if ev.cls == 2 and resp:
            log.info("  ^ cls=2 while agent SPEAKING (echo?) — muted")
        tee("prediction", ev)

    @saa.on_turn_ready
    def _(ev):
        # SAA's authoritative turn boundary — trigger ElevenLabs' endpoint
        log.info("SAA turn_ready dur=%.1fs", ev.duration_sec)
        attn.endpoint()
        tee("turn_ready", {"duration_sec": ev.duration_sec})

    @saa.on_interrupt
    def _(ev):
        log.info("SAA interrupt conf=%.2f", ev.confidence)
        tee("interrupt", ev)

    @saa.on_error
    def _(ev):
        log.warning("SAA error: %s — %s", ev.title, ev.message)
        tee("error", ev)

    @saa.on_disconnected
    def _(ev):
        # distinct, fatal-looking line — separate from the recurring stall noise
        log.error("SAA DISCONNECTED code=%s reason=%s clean=%s",
                  ev.code, ev.reason or "none", ev.was_clean)
        state["disconnected"] = not ev.was_clean
        tee("disconnected", ev)

    @saa.on_reconnecting
    def _(ev):
        log.warning("SAA reconnecting attempt=%d in %.1fs (last_code=%s)",
                    ev.attempt, ev.delay_s, ev.last_code)
        tee("reconnecting", ev)

    @saa.on_reconnected
    def _(ev):
        log.info("SAA reconnected after %d attempt(s)", ev.attempts)
        tee("reconnected", ev)

    conversation = Conversation(
        client=ElevenLabs(api_key=api_key),
        agent_id=agent_id,
        requires_auth=True,
        audio_interface=attn,
        callback_agent_response=lambda r: log.info("AGENT: %s", r),
        callback_user_transcript=lambda t: log.info("USER:  %s", t),
    )

    saa.start()
    attn.prime()
    log.info("warming up SAA model... (~10-15s)")
    if not warmed.wait(timeout=20.0):
        log.warning("SAA warmup did not complete within 20s — greeting anyway")
    # Hold ElevenLabs' turn open during silence/side-talk: its turn_timeout caps at
    # 30s and isn't overridable, so we reset it
    conversation.start_session()
    attn.bind_keepalive(conversation.register_user_activity)
    log.info("session started — Ctrl+C to end")
    exit_cause = "normal"
    try:
        conversation.wait_for_session_end()
    except KeyboardInterrupt:
        pass
    except Exception:
        exit_cause = "exception"
        log.exception("session ended with exception")
        raise
    finally:
        if exit_cause == "normal" and state["disconnected"]:
            exit_cause = "disconnected"
        conversation.end_session()
        saa.stop()
        if slog is not None:
            slog.finalize(exit_cause)
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("elevenlabs").setLevel(logging.WARNING)
    logging.getLogger("saa").setLevel(logging.WARNING)
    raise SystemExit(main())
