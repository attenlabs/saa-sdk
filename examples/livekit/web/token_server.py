# minimal dev token endpoint for the SAA + LiveKit web demo — NOT for production
# mints a browser LiveKit join token and serves the static files
#
# SAA is summoned by the voice-agent worker in the room, NOT here — run one of
# examples/livekit/voice_agent_cascaded or voice_agent_realtime in the same
# LiveKit project alongside this demo. The agent owns the SAA session and
# answers you; this server just lets the browser join the room.
import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from livekit.api import AccessToken, VideoGrants

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

REQUIRED_ENV = ["LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "LIVEKIT_URL"]


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise HTTPException(503, f"server misconfigured: {name} not set")
    return val


@app.on_event("startup")
async def _check_env() -> None:
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        print(f"[token_server] missing env: {', '.join(missing)} — /token will 503")


@app.get("/token")
async def token(room: str, identity: str) -> dict:
    # browser join token — publish cam+mic, subscribe to the agent's audio
    user_jwt = (
        AccessToken(_require("LIVEKIT_API_KEY"), _require("LIVEKIT_API_SECRET"))
        .with_identity(identity)
        .with_grants(
            VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True)
        )
        .with_ttl(timedelta(hours=1))
        .to_jwt()
    )
    return {"url": _require("LIVEKIT_URL"), "token": user_jwt}


# serve index.html + app.js + styles.css from the same origin
# (declared after /token so the route resolves first)
app.mount("/", StaticFiles(directory=os.path.dirname(__file__), html=True), name="static")
