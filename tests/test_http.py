"""Tests for the wired-together ASGI app: /health, /ready, and the bearer-auth
middleware — via server.build_app(), the same function __main__ uses."""

import httpx
from starlette.testclient import TestClient

import server


def test_health_does_not_call_reclaimerr(mock_reclaimerr, no_auth):
    def handler(request):
        raise AssertionError("/health must not call Reclaimerr")

    mock_reclaimerr(handler)

    with TestClient(server.build_app()) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_success(mock_reclaimerr, no_auth):
    mock_reclaimerr(
        lambda req: httpx.Response(
            200,
            json={
                "status": "ok",
                "program": "reclaimerr",
                "version": "0.4.7",
                "api_version": "v1",
                "capabilities": ["candidate-lifecycle"],
            },
        )
    )

    with TestClient(server.build_app()) as client:
        response = client.get("/ready")

    body = response.json()
    assert response.status_code == 200
    assert body == {
        "status": "ok",
        "reachable": True,
        "authenticated": True,
        "reclaimerr": {
            "url": server.RECLAIMERR_URL,
            "version": "0.4.7",
            "apiVersion": "v1",
            "capabilities": ["candidate-lifecycle"],
        },
        # A rules problem never makes /ready unhealthy; it is only reported.
        "rules": {"enabled": False, "status": "disabled", "reason": "not checked in tests", "account": None},
    }


def test_ready_invalid_token(mock_reclaimerr, no_auth):
    mock_reclaimerr(lambda req: httpx.Response(401, json={"detail": "Invalid API token"}))

    with TestClient(server.build_app()) as client:
        response = client.get("/ready")

    body = response.json()
    assert response.status_code == 503
    assert body["reachable"] is True
    assert body["authenticated"] is False
    assert "invalid or revoked" in body["error"]


def test_ready_missing_scope(mock_reclaimerr, no_auth):
    mock_reclaimerr(lambda req: httpx.Response(403, json={"detail": "API token requires the system:read scope"}))

    with TestClient(server.build_app()) as client:
        response = client.get("/ready")

    body = response.json()
    assert response.status_code == 503
    assert body["reachable"] is True
    assert body["authenticated"] is True
    assert "system:read scope" in body["error"]


def test_ready_unreachable_host(mock_reclaimerr, no_auth):
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    mock_reclaimerr(handler)

    with TestClient(server.build_app()) as client:
        response = client.get("/ready")

    body = response.json()
    assert response.status_code == 503
    assert body["reachable"] is False
    assert body["authenticated"] is False


def test_ready_other_reclaimerr_error(mock_reclaimerr, no_auth):
    mock_reclaimerr(lambda req: httpx.Response(500, json={"detail": "boom"}))

    with TestClient(server.build_app()) as client:
        response = client.get("/ready")

    body = response.json()
    assert response.status_code == 503
    assert body["reachable"] is True
    assert body["authenticated"] is True
    assert "HTTP 500" in body["error"]


def test_no_auth_token_leaves_mcp_open(mock_reclaimerr, no_auth):
    mock_reclaimerr(lambda req: httpx.Response(200, json={}))

    with TestClient(server.build_app()) as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            headers={"Accept": "application/json, text/event-stream"},
        )

    # No 401 — auth is off. The exact protocol response doesn't matter here,
    # only that the request wasn't rejected by the auth layer.
    assert response.status_code != 401


def test_auth_token_blocks_mcp_without_header(mock_reclaimerr, with_auth):
    mock_reclaimerr(lambda req: httpx.Response(200, json={}))

    with TestClient(server.build_app()) as client:
        response = client.get("/mcp", headers={"Accept": "application/json, text/event-stream"})

    assert response.status_code == 401


def test_auth_token_blocks_mcp_with_wrong_token(mock_reclaimerr, with_auth):
    mock_reclaimerr(lambda req: httpx.Response(200, json={}))

    with TestClient(server.build_app()) as client:
        response = client.get(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer wrong-token",
            },
        )

    assert response.status_code == 401


def test_auth_token_allows_mcp_with_correct_token(mock_reclaimerr, with_auth):
    mock_reclaimerr(lambda req: httpx.Response(200, json={}))
    token = with_auth

    with TestClient(server.build_app()) as client:
        response = client.get(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {token}",
            },
        )

    # Past auth — the normal MCP protocol response for a bare GET with no
    # prior session (400 missing-session), not a 401.
    assert response.status_code != 401


def test_auth_token_does_not_block_health_or_ready(mock_reclaimerr, with_auth):
    mock_reclaimerr(lambda req: httpx.Response(200, json={"status": "ok", "version": "0.4.7"}))

    with TestClient(server.build_app()) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 200


def test_ready_reports_rules_state_without_affecting_health(mock_reclaimerr, no_auth, monkeypatch):
    mock_reclaimerr(lambda req: httpx.Response(200, json={"version": "0.4.7", "api_version": "v1"}))
    monkeypatch.setattr(
        server, "rules_state", server.RulesState("disabled", "account 'x' has role 'user'", "x")
    )

    with TestClient(server.build_app()) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["rules"]["reason"] == "account 'x' has role 'user'"


def test_ready_includes_rules_state_on_failure_too(mock_reclaimerr, no_auth):
    mock_reclaimerr(lambda req: httpx.Response(401, json={"detail": "Invalid API token"}))

    with TestClient(server.build_app()) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["rules"]["status"] == "disabled"


def test_ready_rules_only_mode_ok_when_rules_ready(monkeypatch, rules_ready):
    monkeypatch.setattr(server, "RECLAIMERR_API_TOKEN", None)

    with TestClient(server.build_app()) as client:
        response = client.get("/ready")

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["rules"]["enabled"] is True


def test_ready_rules_only_mode_unhealthy_when_rules_disabled(monkeypatch, with_auth):
    monkeypatch.setattr(server, "RECLAIMERR_API_TOKEN", None)
    monkeypatch.setattr(server, "rules_state", server.RulesState("disabled", "wrong password"))

    with TestClient(server.build_app()) as client:
        response = client.get("/ready")

    body = response.json()
    assert response.status_code == 503
    assert body["error"] == "wrong password"
    assert body["reachable"] is True


def test_ready_rules_only_mode_reports_unreachable_while_pending(monkeypatch, with_auth):
    monkeypatch.setattr(server, "RECLAIMERR_API_TOKEN", None)
    monkeypatch.setattr(server, "rules_state", server.RulesState("pending", "cannot reach Reclaimerr"))

    with TestClient(server.build_app()) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["reachable"] is False
