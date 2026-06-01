"""REST client for the saa hosted bridge.

Wraps `POST /v1/sessions/daily` (and the GET / DELETE companions) into a
single `start_attention_session(...)` call returning a `SessionHandle`.

The consumer only needs to call this once per Daily session;
the actual saa bot joins their room asynchronously and starts
publishing events on the `"saa"` app-message topic shortly after.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx


logger = logging.getLogger("saa_pipecat_client.api")

DEFAULT_API_BASE = "https://broker.attentionlabs.ai"


class AttentionAPIError(RuntimeError):
    """Raised when the hosted bridge rejects the request or returns a non-2xx
    response. `.status_code` exposes the HTTP code; `.body` carries the
    server's JSON error payload (or raw text if non-JSON).
    """

    def __init__(self, status_code: int, body: Any, message: str | None = None):
        super().__init__(message or f"hosted bridge error: {status_code} {body!r}")
        self.status_code = status_code
        self.body = body


@dataclass
class SessionHandle:
    """Reference to an active hosted-bridge session.

    `agent_identity` is the Daily `user_name` the hosted bot joined under —
    use it when constructing `AttentionEngine` and when sending upstream
    actions so they're scoped to the right participant.

    `stop()` is idempotent: calling it twice (e.g. via an explicit cleanup
    AND a shutdown hook) won't raise on the second call. The underlying
    httpx client is closed exactly once.
    """

    session_id: str
    agent_identity: str
    _api_base: str
    _api_key: str
    _client: httpx.AsyncClient
    _closed: bool = field(default=False, repr=False)

    async def stop(self) -> None:
        """Signal the hosted bot to disconnect and tear down the session."""
        if self._closed:
            return
        self._closed = True
        try:
            resp = await self._client.delete(
                f"{self._api_base}/v1/sessions/{self.session_id}",
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            if resp.status_code >= 400 and resp.status_code != 404:
                # 404 = already gone, treat as success
                raise AttentionAPIError(resp.status_code, _safe_json(resp))
        finally:
            await self._client.aclose()

    async def status(self) -> dict[str, Any]:
        """Fetch current session status (uptime, last_prediction, error_count)."""
        if self._closed:
            raise AttentionAPIError(
                0, None, "SessionHandle.status() called after stop()",
            )
        resp = await self._client.get(
            f"{self._api_base}/v1/sessions/{self.session_id}",
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        if resp.status_code >= 400:
            raise AttentionAPIError(resp.status_code, _safe_json(resp))
        return resp.json()


async def start_attention_session(
    *,
    api_key: str,
    room_url: str,
    agent_token: str,
    participant_identity: str,
    attention_config: dict[str, Any] | None = None,
    api_base: str = DEFAULT_API_BASE,
    timeout: float = 30.0,
) -> SessionHandle:
    """Summon the saa hosted bot into the customer's Daily room.

    The call returns once the hosted bridge accepts the request (HTTP 200);
    the hidden bot joins the room asynchronously shortly after. Wait on
    `AttentionEngine.is_ready` (or the `"started"` event) before relying
    on inference output.

    Args:
        api_key:              SAA_API_KEY — customer's saa API key.
        room_url:             Customer's Daily room URL (e.g.
                              https://their-org.daily.co/sess-xyz). Must be
                              publicly reachable from our cloud.
        agent_token:          Hidden-participant Daily meeting token, issued
                              via `attention_agent_token(...)`.
        participant_identity: `user_name` of the human user whose tracks the
                              bot should analyze.
        attention_config:     Optional config overrides — vetted subset only.
                              See docs/daily-integration.md for the public
                              field list. Unknown fields are silently ignored.
        api_base:             Override the API base URL (testing / private envs).
        timeout:              HTTP timeout for the POST call.
    """
    api_base = api_base.rstrip("/")

    body: dict[str, Any] = {
        "room_url": room_url,
        "agent_token": agent_token,
        "participant_identity": participant_identity,
    }
    if attention_config:
        body["attention_config"] = attention_config

    # Keep the httpx client open for the lifetime of the handle so
    # subsequent .stop() / .status() calls reuse the same connection pool.
    client = httpx.AsyncClient(timeout=timeout)
    try:
        resp = await client.post(
            f"{api_base}/v1/sessions/daily",
            json=body,
            headers={"Authorization": f"Bearer {api_key}"},
        )
    except Exception:
        await client.aclose()
        raise

    if resp.status_code >= 400:
        body_text = _safe_json(resp)
        await client.aclose()
        raise AttentionAPIError(resp.status_code, body_text)

    data = resp.json()
    logger.info(
        "started attention session %s (agent_identity=%s)",
        data.get("session_id"), data.get("agent_identity"),
    )
    return SessionHandle(
        session_id=data["session_id"],
        agent_identity=data["agent_identity"],
        _api_base=api_base,
        _api_key=api_key,
        _client=client,
    )


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return resp.text
