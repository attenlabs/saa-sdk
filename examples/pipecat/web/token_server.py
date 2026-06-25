# All-in-one dev /session endpoint for the SAA + Daily web demo — NOT for production
import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from saa_pipecat_client import attention_agent_token, start_attention_session

load_dotenv(Path(__file__).resolve().parent / ".env")

logger = logging.getLogger("token-server")
logging.basicConfig(level=logging.INFO)

DAILY_API = "https://api.daily.co/v1"

VOICE_AGENT_PROVIDER_KEYS = ("OPENAI_API_KEY",)

def _voice_agent_enabled() -> tuple[bool, list[str]]:
    missing = [k for k in VOICE_AGENT_PROVIDER_KEYS if not os.environ.get(k)]
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


_agent_tasks: dict[str, asyncio.Task] = {}


@app.on_event("startup")
async def _log_mode() -> None:
    enabled, missing = _voice_agent_enabled()
    if enabled:
        logger.info("Voice agent ENABLED (OpenAI Realtime)")
    else:
        logger.warning(
            "Voice agent DISABLED — missing %s; overlay-only mode",
            ", ".join(missing),
        )

    if not os.environ.get("SAA_API_KEY"):
        logger.warning("SAA_API_KEY is not set; /session will return 503.")

    key = os.environ.get("DAILY_API_KEY") or ""
    if not key:
        logger.warning("DAILY_API_KEY is not set; /session will return 503.")
    elif key.startswith("eyJ"):
        logger.warning(
            "DAILY_API_KEY looks like a JWT (starts with 'eyJ') — that's a "
            "Daily MEETING TOKEN, not a REST API key. /session will 401 from "
            "Daily. Get the REST key at https://dashboard.daily.co -> Developers."
        )
    elif key.startswith("pk_") or key.startswith("sk_"):
        logger.warning(
            "DAILY_API_KEY starts with 'pk_'/'sk_' — that's a Pipecat Cloud "
            "token, NOT a Daily REST key. /session will 401. The Daily REST key "
            "lives at https://dashboard.daily.co -> Developers (or, if you "
            "signed up via Pipecat Cloud, at https://pipecat.daily.co -> "
            "Settings -> Daily (WebRTC) tab)."
        )
    else:
        logger.info(
            "DAILY_API_KEY fingerprint: %s (will be sent as 'Authorization: "
            "Bearer ...' to api.daily.co)", _daily_key_fingerprint(),
        )

@app.on_event("shutdown")
async def _shutdown_agents() -> None:
    tasks = list(_agent_tasks.values())
    for t in tasks:
        t.cancel()
    for t in tasks:
        try:
            await asyncio.wait_for(t, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass
    _agent_tasks.clear()


def _daily_headers() -> dict[str, str]:
    key = os.environ.get("DAILY_API_KEY")
    if not key:
        raise HTTPException(500, "DAILY_API_KEY not set")
    return {"Authorization": f"Bearer {key}"}


def _daily_key_fingerprint() -> str:
    key = os.environ.get("DAILY_API_KEY") or ""
    if len(key) <= 8:
        return f"(len={len(key)})"
    return f"{key[:4]}…{key[-4:]} (len={len(key)})"


def _daily_credential_hint() -> str:
    key = os.environ.get("DAILY_API_KEY") or ""
    if not key:
        return "DAILY_API_KEY is empty."
    if key.startswith("eyJ"):
        return (
            "Your DAILY_API_KEY looks like a JWT (starts with 'eyJ'). That's a "
            "Daily MEETING TOKEN, not a Daily REST API key."
        )
    if key.startswith("pk_") or key.startswith("sk_"):
        return (
            "Your DAILY_API_KEY starts with 'pk_'/'sk_' — that's a Pipecat Cloud "
            "token (for api.pipecat.daily.co), NOT a Daily REST key."
        )
    return (
        "Daily REST returned authentication-error for the key at "
        f"{_daily_key_fingerprint()}."
    )


async def _create_daily_room() -> dict:
    name = f"saa-demo-{int(time.time())}"
    exp = int(time.time()) + 3600
    async with httpx.AsyncClient(timeout=20.0) as http:
        r = await http.post(
            f"{DAILY_API}/rooms",
            headers=_daily_headers(),
            json={
                "name": name,
                "properties": {
                    "exp": exp,
                },
            },
        )
    if r.status_code >= 300:
        raise HTTPException(r.status_code, _daily_error_detail("rooms create", r))
    body = r.json()
    return {"name": body["name"], "url": body["url"]}


def _daily_error_detail(action: str, resp: httpx.Response) -> str:
    body = resp.text
    if resp.status_code in (400, 401):
        return (
            f"daily {action} failed ({resp.status_code}). "
            f"Daily said: {body}\n\n"
            f"{_daily_credential_hint()}\n\n"
            f"Key fingerprint (first4…last4): {_daily_key_fingerprint()}. "
            f"Compare it against the value shown at "
            f"https://dashboard.daily.co -> Developers."
        )
    return f"daily {action} failed ({resp.status_code}): {body}"


async def _mint_meeting_token(
    room_name: str, user_name: str, *, is_owner: bool = False,
) -> str:
    async with httpx.AsyncClient(timeout=20.0) as http:
        r = await http.post(
            f"{DAILY_API}/meeting-tokens",
            headers=_daily_headers(),
            json={
                "properties": {
                    "room_name": room_name,
                    "user_name": user_name,
                    "is_owner": is_owner,
                    "exp": int(time.time()) + 3600,
                }
            },
        )
    if r.status_code >= 300:
        raise HTTPException(r.status_code, _daily_error_detail("token mint", r))
    return r.json()["token"]


async def _spawn_voice_agent(
    *, room_url: str, bot_token: str, saa_agent_identity: str, room_name: str,
) -> None:
    # lazy import so the overlay-only path works without pipecat-ai installed
    try:
        from voice_agent import run_voice_agent
    except Exception:
        logger.exception(
            "Failed to import voice_agent module — overlay-only for room=%s",
            room_url,
        )
        return

    async def _wrap() -> None:
        try:
            await run_voice_agent(
                room_url=room_url,
                bot_token=bot_token,
                saa_agent_identity=saa_agent_identity,
                openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("voice agent crashed for room=%s", room_url)
        finally:
            _agent_tasks.pop(room_name, None)

    task = asyncio.create_task(_wrap(), name=f"voice-agent-{room_name}")
    _agent_tasks[room_name] = task
    logger.info("voice agent spawned for room=%s (task=%s)", room_url, task.get_name())


@app.get("/session")
async def session(room: Optional[str] = None) -> dict:
    if room:
        room_name = room
        domain = os.environ.get("DAILY_DOMAIN")
        if not domain:
            raise HTTPException(400, "DAILY_DOMAIN must be set to use ?room=")
        room_url = f"https://{domain}.daily.co/{room_name}"
    else:
        info = await _create_daily_room()
        room_name = info["name"]
        room_url = info["url"]

    # SAA bot matches the human via participant["info"]["userName"], which
    # is the meeting token's user_name field — reuse the same string for both
    human_identity = f"user-{int(time.time())}"
    user_token = await _mint_meeting_token(room_name, human_identity)

    saa_agent_token = attention_agent_token(
        daily_api_key=_require("DAILY_API_KEY"),
        room_name=room_name,
    )

    handle = await start_attention_session(
        api_key=_require("SAA_API_KEY"),
        room_url=room_url,
        agent_token=saa_agent_token,
        participant_identity=human_identity,
    )

    enabled, missing = _voice_agent_enabled()
    if enabled:
        bot_token = await _mint_meeting_token(
            room_name, "SAA Voice Agent", is_owner=True,
        )
        await _spawn_voice_agent(
            room_url=room_url,
            bot_token=bot_token,
            saa_agent_identity=handle.agent_identity,
            room_name=room_name,
        )

    return {
        "room_url": room_url,
        "user_token": user_token,
        "user_name": human_identity,
        "agent_identity": handle.agent_identity,
        "session_id": handle.session_id,
        "voice_agent_enabled": enabled,
        "voice_agent_missing_env": missing,
    }


# mounted after /session so the route resolves first
app.mount("/", StaticFiles(directory=os.path.dirname(__file__), html=True), name="static")
