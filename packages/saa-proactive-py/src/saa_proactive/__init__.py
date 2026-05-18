"""attenlabs-saa-proactive — tiny lifecycle helper for proactive voice agents.

The public surface is two helpers plus their supporting types:

* :class:`ProactiveLifecycle` (with the :func:`run_proactive_turn`
  convenience): the ``mark_responding(True)`` → speak →
  ``mark_responding(False)`` loop, including a configurable tail-ms
  to absorb trailing TTS chunks and try/finally semantics so the
  gate is always released.
* :class:`TriggerHub`: in-process pub/sub for proactive-turn events.
  Used by framework overlays to relay ``POST /trigger`` HTTP webhooks
  to one or more connected browsers via Server-Sent Events. Each
  delivered event is a :class:`TriggerEvent` record.

Apache-2.0. See ``../LICENSE`` and ``../NOTICE``.
"""
from __future__ import annotations

from .lifecycle import ProactiveLifecycle, run_proactive_turn
from .trigger_server import TriggerHub, TriggerEvent

__all__ = [
    "ProactiveLifecycle",
    "run_proactive_turn",
    "TriggerHub",
    "TriggerEvent",
    "__version__",
]

__version__ = "0.1.0"
