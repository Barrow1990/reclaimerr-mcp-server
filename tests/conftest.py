"""Shared pytest fixtures.

Sets a fake RECLAIMERR_URL/RECLAIMERR_API_TOKEN *before* importing server.py,
since the module requires both at import time (see server._require_env).
Individual tests then swap server.client's transport to control what
"Reclaimerr" returns, via httpx.MockTransport — no extra mocking library
needed, it ships in httpx (already a runtime dependency).
"""

import os

os.environ.setdefault("RECLAIMERR_URL", "http://test-reclaimerr:8000")
os.environ.setdefault("RECLAIMERR_API_TOKEN", "rcl_test_faketoken")

import httpx  # noqa: E402
import pytest  # noqa: E402

import server  # noqa: E402


@pytest.fixture
def mock_reclaimerr(monkeypatch):
    """Point server.client at a fake Reclaimerr.

    Usage:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[...])
        mock_reclaimerr(handler)
    """

    def _install(handler):
        fake_client = httpx.Client(
            base_url=f"{server.RECLAIMERR_URL}/api/v1",
            headers={"Authorization": f"Bearer {server.RECLAIMERR_API_TOKEN}"},
            transport=httpx.MockTransport(handler),
        )
        monkeypatch.setattr(server, "client", fake_client)
        return fake_client

    return _install


@pytest.fixture
def no_auth(monkeypatch):
    """Run with MCP_AUTH_TOKEN unset (the default, open-server mode)."""
    monkeypatch.setattr(server, "MCP_AUTH_TOKEN", None)


@pytest.fixture
def with_auth(monkeypatch):
    """Run with a known MCP_AUTH_TOKEN, and return it."""
    token = "test-shared-secret"
    monkeypatch.setattr(server, "MCP_AUTH_TOKEN", token)
    return token
