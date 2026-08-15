"""Integration tests: real seat CLIs, real councils. Skipped unless
RUN_INTEGRATION=1 is set in the environment (A4)."""

import os

import pytest


def pytest_collection_modifyitems(config, items):
    if os.environ.get("RUN_INTEGRATION") == "1":
        return
    skip = pytest.mark.skip(reason="integration tests need RUN_INTEGRATION=1")
    for item in items:
        if "integration" not in str(item.fspath):
            continue
        # Modules that run entirely on fake CLI bins (tests/fixtures/fake_bin)
        # opt out of the skip — they need no real CLIs.
        if getattr(item.module, "RUNS_WITHOUT_INTEGRATION", False):
            continue
        item.add_marker(skip)
