"""LiveKit JWT mint helper for browser clients.

A tiny FastAPI server that mints short-lived participant tokens so a
browser or mobile client can join the room your agent worker is
listening to.

Deliberately small (~70 lines). Production deployments should:
  - put this behind your existing auth (Clerk, Auth0, custom session)
  - rate-limit per IP / per user
  - never run with ``LIVEKIT_API_SECRET`` exposed to untrusted callers

Install extras::

    pip install -e ".[serve]"

Run::

    python serve_token.py            # http://localhost:8088

Client request::

    POST /api/token
    { "room": "saa-demo", "identity": "user-abc", "name": "Ada" }

    → { "token": "<jwt>", "url": "wss://...", "room": "saa-demo" }

To wire LiveKit's playground at <https://agents-playground.livekit.io>,
use the *Custom* tab and supply LIVEKIT_URL + the minted token.

The module is importable without the [serve] extras installed; the
FastAPI/livekit-api imports are deferred to :func:`build_app` so test
harnesses and shape-check tools can ``import serve_token`` cheaply.
"""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()

DEFAULT_TOKEN_TTL_S = int(os.environ.get("TOKEN_TTL_S", "3600"))


def build_app() -> Any:
    """Construct the FastAPI application. Requires the ``[serve]`` extras."""
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.middleware.cors import CORSMiddleware
        from livekit import api as lk_api
        from pydantic import BaseModel, Field
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "serve_token.py requires the [serve] extras: "
            'pip install -e ".[serve]". Underlying error: '
            f"{exc}"
        ) from exc

    api_key = os.environ.get("LIVEKIT_API_KEY")
    api_secret = os.environ.get("LIVEKIT_API_SECRET")
    livekit_url = os.environ.get("LIVEKIT_URL")
    if not (api_key and api_secret and livekit_url):
        raise SystemExit(
            "Set LIVEKIT_URL, LIVEKIT_API_KEY, and LIVEKIT_API_SECRET in .env."
        )

    class TokenRequest(BaseModel):
        room: str = Field(..., min_length=1, max_length=100, description="Room name to join")
        identity: str = Field(..., min_length=1, max_length=200, description="Stable participant id")
        name: Optional[str] = Field(default=None, max_length=200, description="Display name")
        metadata: Optional[str] = Field(default=None, description="Opaque participant metadata")

    class TokenResponse(BaseModel):
        token: str
        url: str
        room: str
        identity: str

    app = FastAPI(title="saa-livekit-agent token server")

    # CORS allow-list. Default is a small set of localhost origins so the
    # dev flow (Vite, Next.js, the LiveKit Agents playground served on
    # localhost) works out of the box without exposing the token-mint to
    # arbitrary origins. Production deployments MUST set TOKEN_ALLOWED_ORIGINS
    # to the exact set of trusted origins (and front the endpoint with their
    # own auth, per the module docstring).
    default_origins = "http://localhost:3000,http://localhost:5173,http://localhost:8080"
    app.add_middleware(
        CORSMiddleware,
        allow_origins=os.environ.get("TOKEN_ALLOWED_ORIGINS", default_origins).split(","),
        allow_methods=["POST", "GET", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.get("/healthz")
    async def health() -> dict:
        return {"ok": True}

    @app.post("/api/token", response_model=TokenResponse)
    async def issue_token(req: TokenRequest) -> TokenResponse:
        try:
            grant = lk_api.VideoGrants(
                room_join=True,
                room=req.room,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
            token = (
                lk_api.AccessToken(api_key, api_secret)
                .with_identity(req.identity)
                .with_name(req.name or req.identity)
                .with_metadata(req.metadata or "")
                .with_grants(grant)
                .with_ttl(timedelta(seconds=DEFAULT_TOKEN_TTL_S))
                .to_jwt()
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"token mint failed: {exc}") from exc

        return TokenResponse(
            token=token,
            url=livekit_url,
            room=req.room,
            identity=req.identity,
        )

    return app


def main() -> None:
    """Run the token server with uvicorn."""
    import uvicorn

    host = os.environ.get("TOKEN_HOST", "0.0.0.0")
    port = int(os.environ.get("TOKEN_PORT", "8088"))
    uvicorn.run(build_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
