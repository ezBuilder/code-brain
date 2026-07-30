from __future__ import annotations

import shutil
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def remove_runtime_test_tmp_path(tmp_path: Path):
    """Delete each test's temporary repository before the next test starts."""
    yield
    shutil.rmtree(tmp_path, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def remove_runtime_test_basetemp(tmp_path_factory: pytest.TempPathFactory):
    """Runtime tests own their basetemp and must not retain repository copies."""
    basetemp = tmp_path_factory.getbasetemp()
    yield
    shutil.rmtree(basetemp, ignore_errors=True)
