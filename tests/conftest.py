"""Shared pytest fixtures.

Sets fake RECLAIMERR_URL/RECLAIMERR_API_TOKEN/RECLAIMERR_USERNAME/
RECLAIMERR_PASSWORD *before* importing server.py, since the module validates
its configuration at import time (see server._require_env). Individual tests
then swap server.client (the /api/v1 Bearer client) or server.session (the
cookie-authenticated /api client the rules tools use) for one backed by
httpx.MockTransport to control what "Reclaimerr" returns — no extra mocking
library needed, it ships in httpx (already a runtime dependency).
"""

import json
import os

os.environ.setdefault("RECLAIMERR_URL", "http://test-reclaimerr:8000")
os.environ.setdefault("RECLAIMERR_API_TOKEN", "rcl_test_faketoken")
os.environ.setdefault("RECLAIMERR_USERNAME", "test-admin")
os.environ.setdefault("RECLAIMERR_PASSWORD", "test-password")

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


@pytest.fixture(autouse=True)
def _rules_start_disabled(monkeypatch):
    """Every test starts with the rules tools 'not checked', whatever ran before it."""
    monkeypatch.setattr(server, "rules_state", server.RulesState("disabled", "not checked in tests"))


@pytest.fixture
def rules_ready(monkeypatch, with_auth):
    """Rules tools enabled: admin check passed and MCP_AUTH_TOKEN set."""
    monkeypatch.setattr(server, "rules_state", server.RulesState("ready", None, "test-admin"))


class FakeReclaimerr:
    """A stand-in for Reclaimerr's cookie-authenticated /api routes.

    Mimics the behaviours the server depends on: login sets an `access_token`
    cookie, every other route answers 401 without the current cookie, `action`
    is replaced wholesale on update and `auto_delete_enabled` is normalised to a
    bool (as backend/api/routes/rules.py does), and validation failures come back
    as a 422 with a FastAPI-style `detail`.
    """

    def __init__(self):
        self.rules: list[dict] = []
        self.requests: list[tuple[str, str, object]] = []  # (method, path, json body)
        self.logins = 0
        self.token: str | None = None
        self.role = "admin"
        self.login_status = 200
        self.rules_status: int | None = None  # force GET /rules to answer this status
        self.always_unauthorized = False  # session never accepted, even right after login
        self.preview_status = 200
        self.preview_total = 3
        self._next_id = 1

    def add_rule(self, **overrides) -> dict:
        rule = {
            "id": self._next_id,
            "name": "Existing rule",
            "description": None,
            "media_type": "series",
            "enabled": False,
            "target_scope": "episode",
            "definition": {"version": 1, "root": {"type": "group", "op": "and", "children": []}},
            "action": {"outcome": "candidate", "arr_action": "unmonitor", "auto_delete_enabled": False},
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        }
        rule.update(overrides)
        self._next_id = max(self._next_id, rule["id"]) + 1
        self.rules.append(rule)
        return rule

    def expire_session(self):
        """Make the current cookie stale, like Reclaimerr's 24h session expiry."""
        self.token = None

    def calls(self, method: str, path: str) -> list:
        return [body for (m, p, body) in self.requests if (m, p) == (method, path)]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api")
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, path, body))

        if path == "/auth/login":
            self.logins += 1
            if self.login_status != 200:
                return httpx.Response(self.login_status, json={"detail": "nope"})
            self.token = f"session-{self.logins}"
            return httpx.Response(
                200,
                json={"user": {"username": "admin", "role": self.role}},
                headers={"set-cookie": f"access_token={self.token}; Path=/; HttpOnly"},
            )

        cookie = request.headers.get("cookie", "")
        if self.always_unauthorized or self.token is None or f"access_token={self.token}" not in cookie:
            return httpx.Response(401, json={"detail": "Not authenticated"})

        if path == "/rules" and request.method == "GET":
            if self.rules_status:
                return httpx.Response(self.rules_status, json={"detail": "forced"})
            return httpx.Response(200, json=self.rules)
        if path == "/rules/preview":
            if self.preview_status != 200:
                return httpx.Response(
                    self.preview_status,
                    json={"detail": [{"loc": ["body", "definition"], "msg": "unknown field 'nope'"}]},
                )
            return httpx.Response(
                200,
                json={
                    "items": [{"title": "Some Show S01E01"}],
                    "total": self.preview_total,
                    "page": body["page"],
                    "per_page": body["per_page"],
                    "total_pages": 1,
                    "metadata": {"matched_count": self.preview_total},
                },
            )
        if path == "/rules" and request.method == "POST":
            rule = self.add_rule(**{k: v for k, v in body.items()})
            rule["action"]["auto_delete_enabled"] = rule["action"].get("auto_delete_enabled") is True
            return httpx.Response(201, json=rule)

        parts = path.split("/")  # ["", "rules", "<id>"]
        if len(parts) == 3 and parts[1] == "rules" and parts[2].isdigit():
            rule = next((r for r in self.rules if r["id"] == int(parts[2])), None)
            if rule is None:
                return httpx.Response(404, json={"detail": f"Rule with ID {parts[2]} not found"})
            if request.method == "POST":
                rule.update(body)  # replaces `action` wholesale, like the real route
                if "action" in body:
                    rule["action"]["auto_delete_enabled"] = rule["action"].get("auto_delete_enabled") is True
                return httpx.Response(200, json=rule)
            if request.method == "DELETE":
                self.rules.remove(rule)
                return httpx.Response(200, json={"message": f"Deleted cleanup rule: {rule['name']}"})
        return httpx.Response(404, json={"detail": "no such route"})


@pytest.fixture
def fake_reclaimerr(monkeypatch):
    """Point server.session at a FakeReclaimerr with a clean cookie jar; returns the fake."""
    fake = FakeReclaimerr()
    fake_session = httpx.Client(
        base_url=f"{server.RECLAIMERR_URL}/api",
        transport=httpx.MockTransport(fake.handler),
    )
    monkeypatch.setattr(server, "session", fake_session)
    return fake
