"""Integration tests run only against a *_test database loaded with eval/fixtures (see load_fixture)."""

import json
import os

import pytest

from app.evals.paths import FIXTURE_META

DB = os.environ.get("POSTGRES_DB", "")
META = json.loads(FIXTURE_META.read_text(encoding="utf-8")) if FIXTURE_META.exists() else None

if DB.endswith("_test") and META:
    os.environ.setdefault("FIXED_NOW", META["fixed_now"])  # before app.api is imported by the tests


def pytest_collection_modifyitems(config, items):
    if DB.endswith("_test") and META:
        return
    skip = pytest.mark.skip(reason="set POSTGRES_DB=<name>_test and load the fixture to run integration tests")
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def meta():
    return META


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient

    from app.api import app

    with TestClient(app) as c:
        yield c
