"""
Reclaimerr MCP Server

Exposes a small set of Reclaimerr (automated media-library cleanup tool)
operations as MCP tools, so an MCP-compatible AI assistant (e.g. Claude Code /
Claude Desktop) can see what's queued for deletion, protect or postpone items,
inspect protections, and trigger/observe Reclaimerr's background tasks via its
REST API.

Configuration is via environment variables:
  RECLAIMERR_URL          e.g. http://192.168.1.50:8000 (required)
  RECLAIMERR_API_TOKEN    Reclaimerr > Settings > Account > API Tokens
                          (required). Must be an "rcl_..." token with at
                          least the candidates:read, protections:read, and
                          system:read scopes; add candidates:manage,
                          tasks:read, and tasks:run for the write tools below.
  MCP_HOST                interface to bind to (default 0.0.0.0)
  MCP_PORT                port to listen on (default 8941)
  MCP_AUTH_TOKEN          shared secret required as `Authorization: Bearer <token>`
                          on every request (optional — if unset, the server is open
                          to anyone who can reach it; see README for why that's a
                          real trade-off, not just a default to ignore)

Unlike the Servarr apps (Sonarr/Radarr/Prowlarr), Reclaimerr's external API is
not version-negotiated — it lives at a fixed `/api/v1` prefix reported by the
app itself (see GET /api/v1's own `api_version` field), so there is no
RECLAIMERR_API_VERSION to configure and no discovery-endpoint drift check
here; /ready instead confirms reachability and validates the token directly
against GET /api/v1/system.

Auth is a scoped Bearer API token (format `rcl_<prefix>_<secret>`), generated
in Reclaimerr's own UI per-token with specific scopes — this is Reclaimerr's
own auth model, not something this server invents. A 403 from Reclaimerr
means the token is valid but missing a required scope; that's surfaced
distinctly from a 401 (bad/revoked token) in tool errors and in /ready.

Transport: streamable-http. This runs as a standing network service (bind
0.0.0.0 inside the container; publish the port only on your internal
network/VLAN — never forward it externally) rather than being spawned
per-client over stdio, so any MCP client on the LAN can connect to
http://<host>:<port>/mcp.

Auth here is a single shared bearer token checked by plain middleware, not
the SDK's built-in OAuth support (mcp.server.auth) — that machinery expects
a full OAuth authorization server (issuer/resource metadata, RFC 8414/8707/
9068 discovery), which is unwarranted complexity for a single internal
secret shared by trusted LAN clients.
"""

import hmac
import os
import sys

import httpx
import uvicorn
from mcp.server.mcpserver import MCPServer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"error: required environment variable {name} is not set", file=sys.stderr)
        sys.exit(1)
    return value


RECLAIMERR_URL = _require_env("RECLAIMERR_URL").rstrip("/")
RECLAIMERR_API_TOKEN = _require_env("RECLAIMERR_API_TOKEN")
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "8941"))
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN")

client = httpx.Client(
    base_url=f"{RECLAIMERR_URL}/api/v1",
    headers={"Authorization": f"Bearer {RECLAIMERR_API_TOKEN}"},
    timeout=30,
)

mcp = MCPServer("reclaimerr")


@mcp.tool()
def list_candidates(media_type: str | None = None, auto_delete_state: str | None = None) -> list[dict]:
    """List reclaim candidates (media Reclaimerr has flagged for possible deletion).

    media_type: optional filter, "movie" or "series".
    auto_delete_state: optional filter, one of "disabled", "scheduled",
    "eligible", "postponed", "canceled".
    Returns up to 200 candidates (Reclaimerr's own page size cap), most
    recently created first.
    """
    params = {"per_page": 200}
    if media_type:
        params["media_type"] = media_type
    if auto_delete_state:
        params["auto_delete_state"] = auto_delete_state

    response = client.get("/candidates", params=params)
    response.raise_for_status()
    body = response.json()
    return [
        {
            "id": c["id"],
            "mediaType": c["media_type"],
            "title": c["title"],
            "year": c.get("year"),
            "scope": c["scope"],
            "reason": c.get("reason"),
            "autoDeleteState": c["auto_delete_state"],
            "autoDeleteEligibleAt": c.get("auto_delete_eligible_at"),
            "blockers": c.get("blockers", []),
        }
        for c in body["items"]
    ]


@mcp.tool()
def candidate_status(candidate_id: int) -> dict:
    """Get full lifecycle detail for a single reclaim candidate by its Reclaimerr ID."""
    response = client.get(f"/candidates/{candidate_id}")
    response.raise_for_status()
    return response.json()


@mcp.tool()
def protect_candidate(candidate_id: int, reason: str | None = None) -> dict:
    """Permanently protect a reclaim candidate from deletion (requires candidates:manage scope)."""
    response = client.post(f"/candidates/{candidate_id}/protect", json={"reason": reason})
    response.raise_for_status()
    return response.json()


@mcp.tool()
def postpone_candidate(candidate_id: int, until: str, reason: str | None = None) -> dict:
    """Postpone a candidate's scheduled deletion until a later ISO-8601 timestamp
    (must be after its current deletion deadline; requires candidates:manage scope)."""
    response = client.post(
        f"/candidates/{candidate_id}/postpone", json={"until": until, "reason": reason}
    )
    response.raise_for_status()
    return response.json()


@mcp.tool()
def cancel_candidate_deletion(candidate_id: int, reason: str | None = None) -> dict:
    """Cancel a pending scheduled deletion for a candidate (requires candidates:manage scope)."""
    response = client.post(f"/candidates/{candidate_id}/cancel", json={"reason": reason})
    response.raise_for_status()
    return response.json()


@mcp.tool()
def list_protections(media_type: str | None = None, active_only: bool = True) -> list[dict]:
    """List protected media (items excluded from reclaim), optionally filtered by
    media_type ("movie" or "series"). Returns up to 200 protections."""
    params = {"per_page": 200, "active_only": active_only}
    if media_type:
        params["media_type"] = media_type

    response = client.get("/protections", params=params)
    response.raise_for_status()
    return response.json()["items"]


@mcp.tool()
def list_tasks() -> list[dict]:
    """List Reclaimerr's background tasks (media sync, candidate scan, cleanup, ...)
    with their schedule, last/next run time, and current status."""
    response = client.get("/tasks")
    response.raise_for_status()
    return response.json()["items"]


@mcp.tool()
def run_task(task_id: str) -> dict:
    """Trigger an immediate run of a background task by its ID (e.g.
    "scan_cleanup_candidates"; see list_tasks for valid IDs). Requires tasks:run scope."""
    response = client.post(f"/tasks/{task_id}/run")
    response.raise_for_status()
    return response.json()


@mcp.tool()
def system_status() -> dict:
    """Get Reclaimerr system status: version, capabilities, and last sync/scan times."""
    response = client.get("/system")
    response.raise_for_status()
    return response.json()


# Paths that must stay reachable without MCP_AUTH_TOKEN, so Docker's own
# HEALTHCHECK, Dockhand's health probe, etc. don't need the secret.
UNAUTHENTICATED_PATHS = {"/health", "/ready"}


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> Response:
    """Liveness check: the process is up and serving HTTP. Does not call Reclaimerr."""
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/ready", methods=["GET"])
async def ready(request: Request) -> Response:
    """Readiness check: RECLAIMERR_URL is reachable and RECLAIMERR_API_TOKEN is
    valid with (at least) the system:read scope."""
    try:
        response = client.get("/system", timeout=5)
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        status_code = error.response.status_code
        if status_code == 401:
            reason = "invalid or revoked Reclaimerr API token"
        elif status_code == 403:
            reason = "Reclaimerr API token is missing the system:read scope"
        else:
            reason = f"Reclaimerr returned HTTP {status_code}"
        return JSONResponse(
            {"status": "error", "reachable": True, "authenticated": status_code != 401, "error": reason},
            status_code=503,
        )
    except httpx.RequestError as error:
        return JSONResponse(
            {
                "status": "error",
                "reachable": False,
                "authenticated": False,
                "error": f"cannot reach Reclaimerr at {RECLAIMERR_URL}: {error}",
            },
            status_code=503,
        )

    body = response.json()
    return JSONResponse(
        {
            "status": "ok",
            "reachable": True,
            "authenticated": True,
            "reclaimerr": {
                "url": RECLAIMERR_URL,
                "version": body.get("version"),
                "apiVersion": body.get("api_version"),
                "capabilities": body.get("capabilities", []),
            },
        }
    )


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Require `Authorization: Bearer <MCP_AUTH_TOKEN>` on every request except
    the health/readiness endpoints, which are meant to be publicly pollable."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path in UNAUTHENTICATED_PATHS:
            return await call_next(request)

        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token, MCP_AUTH_TOKEN):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def build_app():
    """Build the ASGI app (routes + auth middleware). Split out from __main__ so
    tests can exercise the real, fully-wired app without going through uvicorn."""
    app = mcp.streamable_http_app(host=MCP_HOST)

    if MCP_AUTH_TOKEN:
        app.add_middleware(BearerTokenMiddleware)
        print("Auth enabled: Authorization: Bearer <token> required", file=sys.stderr)
    else:
        print("WARNING: MCP_AUTH_TOKEN not set — server is open to anyone who can reach it", file=sys.stderr)

    return app


if __name__ == "__main__":
    uvicorn.run(build_app(), host=MCP_HOST, port=MCP_PORT)
