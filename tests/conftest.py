import os
import tempfile

import pytest

os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("POS_PATH", "/nonexistent.csv")
os.environ.setdefault("START_WATCHER", "0")

from app.storage import Store  # noqa: E402


@pytest.fixture
def store():
    return Store(":memory:")
