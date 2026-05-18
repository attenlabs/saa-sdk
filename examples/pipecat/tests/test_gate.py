"""Compatibility shim, the SAA × Pipecat behavioural tests now live in
``tests/test_saa_gate.py`` (renamed to match the public adapter shape).

This file used to host the gate behavioural tests directly; it kept the
old ``SAAGate`` constructor signature (no ``upstream_mode``, no
``feed_audio`` on the fake client). The new file is a strict superset
upstream-mode contract, lifecycle, passthrough, threshold, error /
prediction / state events.

Kept as a no-op so any existing CI invocations of ``pytest test_gate.py``
still exit 0 while the new file does the real work.
"""
