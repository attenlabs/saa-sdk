# minimal dev token endpoint for the SAA + LiveKit web demo — NOT for production
# mints a browser join token and summons the hidden SAA agent for the room
# the SAA API key stays server-side; the browser never sees it
import os
from datetime import timedelta

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from livekit.api import AccessToken, VideoGrants

from saa_livekit_client import attention_agent_token, start_attention_session

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/token")
async def token(room: str, identity: str) -> dict:
    # browser join token — publish cam+mic, subscribe to others
    user_jwt = (
        AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
        .with_identity(identity)
        .with_grants(
            VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True)
        )
        .with_ttl(timedelta(hours=1))
        .to_jwt()
    )

    # summon the hidden SAA agent for this room
    handle = await start_attention_session(
        api_key=os.environ["SAA_API_KEY"],
        livekit_url=os.environ["LIVEKIT_URL"],
        agent_token=attention_agent_token(
            api_key=os.environ["LIVEKIT_API_KEY"],
            api_secret=os.environ["LIVEKIT_API_SECRET"],
            room_name=room,
        ),
        room_name=room,
        participant_identity=identity,
    )

    return {
        "url": os.environ["LIVEKIT_URL"],
        "token": user_jwt,
        "agent_identity": handle.agent_identity,
        "session_id": handle.session_id,
    }


# serve index.html + app.js + styles.css from the same origin
# (declared after /token so the route resolves first)
app.mount("/", StaticFiles(directory=os.path.dirname(__file__), html=True), name="static")
