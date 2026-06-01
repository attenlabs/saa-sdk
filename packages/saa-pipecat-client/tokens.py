"""Helper for issuing the hidden-participant Daily meeting token the saa
hosted bot uses to join a Daily room.

The customer's backend calls `attention_agent_token(...)` with THEIR Daily
API key and hands the resulting JWT to `start_attention_session(...)`.
We never see the customer's Daily API key — only the minimum-grant token
they issued specifically for our bot.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx


logger = logging.getLogger("saa_pipecat_client.tokens")

DEFAULT_TTL_HOURS = 2.0
DEFAULT_AGENT_IDENTITY = "saa-agent"

_MEETING_TOKENS_URL = "https://api.daily.co/v1/meeting-tokens"


class DailyTokenError(RuntimeError):
    """Raised when the Daily REST API rejects the token-mint request."""

    def __init__(self, status_code: int, body: Any, message: str | None = None):
        super().__init__(message or f"daily token mint failed: {status_code} {body!r}")
        self.status_code = status_code
        self.body = body


def attention_agent_token(
    *,
    daily_api_key: str,
    room_name: str,
    identity: str = DEFAULT_AGENT_IDENTITY,
    ttl_hours: float = DEFAULT_TTL_HOURS,
) -> str:
    """Issue a Daily meeting token for saa's hidden bot.

    Permissions are minimum-necessary: `hasPresence=False` (invisible in the
    participant list), `canSend=False` (the bot never publishes media),
    `canReceive={audio:True, video:True}` (it needs both modalities).
    `user_id` is fixed to `identity` so concurrent sessions for the same
    customer get distinct bots.

    Args:
        daily_api_key: Customer's Daily REST API key (bearer credential).
        room_name:     Room the bot should join. Token is scoped to this room.
        identity:      `user_name` for the bot. Surfaced as
                       `session_handle.agent_identity` in the POST response so
                       the customer's voice agent can route upstream actions.
        ttl_hours:     Token validity window. Default 2 h.

    Returns:
        The JWT meeting-token string to hand to `start_attention_session(...)`.
    """
    if not daily_api_key:
        raise ValueError("daily_api_key is required")
    if not room_name:
        raise ValueError("room_name is required")

    exp = _now_unix() + int(ttl_hours * 3600)

    body: dict[str, Any] = {
        "properties": {
            "room_name": room_name,
            "user_name": identity,
            "user_id": identity,
            "exp": exp,
            # Verified permission recipe (Daily REST API schema 2026-05):
            # hasPresence=False hides the bot from the participants list,
            # canSend=False blocks media publishing, canReceive lets the
            # bot subscribe to both modalities.
            "permissions": {
                "hasPresence": False,
                "canSend": False,
                "canReceive": {
                    "audio": True,
                    "video": True,
                },
            },
        },
    }

    resp = httpx.post(
        _MEETING_TOKENS_URL,
        json=body,
        headers={
            "Authorization": f"Bearer {daily_api_key}",
            "Content-Type": "application/json",
        },
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise DailyTokenError(resp.status_code, _safe_json(resp))

    data = resp.json()
    token = data.get("token")
    if not token:
        raise DailyTokenError(
            resp.status_code, data, "daily token response missing 'token' field",
        )
    return token


def _now_unix() -> int:
    import time
    return int(time.time())


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return resp.text
