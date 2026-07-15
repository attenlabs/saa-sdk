"""Test bootstrap: run the suite against the in-repo src/ without an install.

The package uses a src-layout and isn't pip-installed in CI, so put src/ on the
import path here. capture.py does a top-level `import cv2`; the OpenCV/audio
capture backends aren't needed for pure logic tests, so satisfy that import with
a lightweight stub (no runtime dependency added).
"""

import os
import sys
import types

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC not in sys.path:
    sys.path.insert(0, os.path.abspath(_SRC))

sys.modules.setdefault("cv2", types.ModuleType("cv2"))
