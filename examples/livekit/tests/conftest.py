"""pytest config: put the SDK source and the example dir on sys.path, and
stub the SDK's native deps so the suite runs in CI without PortAudio /
OpenCV installed.

Why stubs are safe:
  - ``capture.py`` does ``import cv2`` at module load, but only *uses* it
    inside ``CameraCapture.start()``. The tests never start a CameraCapture.
  - ``capture.py`` lazy-imports ``sounddevice`` inside ``MicCapture.start()``,
    so no stub is needed for it; we add one defensively for resilience.

This matches the pattern used by ``packages/saa-py/tests``.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]      # examples/livekit/
MONOREPO = ROOT.parents[1]                       # repo root

# Local example modules (saa_gate.py, agent.py).
sys.path.insert(0, str(ROOT))
# attenlabs-saa SDK source (the wheel mirrors this layout).
sys.path.insert(0, str(MONOREPO / "packages" / "saa-py" / "src"))

# Stub native deps that the SDK only uses for live mic / camera capture
# our tests inject a fake AttentionClient and never actuate them.
for missing in ("cv2", "sounddevice"):
    if missing not in sys.modules:
        stub = types.ModuleType(missing)
        # Provide a couple of attributes that ``capture.py`` references at
        # module level so import-time attribute lookups don't crash.
        if missing == "cv2":
            stub.VideoCapture = object  # type: ignore[attr-defined]
            stub.CAP_PROP_FRAME_WIDTH = 3  # type: ignore[attr-defined]
            stub.CAP_PROP_FRAME_HEIGHT = 4  # type: ignore[attr-defined]
            stub.CAP_PROP_FPS = 5  # type: ignore[attr-defined]
            stub.CAP_PROP_BUFFERSIZE = 38  # type: ignore[attr-defined]
        sys.modules[missing] = stub
