# All-in-one dev /session endpoint for the SAA + Stream Video demo — NOT for production.
# Mints a Stream Video user JWT and returns Stream credentials + call ID to the browser.
# The browser client then joins the Stream call directly (no server-side media agent).
import logging
import os
import uuid
from pathlib import Path

from getstream import Stream
from getstream.models import CallRequest, UserRequest
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

load_dotenv(Path(__file__).resolve().parent / ".env")

logger = logging.getLogger("token-server")
logging.basicConfig(level=logging.INFO)

_VOICE_AGENT_KEYS = ("OPENAI_API_KEY",)


def _voice_agent_enabled() -> tuple[bool, list[str]]:
    missing = [k for k in _VOICE_AGENT_KEYS if not os.environ.get(k)]
    return (not missing, missing)


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise HTTPException(503, f"server misconfigured: {name} not set")
    return val


app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _log_mode() -> None:
    enabled, missing = _voice_agent_enabled()
    if enabled:
        logger.info("Voice agent mode ENABLED (OpenAI Realtime in browser)")
    else:
        logger.warning(
            "Voice agent mode DISABLED — missing %s; overlay-only mode",
            ", ".join(missing),
        )

    for key in ("STREAM_API_KEY", "STREAM_API_SECRET"):
        if not os.environ.get(key):
            logger.warning("%s is not set; /session will return 503.", key)

    if not os.environ.get("SAA_API_KEY"):
        logger.warning(
            "SAA_API_KEY is not set — users must paste their SAA token manually in the UI."
        )


@app.get("/config")
async def config() -> dict:
    """Lightweight probe — no side effects. Tells the UI what is server-configured."""
    return {
        "saaConfigured":    bool(os.environ.get("SAA_API_KEY")),
        "openaiConfigured": bool(os.environ.get("OPENAI_API_KEY")),
    }


@app.get("/session")
async def session(room: str = Query(default=None)) -> dict:
    api_key    = _require("STREAM_API_KEY")
    api_secret = _require("STREAM_API_SECRET")

    user_id = f"saa-{uuid.uuid4().hex[:8]}"
    # Use caller-supplied room ID so multiple participants can share one call;
    # generate a fresh ID when no room is given (first person in).
    call_id = room or f"saa-demo-{uuid.uuid4().hex[:8]}"

    # The getstream SDK mints the correct JWT: {"user_id", "iat", "exp"} — no iss/sub.
    client = Stream(api_key=api_key, api_secret=api_secret, timeout=5.0)

    # Upsert user so Stream recognises them before the call is created
    try:
        client.upsert_users(UserRequest(id=user_id, name=user_id, role="user"))
    except Exception as exc:
        logger.warning("upsert_users failed (non-fatal): %s", exc)

    # SDK-generated token has the correct payload Stream Video expects
    user_token = client.create_token(user_id)

    # Create call server-side so it appears in the Stream dashboard immediately
    try:
        call = client.video.call("default", call_id)
        call.get_or_create(data=CallRequest(created_by_id=user_id))
        logger.info("Stream call created: default/%s (user=%s)", call_id, user_id)
    except Exception as exc:
        logger.warning("call.get_or_create failed (non-fatal): %s", exc)

    enabled, missing = _voice_agent_enabled()

    return {
        "callId":               call_id,
        "callType":             "default",
        "userId":               user_id,
        "userToken":            user_token,
        "streamApiKey":         api_key,
        "saaToken":             os.environ.get("SAA_API_KEY", ""),
        "openaiApiKey":         os.environ.get("OPENAI_API_KEY", ""),
        "voiceAgentEnabled":    enabled,
        "voiceAgentMissingEnv": missing,
    }


# Mounted after /session so the route resolves first
app.mount(
    "/",
    StaticFiles(directory=os.path.dirname(__file__), html=True),
    name="static",
)
