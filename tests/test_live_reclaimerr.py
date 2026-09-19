"""Live contract tests against a *real* Reclaimerr instance.

These do not run by default — there's no Reclaimerr in CI, and we don't want
to accidentally fire real requests using the fake RECLAIMERR_URL/RECLAIMERR_API_TOKEN
that conftest.py sets for the rest of the suite. To run them:

    RUN_LIVE_RECLAIMERR_TESTS=1 RECLAIMERR_URL=https://reclaimerr.example.com \\
    RECLAIMERR_API_TOKEN=<real rcl_... token> pytest tests/test_live_reclaimerr.py -v

Point is to catch drift: if a Reclaimerr upgrade renames/removes a field our
tools depend on (id, title, auto_delete_state, capabilities, ...), these fail
even though the mocked unit tests in test_tools.py would still happily pass
(they only assert against fixtures we wrote ourselves).
"""

import os

import httpx
import pytest

import server

RUN_LIVE = os.environ.get("RUN_LIVE_RECLAIMERR_TESTS") == "1"
pytestmark = pytest.mark.skipif(
    not RUN_LIVE,
    reason="opt-in only: set RUN_LIVE_RECLAIMERR_TESTS=1 with a real RECLAIMERR_URL/RECLAIMERR_API_TOKEN",
)


@pytest.fixture(scope="module")
def live_client():
    return httpx.Client(
        base_url=f"{server.RECLAIMERR_URL}/api/v1",
        headers={"Authorization": f"Bearer {server.RECLAIMERR_API_TOKEN}"},
        timeout=15,
    )


def test_system_shape(live_client):
    """The fields system_status()/`/ready` depend on actually exist."""
    response = live_client.get("/system")
    response.raise_for_status()
    data = response.json()

    assert isinstance(data.get("version"), str)
    assert data.get("api_version") == "v1"
    assert "capabilities" in data


def test_candidate_shape_matches_what_list_candidates_assumes(live_client):
    """Every field list_candidates() reads exists on a real candidate record."""
    response = live_client.get("/candidates", params={"per_page": 5})
    response.raise_for_status()
    items = response.json()["items"]

    if not items:
        pytest.skip("no reclaim candidates right now — nothing to validate the shape of")

    sample = items[0]
    for required_field in ("id", "media_type", "title", "scope", "auto_delete_state"):
        assert required_field in sample, f"Reclaimerr's /candidates no longer returns '{required_field}'"


def test_our_tools_run_cleanly_against_real_reclaimerr(monkeypatch, live_client):
    """Run the actual tool functions (not just raw requests) against real Reclaimerr."""
    monkeypatch.setattr(server, "client", live_client)

    status = server.system_status()
    assert "version" in status

    candidates = server.list_candidates()
    assert isinstance(candidates, list)

    protections = server.list_protections()
    assert isinstance(protections, list)

    tasks = server.list_tasks()
    assert isinstance(tasks, list)
