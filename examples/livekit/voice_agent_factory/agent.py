# SAA-gated LiveKit agent via the build_attention_entrypoint greenfield factory
import logging
from pathlib import Path

from dotenv import load_dotenv
from livekit.agents import AgentServer, cli

from saa_livekit_client import build_attention_entrypoint

load_dotenv(Path(__file__).resolve().parents[1] / ".env")  # shared examples/livekit/.env

log = logging.getLogger("voice-agent-factory")
logging.basicConfig(level=logging.INFO)


async def handle_turn(ev, ctx):
    # one device-directed utterance; forward ev.audio_pcm16 to your STT/LLM/TTS
    log.info("turn ready: %.2fs context=%s", ev.duration, ev.context)


server = AgentServer()
server.rtc_session()(build_attention_entrypoint(on_turn=handle_turn))


if __name__ == "__main__":
    cli.run_app(server)
