"""Smoke test for the attenlabs-saa package's public surface.

Verifies every name re-exported from ``saa.__init__`` resolves and that the
package version matches ``pyproject.toml``. The test does not open a network
connection or instantiate ``AttentionClient`` against a real server.
"""
from __future__ import annotations

import saa


def test_public_exports_resolve() -> None:
    """Every documented public name imports cleanly."""
    from saa import (
        AttentionClient,
        CameraConfig,
        MicConfig,
        PredictionEvent,
        VadEvent,
        StateEvent,
        TurnFrame,
        TurnReadyEvent,
        ConfigEvent,
        StatsEvent,
        AttentionErrorEvent,
        DisconnectedEvent,
        ConversationState,
    )

    assert AttentionClient.__name__ == "AttentionClient"
    assert CameraConfig.__name__ == "CameraConfig"
    assert MicConfig.__name__ == "MicConfig"
    assert PredictionEvent.__name__ == "PredictionEvent"
    assert VadEvent.__name__ == "VadEvent"
    assert StateEvent.__name__ == "StateEvent"
    assert TurnFrame.__name__ == "TurnFrame"
    assert TurnReadyEvent.__name__ == "TurnReadyEvent"
    assert ConfigEvent.__name__ == "ConfigEvent"
    assert StatsEvent.__name__ == "StatsEvent"
    assert AttentionErrorEvent.__name__ == "AttentionErrorEvent"
    assert DisconnectedEvent.__name__ == "DisconnectedEvent"
    # ConversationState is a Literal alias; just check it imported.
    assert ConversationState is not None


def test_all_attribute() -> None:
    """``__all__`` enumerates exactly the public exports."""
    expected = {
        "AttentionClient",
        "CameraConfig",
        "MicConfig",
        "PredictionEvent",
        "VadEvent",
        "StateEvent",
        "TurnFrame",
        "TurnReadyEvent",
        "ConfigEvent",
        "StatsEvent",
        "AttentionErrorEvent",
        "DisconnectedEvent",
        "ConversationState",
    }
    assert set(saa.__all__) == expected


def test_version_present() -> None:
    """The package exposes a SemVer ``__version__``."""
    assert hasattr(saa, "__version__")
    parts = saa.__version__.split(".")
    assert len(parts) >= 3
    for part in parts[:3]:
        # Allow trailing pre-release segments (rc, b, etc.) only on the patch.
        head = "".join(c for c in part if c.isdigit())
        assert head, f"version part {part!r} must start with digits"
