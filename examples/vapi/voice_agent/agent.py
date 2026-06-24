# SAA-gated Vapi WebSocket assistant
#
# SAA owns addressee detection. Vapi receives the user's real mic audio only while
# SAA says it's device-directed; otherwise silence is streamed so Vapi's own VAD
# never sees side talk. Assistant playback drives mark_responding so SAA knows
# when the agent is speaking.
import asyncio
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from contextlib import suppress
from pathlib import Path

import sounddevice as sd
import websockets
from dotenv import load_dotenv

from saa import AttentionClient

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

log = logging.getLogger("vapi-saa")

SAMPLE_RATE = 16000
CHANNELS = 1
BLOCK_SAMPLES = 320          # 20 ms @ 16 kHz
BLOCK_BYTES = BLOCK_SAMPLES * 2
OUTPUT_BYTES_PER_SEC = SAMPLE_RATE * 2
SILENCE_BLOCK = bytes(BLOCK_BYTES)


class SAAGate:
    """Device-directed gate + responding tracker for a downstream audio sink."""

    def __init__(self, saa: AttentionClient, *, close_debounce_ticks: int = 4,
                 responding_tail_ms: int = 300):
        self._saa = saa
        self._close_debounce = close_debounce_ticks
        self._tail_s = responding_tail_ms / 1000.0
        self._gate_open = False
        self._misses = 0
        self._responding = False
        self._play_until = 0.0
        self._sending_real = False
        self._lock = threading.Lock()

    @property
    def responding(self) -> bool:
        return self._responding

    @property
    def gate_open(self) -> bool:
        return self._gate_open

    def update_gate(self, cls: int) -> None:
        if cls == 2:
            self._misses = 0
            self._gate_open = True
        elif cls == 1:
            self._gate_open = False
        elif self._gate_open:
            self._misses += 1
            if self._misses >= self._close_debounce:
                self._gate_open = False

    def chunk_for_vapi(self, pcm16: bytes) -> tuple[bytes, bool]:
        """Return (bytes to forward, whether they are real mic audio)."""
        send_real = self._gate_open and not self._responding
        if send_real != self._sending_real:
            self._sending_real = send_real
            log.info(
                "SEND %s  (gate=%s resp=%s)",
                "real" if send_real else "muted",
                self._gate_open,
                self._responding,
            )
        return (pcm16 if send_real else SILENCE_BLOCK[: len(pcm16)]), send_real

    def on_agent_audio(self, nbytes: int) -> None:
        now = time.monotonic()
        first = False
        with self._lock:
            self._play_until = max(self._play_until, now) + nbytes / OUTPUT_BYTES_PER_SEC
            if not self._responding:
                self._responding = True
                first = True
        if first:
            self._saa.mark_responding(True)
            log.info("RESP -> SPEAKING")

    def poll_responding(self) -> None:
        now = time.monotonic()
        with self._lock:
            done = self._responding and now >= self._play_until + self._tail_s
        if done:
            with self._lock:
                self._responding = False
            self._saa.mark_responding(False)
            log.info("RESP -> idle  (playback-done)")


def create_vapi_websocket_call(api_key: str, assistant_id: str) -> str:
    """POST /call with vapi.websocket transport; return websocketCallUrl."""
    body = json.dumps({
        "assistantId": assistant_id,
        "transport": {
            "provider": "vapi.websocket",
            "audioFormat": {
                "format": "pcm_s16le",
                "container": "raw",
                "sampleRate": SAMPLE_RATE,
            },
        },
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.vapi.ai/call",
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        data=body,
    )
    try:
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Vapi /call failed: HTTP {e.code} {detail or e.reason}") from e
    url = (payload.get("transport") or {}).get("websocketCallUrl")
    if not url:
        raise RuntimeError(f"Vapi /call returned no websocketCallUrl: {payload!r}")
    return url


async def _responding_poll(gate: SAAGate, stop: asyncio.Event) -> None:
    while not stop.is_set():
        gate.poll_responding()
        await asyncio.sleep(0.1)


async def run_vapi_session(ws_url: str, gate: SAAGate, uplink: asyncio.Queue,
                           playback: deque[bytes], stop: asyncio.Event) -> None:
    async with websockets.connect(ws_url) as ws:
        log.info("Vapi WebSocket connected")

        async def sender() -> None:
            while not stop.is_set():
                try:
                    chunk = await asyncio.wait_for(uplink.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                if chunk is None:
                    break
                await ws.send(chunk)

        async def receiver() -> None:
            async for message in ws:
                if isinstance(message, bytes):
                    gate.on_agent_audio(len(message))
                    playback.append(message)
                else:
                    log.info("VAPI control: %s", message)

        await asyncio.gather(sender(), receiver())


def main() -> int:
    saa_api_key = os.environ.get("SAA_API_KEY", "").strip()
    vapi_api_key = os.environ.get("VAPI_API_KEY", "").strip()
    assistant_id = os.environ.get("VAPI_ASSISTANT_ID", "").strip()
    if not (saa_api_key and vapi_api_key and assistant_id):
        print("set SAA_API_KEY, VAPI_API_KEY, VAPI_ASSISTANT_ID")
        return 2

    threshold = float(os.environ.get("SAA_CLASS2_THRESHOLD", "0.7"))
    saa = AttentionClient(
        token=saa_api_key,
        enable_audio=False,
        enable_video=False,
        initial_threshold=threshold,
    )
    gate = SAAGate(saa)

    warmed = threading.Event()

    @saa.on_warmup_complete
    def _on_warmup():
        log.info("WARMUP complete — SAA predicting")
        warmed.set()

    @saa.on_prediction
    def _on_prediction(ev):
        gate.update_gate(ev.cls)
        log.info(
            "PRED cls=%d conf=%.2f src=%s | resp=%s gate=%s send=%s",
            ev.cls,
            ev.confidence or 0.0,
            ev.source,
            gate.responding,
            "open" if gate.gate_open else "shut",
            "real" if (gate.gate_open and not gate.responding) else "muted",
        )

    @saa.on_turn_ready
    def _on_turn_ready(ev):
        log.info("SAA turn_ready dur=%.1fs", ev.duration_sec)

    @saa.on_interrupt
    def _on_interrupt(ev):
        log.info("SAA interrupt conf=%.2f fade_ms=%d", ev.confidence, ev.fade_ms)
        saa.unmute()
        saa.mark_responding(False)

    @saa.on_error
    def _on_error(ev):
        log.warning("SAA error: %s — %s", ev.title, ev.message)

    @saa.on_disconnected
    def _on_disconnected(ev):
        log.error(
            "SAA DISCONNECTED code=%s reason=%s clean=%s",
            ev.code, ev.reason or "none", ev.was_clean,
        )

    saa.start()
    log.info("warming up SAA model... (~10-15s)")

    loop = asyncio.new_event_loop()
    uplink: asyncio.Queue[bytes | None] = asyncio.Queue()
    playback: deque[bytes] = deque()
    stop = asyncio.Event()
    stream_to_vapi = threading.Event()

    def mic_callback(indata, frames, time_info, status):
        if status:
            log.debug("mic status: %s", status)
        pcm = bytes(indata)
        try:
            saa.feed_audio(pcm)
        except Exception:
            log.exception("feed_audio failed")
        if stream_to_vapi.is_set():
            chunk, _ = gate.chunk_for_vapi(pcm)
            loop.call_soon_threadsafe(uplink.put_nowait, chunk)

    def out_callback(outdata, frames, time_info, status):
        del time_info, status
        need = frames * CHANNELS * 2
        buf = bytearray()
        while len(buf) < need and playback:
            buf.extend(playback.popleft())
        if len(buf) < need:
            buf.extend(b"\x00" * (need - len(buf)))
        outdata[:] = buf[:need]

    in_stream = sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="int16",
        blocksize=BLOCK_SAMPLES,
        callback=mic_callback,
    )
    out_stream = sd.RawOutputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="int16",
        blocksize=BLOCK_SAMPLES,
        callback=out_callback,
    )

    async def run() -> None:
        ready = await asyncio.to_thread(warmed.wait, 20.0)
        if not ready:
            log.warning("SAA warmup did not complete within 20s — starting Vapi anyway")
        ws_url = await asyncio.to_thread(create_vapi_websocket_call, vapi_api_key, assistant_id)
        log.info("Vapi call created: %s", ws_url)
        stream_to_vapi.set()
        responding_task = asyncio.create_task(_responding_poll(gate, stop))
        try:
            await run_vapi_session(ws_url, gate, uplink, playback, stop)
        finally:
            stop.set()
            await uplink.put(None)
            responding_task.cancel()
            with suppress(asyncio.CancelledError):
                await responding_task

    exit_code = 0
    try:
        in_stream.start()
        out_stream.start()
        loop.run_until_complete(run())
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("session ended with exception")
        exit_code = 1
    finally:
        stop.set()
        stream_to_vapi.clear()
        in_stream.stop()
        out_stream.stop()
        in_stream.close()
        out_stream.close()
        saa.stop()
        loop.close()
    return exit_code


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("saa").setLevel(logging.WARNING)
    raise SystemExit(main())
