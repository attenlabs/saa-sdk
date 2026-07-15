import asyncio
from unittest.mock import MagicMock

import pytest

from saa_livekit_client import build_attention_entrypoint


async def _noop(ev, ctx):
    pass


def test_returns_callable():
    assert callable(build_attention_entrypoint(on_turn=_noop))


def test_missing_env_raises(monkeypatch):
    for k in ("SAA_API_KEY", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "LIVEKIT_URL"):
        monkeypatch.delenv(k, raising=False)
    entrypoint = build_attention_entrypoint(on_turn=_noop)
    with pytest.raises(RuntimeError, match="missing required value"):
        asyncio.run(entrypoint(MagicMock()))
