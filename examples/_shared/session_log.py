"""Dependency-free per-session artifact logger shared across the SAA examples.

Writes a timestamped session_<UTC>/ dir holding events.jsonl (one JSON line per
SDK callback), saa.log (tee of the saa + root loggers), and meta.json. Designed
to be opt-in and exception-safe: a logging failure must never crash the example.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _jsonable(value: Any) -> Any:
    # dataclasses -> dict; numpy arrays and other oddballs -> repr/str fallback
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        return repr(value)
    except Exception:
        return str(value)


class SessionLog:
    """Per-session artifact directory: events.jsonl + saa.log + meta.json."""

    def __init__(self, base_dir: str | Path = "./sessions"):
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.dir = Path(base_dir) / f"session_{ts}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.dir / "events.jsonl"
        self.meta_path = self.dir / "meta.json"
        self._start_monotonic = time.monotonic()
        self._handler: logging.Handler | None = None
        self._attached: list[logging.Logger] = []
        # SAA callbacks fire on the WS-receive AND heartbeat threads — serialize writes
        self._lock = threading.Lock()
        self.write_meta({"started": datetime.now(timezone.utc).isoformat()})

    def append_event(self, name: str, payload: Any = None) -> None:
        # one JSON line per event; never raises
        try:
            row = {
                "ts": round(time.monotonic() - self._start_monotonic, 4),
                "wallclock": datetime.now(timezone.utc).isoformat(),
                "event": name,
                "payload": _jsonable(payload),
            }
            line = json.dumps(row) + "\n"
            with self._lock, self.events_path.open("a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    def attach_logging(self) -> None:
        # tee the saa logger and root logger into saa.log
        try:
            handler = logging.FileHandler(self.dir / "saa.log", encoding="utf-8")
            handler.setFormatter(logging.Formatter(
                "%(asctime)s.%(msecs)03d %(name)s %(levelname)s %(message)s",
                datefmt="%H:%M:%S",
            ))
            for logger in (logging.getLogger("saa"), logging.getLogger()):
                logger.addHandler(handler)
                self._attached.append(logger)
            self._handler = handler
        except Exception:
            pass

    def write_meta(self, meta: dict) -> None:
        try:
            with self._lock:
                existing: dict = {}
                if self.meta_path.exists():
                    existing = json.loads(self.meta_path.read_text(encoding="utf-8"))
                existing.update(_jsonable(meta))
                self.meta_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        except Exception:
            pass

    def finalize(self, exit_cause: str) -> None:
        # record end time + cause, then detach the file handler; never raises
        try:
            self.write_meta({
                "ended": datetime.now(timezone.utc).isoformat(),
                "duration_s": round(time.monotonic() - self._start_monotonic, 2),
                "exit_cause": exit_cause,
            })
        except Exception:
            pass
        try:
            if self._handler is not None:
                for logger in self._attached:
                    logger.removeHandler(self._handler)
                self._handler.close()
        except Exception:
            pass
        finally:
            self._handler = None
            self._attached = []
