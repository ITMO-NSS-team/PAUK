"""Network safety net shared by every test module under tests/bench/.

The bench patches every external API client by name in the `bench`
fixture of test_pipeline_bench.py. A client nobody patched (a new stage,
or a new call inside an existing one) would reach the real network, so
this conftest is the backstop: whatever is forgotten, no bench test can
complete a real network call.
"""

from __future__ import annotations

from unittest import mock

import pytest
import requests

from tests.bench.mocks import NetworkAccessDenied


def _refuse(*args, **kwargs):
    raise NetworkAccessDenied(
        "tests/bench tried to reach the network - patch the client that "
        "made this call in the `bench` fixture (test_pipeline_bench.py) "
        "instead of letting it fall through to requests"
    )


@pytest.fixture(autouse=True, scope="session")
def _block_network():
    with mock.patch.object(requests.Session, "send", _refuse):
        yield
