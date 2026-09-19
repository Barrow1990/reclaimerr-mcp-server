"""Unit tests for each MCP tool's logic, against a mocked Reclaimerr.

`@mcp.tool()` returns the original function unchanged, so these call the
tools directly as plain Python functions — no MCP protocol/session machinery
involved here (that's covered separately in test_http.py).
"""

import json

import httpx
import pytest

import server

SAMPLE_CANDIDATE = {
    "id": 1,
    "media_type": "movie",
    "scope": "movie",
    "media_id": 1,
    "title": "Chernobyl",
    "year": 2019,
    "tmdb_id": 360893,
    "matched_rule_ids": [4],
    "reason": "Unwatched for 180 days",
    "delete_operation": "delete",
    "created_at": "2026-01-01T00:00:00Z",
    "auto_delete_state": "scheduled",
    "auto_delete_delay_days": 30,
    "auto_delete_eligible_at": "2026-02-01T00:00:00Z",
    "auto_delete_is_active": True,
    "auto_delete_is_eligible": False,
    "blockers": [],
}


def test_list_candidates_returns_shaped_records(mock_reclaimerr):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/candidates"
        assert request.url.params["per_page"] == "200"
        return httpx.Response(
            200, json={"items": [SAMPLE_CANDIDATE], "total": 1, "page": 1, "per_page": 200, "total_pages": 1}
        )

    mock_reclaimerr(handler)

    result = server.list_candidates()

    assert result == [
        {
            "id": 1,
            "mediaType": "movie",
            "title": "Chernobyl",
            "year": 2019,
            "scope": "movie",
            "reason": "Unwatched for 180 days",
            "autoDeleteState": "scheduled",
            "autoDeleteEligibleAt": "2026-02-01T00:00:00Z",
            "blockers": [],
        }
    ]


def test_list_candidates_applies_filters(mock_reclaimerr):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["media_type"] == "series"
        assert request.url.params["auto_delete_state"] == "eligible"
        return httpx.Response(200, json={"items": [], "total": 0, "page": 1, "per_page": 200, "total_pages": 0})

    mock_reclaimerr(handler)

    assert server.list_candidates(media_type="series", auto_delete_state="eligible") == []


def test_list_candidates_propagates_http_errors(mock_reclaimerr):
    mock_reclaimerr(lambda req: httpx.Response(500, json={"detail": "boom"}))

    with pytest.raises(httpx.HTTPStatusError):
        server.list_candidates()


def test_candidate_status_hits_correct_path(mock_reclaimerr):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/candidates/42"
        return httpx.Response(200, json=SAMPLE_CANDIDATE)

    mock_reclaimerr(handler)

    assert server.candidate_status(42) == SAMPLE_CANDIDATE


def test_protect_candidate_posts_reason(mock_reclaimerr):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/candidates/1/protect"
        assert json.loads(request.content) == {"reason": "keeping this one"}
        return httpx.Response(
            200, json={"candidate": SAMPLE_CANDIDATE, "event_id": "evt_1", "protection_id": 9, "replayed": False}
        )

    mock_reclaimerr(handler)

    result = server.protect_candidate(1, reason="keeping this one")

    assert result["protection_id"] == 9


def test_postpone_candidate_sends_until_and_reason(mock_reclaimerr):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/candidates/1/postpone"
        body = json.loads(request.content)
        assert body == {"until": "2026-03-01T00:00:00Z", "reason": None}
        return httpx.Response(
            200, json={"candidate": SAMPLE_CANDIDATE, "event_id": "evt_2", "protection_id": None, "replayed": False}
        )

    mock_reclaimerr(handler)

    server.postpone_candidate(1, until="2026-03-01T00:00:00Z")


def test_cancel_candidate_deletion_posts_correct_path(mock_reclaimerr):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/candidates/1/cancel"
        return httpx.Response(
            200, json={"candidate": SAMPLE_CANDIDATE, "event_id": "evt_3", "protection_id": None, "replayed": False}
        )

    mock_reclaimerr(handler)

    result = server.cancel_candidate_deletion(1)

    assert result["event_id"] == "evt_3"


def test_list_protections_hits_correct_path_and_params(mock_reclaimerr):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/protections"
        assert request.url.params["active_only"] == "true"
        return httpx.Response(
            200,
            json={
                "items": [{"id": 1, "media_type": "movie", "title": "Chernobyl", "permanent": True, "active": True}],
                "total": 1,
                "page": 1,
                "per_page": 200,
                "total_pages": 1,
            },
        )

    mock_reclaimerr(handler)

    result = server.list_protections()

    assert result[0]["title"] == "Chernobyl"


def test_list_tasks_hits_correct_path(mock_reclaimerr):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/tasks"
        return httpx.Response(
            200,
            json={
                "items": [{"id": "scan_cleanup_candidates", "name": "Scan Cleanup Candidates", "enabled": True}],
                "has_main_server": True,
            },
        )

    mock_reclaimerr(handler)

    result = server.list_tasks()

    assert result[0]["id"] == "scan_cleanup_candidates"


def test_run_task_posts_correct_path(mock_reclaimerr):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/tasks/scan_cleanup_candidates/run"
        return httpx.Response(
            200, json={"task_id": "scan_cleanup_candidates", "job_id": 5, "queued": True, "already_active": False}
        )

    mock_reclaimerr(handler)

    result = server.run_task("scan_cleanup_candidates")

    assert result["queued"] is True


def test_system_status_hits_correct_path(mock_reclaimerr):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/system"
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "program": "reclaimerr",
                "version": "0.4.7",
                "project_url": "https://jessielw.github.io/Reclaimerr/",
                "api_version": "v1",
                "server_time": "2026-01-01T00:00:00Z",
                "has_main_media_server": True,
                "capabilities": ["candidate-lifecycle"],
            },
        )

    mock_reclaimerr(handler)

    result = server.system_status()

    assert result["version"] == "0.4.7"
