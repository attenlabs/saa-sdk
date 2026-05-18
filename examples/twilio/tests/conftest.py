"""pytest config for the SAA × Twilio adapter tests.

Puts the example directory and the SDK source on ``sys.path`` so
``import server`` and ``import saa`` succeed when pytest is invoked
from either the example folder or the repo root. Also stubs the SDK's
native deps (``cv2`` / ``sounddevice``) the same way the LiveKit and
py-package suites do, since this example uses ``feed_audio()`` and
never opens local mic/cam.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MONOREPO = ROOT.parents[1]

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(MONOREPO / "packages" / "saa-py" / "src"))

for missing in ("cv2", "sounddevice"):
    if missing not in sys.modules:
        stub = types.ModuleType(missing)
        if missing == "cv2":
            stub.VideoCapture = object  # type: ignore[attr-defined]
            stub.CAP_PROP_FRAME_WIDTH = 3  # type: ignore[attr-defined]
            stub.CAP_PROP_FRAME_HEIGHT = 4  # type: ignore[attr-defined]
            stub.CAP_PROP_FPS = 5  # type: ignore[attr-defined]
            stub.CAP_PROP_BUFFERSIZE = 38  # type: ignore[attr-defined]
        sys.modules[missing] = stub
