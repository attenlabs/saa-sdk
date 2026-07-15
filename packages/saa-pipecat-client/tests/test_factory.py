import asyncio
from unittest.mock import MagicMock

import pytest

from saa_pipecat_client import build_attention_runner


async def _noop(ev, transport):
    pass


def test_returns_callable():
    assert callable(build_attention_runner(on_turn=_noop))


def test_missing_env_raises(monkeypatch):
    for k in ("SAA_API_KEY", "DAILY_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    run = build_attention_runner(on_turn=_noop)
    with pytest.raises(RuntimeError, match="missing required value"):
        asyncio.run(run("u", "r", "h", MagicMock(), MagicMock()))
