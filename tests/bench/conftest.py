"""Network safety net shared by every test module under tests/bench/.

The bench replaces every external API client the pipeline touches with an
in-repo double (see mocks.py) so the suite runs offline in a few seconds
instead of minutes of real, sometimes paid, calls. That protection relies on
each stage's client import being patched by name in the `bench` fixture in
test_pipeline_bench.py - a new stage, or a new client call inside an
existing one, is an easy thing to forget to patch (this is exactly what
happened in #155: an unpatched GitHubClient in repo_people sent the bench to
the real api.github.com for 80 repositories and hung CI for over an hour).
This conftest is the backstop: whatever forgets to get mocked, no bench test
can complete a real network call.
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
