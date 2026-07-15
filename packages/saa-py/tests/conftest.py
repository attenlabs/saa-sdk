import pathlib
import sys
import types

# Make the src-layout package importable without an editable install.
SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# capture.py imports cv2 at module load. The camera path is fully monkeypatched
# in these tests, so when the native OpenCV dependency is absent a light stub
# satisfies the import without pulling it in. When cv2 is installed (e.g. CI),
# the real module loads instead.
try:  # pragma: no cover - environment dependent
    import cv2  # noqa: F401
except Exception:  # pragma: no cover - environment dependent
    _cv2 = types.ModuleType("cv2")
    _cv2.VideoCapture = object
    _cv2.CAP_PROP_FRAME_WIDTH = 3
    _cv2.CAP_PROP_FRAME_HEIGHT = 4
    _cv2.CAP_PROP_FPS = 5
    _cv2.CAP_PROP_BUFFERSIZE = 38
    _cv2.IMWRITE_JPEG_QUALITY = 1
    _cv2.imencode = lambda *a, **k: (False, None)
    sys.modules["cv2"] = _cv2
