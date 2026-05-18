"""pytest configuration for the SAA × Pipecat adapter tests.

Makes ``import saa_gate`` work regardless of how pytest is invoked, and
registers ``pytest-asyncio`` in auto mode so test files don't have to.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

EXAMPLE_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLE_DIR))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if "asyncio" not in item.keywords and item.get_closest_marker("asyncio") is None:
            if _is_coroutine_test(item):
                item.add_marker(pytest.mark.asyncio)


def _is_coroutine_test(item: pytest.Item) -> bool:
    import asyncio

    func = getattr(item, "function", None)
    return func is not None and asyncio.iscoroutinefunction(func)
