"""E2E fixtures: clients for the coordinator, the fake LLM and the Docker socket; a clean slate
(no swarms, happy-path fake) around every test. Runs inside the `e2e` compose service."""
from __future__ import annotations

import pytest

from tests.e2e.helpers import Api, Docker, Fake, poll


@pytest.fixture(scope="session")
def api() -> Api:
    client = Api()
    poll(lambda: client.get("/health")["status"] == "ok", timeout=120, desc="coordinator health")
    return client


@pytest.fixture(scope="session")
def fake() -> Fake:
    client = Fake()
    poll(lambda: client.http.get("/health").status_code == 200, timeout=60, desc="fake LLM")
    return client


@pytest.fixture(scope="session")
def docker() -> Docker:
    return Docker()


@pytest.fixture(autouse=True)
def clean_slate(api, fake):
    """Every test starts with no swarm and the happy-path fake, and leaves the same behind."""
    fake.reset()
    api.remove_all_swarms()
    yield
    fake.reset()
    api.remove_all_swarms()
