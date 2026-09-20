"""
Reclaimerr MCP Server

Exposes Reclaimerr (automated media-library cleanup tool) operations as MCP
tools, so an MCP-compatible AI assistant (e.g. Claude Code / Claude Desktop)
can see what's queued for deletion, protect or postpone items, inspect
protections, trigger/observe background tasks, and — with an admin login —
manage cleanup rules.

There are two independent groups of tools, each enabled by its own credentials:

  General tools   RECLAIMERR_API_TOKEN
                  Reclaimerr's external API at the fixed `/api/v1` prefix,
                  authenticated with a scoped Bearer API token (format
                  `rcl_<prefix>_<secret>`, created in Reclaimerr's own UI with
                  specific scopes). A 403 means the token is valid but missing
                  a required scope; that's surfaced distinctly from a 401
                  (bad/revoked token) in tool errors and in /ready.

  Rules tools     RECLAIMERR_USERNAME + RECLAIMERR_PASSWORD  (and MCP_AUTH_TOKEN)
                  Rules have no `/api/v1` equivalent, so these use the web UI's
                  own `/api` routes, which authenticate with a session cookie
                  (`access_token`, ~24h) from `POST /api/auth/login` and require
                  an ADMIN account. The server logs in itself and logs in again
                  when the session expires. These routes are undocumented and
                  may change between Reclaimerr releases.

Configuration is via environment variables:
  RECLAIMERR_URL          e.g. http://192.168.1.50:8000 (required)
  RECLAIMERR_API_TOKEN    "rcl_..." token (Reclaimerr > Settings > Account >
                          API Tokens). Enables the general tools. Needs at least
                          candidates:read, protections:read, and system:read;
                          add candidates:manage, tasks:read, and tasks:run for
                          the write tools.
  RECLAIMERR_USERNAME     Admin username (or email) for the rules tools.
  RECLAIMERR_PASSWORD     Its password. Set both or neither.
  MCP_HOST                interface to bind to (default 0.0.0.0)
  MCP_PORT                port to listen on (default 8941)
  MCP_AUTH_TOKEN          shared secret required as `Authorization: Bearer <token>`
                          on every request. Optional for the general tools (if
                          unset, the server is open to anyone who can reach it;
                          see README for why that's a real trade-off). REQUIRED
                          for the rules tools: they hold an admin login, so they
                          stay disabled without it.
At least one of RECLAIMERR_API_TOKEN or the username/password pair is required.

Tools that aren't available (missing config, wrong password, non-admin account,
Reclaimerr unreachable) are not listed at all; `rules_status` is always listed
and tells the AI exactly what is missing and why. A wrong password or non-admin
account stays disabled until the container is restarted with fixed settings;
Reclaimerr merely being unreachable at startup is retried every few minutes.
Clients only re-read the tool list on (re)connect, so reconnect after a fix.

Rules safety policy (enforced here, not just documented to the AI):
  - create_rule always saves the rule DISABLED with auto-delete OFF, after a
    dry-run. A human reviews and enables it in the Reclaimerr UI.
  - Enabling a rule or turning on auto-delete is not possible through this
    server at all.
  - Updating an enabled rule, and deleting any rule, need explicit human
    approval via an MCP elicitation prompt. If the client can't show the
    prompt the change is refused (fail closed).

Unlike the Servarr apps (Sonarr/Radarr/Prowlarr), Reclaimerr's external API is
not version-negotiated — it lives at a fixed `/api/v1` prefix reported by the
app itself (see GET /api/v1's own `api_version` field), so there is no
RECLAIMERR_API_VERSION to configure and no discovery-endpoint drift check
here; /ready instead confirms reachability and validates the token directly
against GET /api/v1/system.

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
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Annotated, Any

import httpx
import uvicorn
from mcp.server.mcpserver import Elicit, ElicitationResult, MCPServer, Resolve
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field
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
RECLAIMERR_API_TOKEN = os.environ.get("RECLAIMERR_API_TOKEN") or None
RECLAIMERR_USERNAME = os.environ.get("RECLAIMERR_USERNAME") or None
RECLAIMERR_PASSWORD = os.environ.get("RECLAIMERR_PASSWORD") or None
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "8941"))
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN")

if bool(RECLAIMERR_USERNAME) != bool(RECLAIMERR_PASSWORD):
    print(
        "error: RECLAIMERR_USERNAME and RECLAIMERR_PASSWORD must be set together (or both left unset)",
        file=sys.stderr,
    )
    sys.exit(1)
if not RECLAIMERR_API_TOKEN and not RECLAIMERR_USERNAME:
    print(
        "error: nothing to serve — set RECLAIMERR_API_TOKEN (general tools) and/or "
        "RECLAIMERR_USERNAME + RECLAIMERR_PASSWORD (rules tools)",
        file=sys.stderr,
    )
    sys.exit(1)

# Name of the session cookie Reclaimerr's own login sets (backend/core/auth.py).
SESSION_COOKIE = "access_token"
# Reclaimerr rate-limits login to 5/minute, so a retry after an unreachable
# startup waits far longer than that; one check per interval can never trip it.
RULES_RECHECK_SECONDS = 300
# How long startup waits for the first rules check before serving anyway.
STARTUP_CHECK_WAIT_SECONDS = 10

INSTRUCTIONS = (
    "Tools for Reclaimerr, a media-library cleanup app. Call rules_status first if you are unsure "
    "which tools are available: it explains what is enabled and why any group is unavailable "
    "(rules tools need an admin login and MCP_AUTH_TOKEN on the server). Rules you create are always "
    "saved disabled with auto-delete off; the user enables them in the Reclaimerr UI. Changes to enabled "
    "rules and rule deletions ask the user for approval; never try to work around a declined approval."
)

# General tools: Reclaimerr's versioned /api/v1, authenticated by the Bearer token.
client = httpx.Client(
    base_url=f"{RECLAIMERR_URL}/api/v1",
    headers={"Authorization": f"Bearer {RECLAIMERR_API_TOKEN}"} if RECLAIMERR_API_TOKEN else {},
    timeout=30,
)

# Rules tools: the web UI's /api routes, authenticated by the cookie the login
# response sets. httpx keeps that cookie in the client's jar and replays it, so
# there is deliberately no auth header here.
session = httpx.Client(base_url=f"{RECLAIMERR_URL}/api", timeout=30)
_session_lock = threading.Lock()

mcp = MCPServer("reclaimerr", instructions=INSTRUCTIONS)


# ---------------------------------------------------------------------------
# Rules availability: config checks, login, and the startup admin check
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RulesState:
    """Whether the rules tools are usable, and if not, why.

    status: "disabled" (won't change until restart with fixed settings),
            "pending" (Reclaimerr was unreachable; being re-checked), or "ready".
    """

    status: str
    reason: str | None = None
    account: str | None = None


rules_state = RulesState("disabled", "not checked yet")


class LoginError(Exception):
    """Login to Reclaimerr failed. `transient` means retrying later can help."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


def _rules_config_problem() -> str | None:
    """Why the rules tools can't be enabled from configuration alone, or None."""
    if not (RECLAIMERR_USERNAME and RECLAIMERR_PASSWORD):
        return "RECLAIMERR_USERNAME and RECLAIMERR_PASSWORD are not set"
    if not MCP_AUTH_TOKEN:
        return (
            "MCP_AUTH_TOKEN is not set. The rules tools hold an admin login to Reclaimerr, "
            "so they only run when every MCP request must carry the shared secret"
        )
    return None


def _login() -> dict[str, Any]:
    """Log in and leave the session cookie in `session`'s jar. Hold `_session_lock`.

    Returns the logged-in user's info (including `role`). Raises LoginError.
    """
    try:
        response = session.post(
            "/auth/login", json={"username": RECLAIMERR_USERNAME, "password": RECLAIMERR_PASSWORD}
        )
    except httpx.RequestError as error:
        raise LoginError(f"cannot reach Reclaimerr at {RECLAIMERR_URL}: {error}", transient=True) from error

    status_code = response.status_code
    if status_code == 200:
        if not session.cookies.get(SESSION_COOKIE):
            raise LoginError(
                f"login succeeded but no '{SESSION_COOKIE}' cookie was kept: a proxy may be stripping "
                "Set-Cookie, or Reclaimerr issued a Secure-only cookie while RECLAIMERR_URL is plain http "
                "(use its https URL)"
            )
        body = response.json()
        return body.get("user", {}) if isinstance(body, dict) else {}
    if status_code == 401:
        raise LoginError("Reclaimerr rejected the username/password (HTTP 401)")
    if status_code == 403:
        raise LoginError("Reclaimerr says this account is disabled (HTTP 403)")
    if status_code == 422:
        raise LoginError("Reclaimerr rejected the username/password format (HTTP 422)")
    if status_code == 429:
        raise LoginError("Reclaimerr's login rate limit (5/minute) was hit (HTTP 429)", transient=True)
    if status_code >= 500:
        raise LoginError(f"Reclaimerr returned HTTP {status_code} on login", transient=True)
    raise LoginError(f"unexpected HTTP {status_code} from Reclaimerr on login")


def _session_request(method: str, path: str, **kwargs: Any) -> httpx.Response:
    """Send a request with the session cookie, logging in first / again as needed.

    A 401 means the ~24h session expired (or was revoked): log in once more and
    retry once. Never loops, because login itself is rate-limited by Reclaimerr.
    Raises LoginError or httpx.RequestError; callers turn those into messages.
    """
    if not session.cookies.get(SESSION_COOKIE):
        with _session_lock:
            if not session.cookies.get(SESSION_COOKIE):
                _login()

    sent_cookie = session.cookies.get(SESSION_COOKIE)
    response = session.request(method, path, **kwargs)
    if response.status_code != 401:
        return response

    with _session_lock:
        # If another thread already logged in again (or Reclaimerr refreshed the
        # cookie) since we sent this request, don't burn another login.
        if session.cookies.get(SESSION_COOKIE) == sent_cookie:
            session.cookies.clear()
            _login()
    response = session.request(method, path, **kwargs)
    if response.status_code == 401:
        raise LoginError("Reclaimerr still rejects the session right after logging in again")
    return response


def check_rules_access() -> RulesState:
    """Log in and confirm this account can use the rules routes; record the outcome.

    Called once at startup, and again on a cooldown while Reclaimerr is unreachable.
    """
    global rules_state

    problem = _rules_config_problem()
    if problem:
        rules_state = RulesState("disabled", problem)
        return rules_state

    try:
        with _session_lock:
            session.cookies.clear()
            user = _login()
        account = user.get("username") or RECLAIMERR_USERNAME
        role = user.get("role")
        if role != "admin":
            rules_state = RulesState(
                "disabled",
                f"account '{account}' has role '{role}', but the rules tools need an admin account",
                account,
            )
            return rules_state

        response = _session_request("GET", "/rules")
        if response.status_code == 200:
            rules_state = RulesState("ready", None, account)
        elif response.status_code == 403:
            rules_state = RulesState("disabled", f"account '{account}' is not allowed to read rules (HTTP 403)", account)
        elif response.status_code == 404:
            rules_state = RulesState(
                "disabled", "Reclaimerr has no /api/rules route (unsupported Reclaimerr version?)", account
            )
        elif response.status_code >= 500:
            rules_state = RulesState("pending", f"Reclaimerr returned HTTP {response.status_code} for /api/rules", account)
        else:
            rules_state = RulesState("disabled", f"unexpected HTTP {response.status_code} from /api/rules", account)
    except LoginError as error:
        rules_state = RulesState("pending" if error.transient else "disabled", str(error))
    except httpx.RequestError as error:
        rules_state = RulesState("pending", f"cannot reach Reclaimerr at {RECLAIMERR_URL}: {error}")
    return rules_state


def _rules_report() -> dict[str, Any]:
    state = rules_state
    return {
        "enabled": state.status == "ready",
        "status": state.status,
        "reason": state.reason,
        "account": state.account,
    }


# ---------------------------------------------------------------------------
# General tools (Reclaimerr /api/v1, Bearer API token)
# ---------------------------------------------------------------------------


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


def candidate_status(candidate_id: int) -> dict:
    """Get full lifecycle detail for a single reclaim candidate by its Reclaimerr ID."""
    response = client.get(f"/candidates/{candidate_id}")
    response.raise_for_status()
    return response.json()


def protect_candidate(candidate_id: int, reason: str | None = None) -> dict:
    """Permanently protect a reclaim candidate from deletion (requires candidates:manage scope)."""
    response = client.post(f"/candidates/{candidate_id}/protect", json={"reason": reason})
    response.raise_for_status()
    return response.json()


def postpone_candidate(candidate_id: int, until: str, reason: str | None = None) -> dict:
    """Postpone a candidate's scheduled deletion until a later ISO-8601 timestamp
    (must be after its current deletion deadline; requires candidates:manage scope)."""
    response = client.post(
        f"/candidates/{candidate_id}/postpone", json={"until": until, "reason": reason}
    )
    response.raise_for_status()
    return response.json()


def cancel_candidate_deletion(candidate_id: int, reason: str | None = None) -> dict:
    """Cancel a pending scheduled deletion for a candidate (requires candidates:manage scope)."""
    response = client.post(f"/candidates/{candidate_id}/cancel", json={"reason": reason})
    response.raise_for_status()
    return response.json()


def list_protections(media_type: str | None = None, active_only: bool = True) -> list[dict]:
    """List protected media (items excluded from reclaim), optionally filtered by
    media_type ("movie" or "series"). Returns up to 200 protections."""
    params = {"per_page": 200, "active_only": active_only}
    if media_type:
        params["media_type"] = media_type

    response = client.get("/protections", params=params)
    response.raise_for_status()
    return response.json()["items"]


def list_tasks() -> list[dict]:
    """List Reclaimerr's background tasks (media sync, candidate scan, cleanup, ...)
    with their schedule, last/next run time, and current status."""
    response = client.get("/tasks")
    response.raise_for_status()
    return response.json()["items"]


def run_task(task_id: str) -> dict:
    """Trigger an immediate run of a background task by its ID (e.g.
    "scan_cleanup_candidates"; see list_tasks for valid IDs). Requires tasks:run scope."""
    response = client.post(f"/tasks/{task_id}/run")
    response.raise_for_status()
    return response.json()


def system_status() -> dict:
    """Get Reclaimerr system status: version, capabilities, and last sync/scan times."""
    response = client.get("/system")
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------------
# Rules tools (Reclaimerr /api, admin session cookie)
# ---------------------------------------------------------------------------


class Approval(BaseModel):
    """The approval prompt shown to the human. Unticked (the default) means reject."""

    approve: bool = Field(default=False, description="Tick to approve this change; leave unticked to reject it")


# Returned by an approval resolver when no prompt is needed; the tool body still
# runs the same `_is_approved` check, so there is one code path for both cases.
_NO_APPROVAL_NEEDED = Approval(approve=True)


def _is_approved(outcome: Any) -> bool:
    """True only for an accepted prompt whose box was ticked (or no prompt was needed).

    Anything else — declined, cancelled, unticked, or a missing outcome — is a refusal.
    """
    return (
        getattr(outcome, "action", None) == "accept"
        and getattr(getattr(outcome, "data", None), "approve", False) is True
    )


def _error_detail(response: httpx.Response) -> str:
    """Reclaimerr's own explanation for an error response (FastAPI `detail`)."""
    detail: Any = None
    try:
        body = response.json()
        if isinstance(body, dict):
            detail = body.get("detail")
    except ValueError:
        pass
    if isinstance(detail, list):  # pydantic validation errors
        detail = "; ".join(
            f"{'.'.join(str(part) for part in item.get('loc', []))}: {item.get('msg')}"
            for item in detail
            if isinstance(item, dict)
        )
    return str(detail or response.text[:300] or "no detail given")


def _rules_call(method: str, path: str, **kwargs: Any) -> Any:
    """Call a rules route and return its JSON, turning failures into readable tool errors."""
    try:
        response = _session_request(method, path, **kwargs)
    except LoginError as error:
        raise ToolError(f"Could not authenticate to Reclaimerr: {error}") from error
    except httpx.RequestError as error:
        raise ToolError(f"Cannot reach Reclaimerr at {RECLAIMERR_URL}: {error}") from error

    if response.status_code == 403:
        raise ToolError(
            f"Reclaimerr refused this request (HTTP 403): the configured account is not an admin. {_error_detail(response)}"
        )
    if response.status_code >= 400:
        raise ToolError(f"Reclaimerr returned HTTP {response.status_code}: {_error_detail(response)}")
    return response.json()


def _all_rules() -> list[dict]:
    return _rules_call("GET", "/rules")


def _find_rule(rule_id: int) -> dict:
    for rule in _all_rules():
        if rule.get("id") == rule_id:
            return rule
    raise ToolError(f"Rule {rule_id} not found. Use list_rules to see existing rule IDs.")


def _short(value: Any, limit: int = 400) -> str:
    text = json.dumps(value, sort_keys=True, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _preview(
    media_type: str,
    target_scope: str,
    definition: dict,
    name: str | None = None,
    outcome: str = "candidate",
    page: int = 1,
    per_page: int = 10,
) -> dict:
    """Dry-run a rule definition against the library. Saves nothing."""
    body = _rules_call(
        "POST",
        "/rules/preview",
        json={
            "name": name,
            "media_type": media_type,
            "target_scope": target_scope,
            "definition": definition,
            "outcome": outcome,
            "page": max(page, 1),
            "per_page": min(max(per_page, 1), 25),
        },
        timeout=120,  # scans the whole library, and may query Sonarr/Seerr/playback
    )
    return {
        "totalMatches": body.get("total"),
        "page": body.get("page"),
        "totalPages": body.get("total_pages"),
        "items": body.get("items", []),
        "metadata": body.get("metadata", {}),
    }


def rules_status() -> dict:
    """Report which Reclaimerr tool groups are enabled on this server, and exactly why any are not.

    Call this first if you can't find the rules tools, or a rules tool fails. The rules tools need
    an ADMIN Reclaimerr login configured on the server plus MCP_AUTH_TOKEN; without those they are
    not listed at all. A wrong password or non-admin account needs a container restart with fixed
    settings; the client must reconnect afterwards to see the new tool list.
    """
    return {
        "generalTools": {
            "enabled": bool(RECLAIMERR_API_TOKEN),
            "reason": None if RECLAIMERR_API_TOKEN else "RECLAIMERR_API_TOKEN is not set",
        },
        "rulesTools": _rules_report(),
        "rulesPolicy": [
            "create_rule always saves the rule disabled with auto-delete off, after a dry-run.",
            "Enabling a rule or turning on auto-delete is only possible in the Reclaimerr UI.",
            "Updating an enabled rule and deleting a rule ask the user for approval first.",
        ],
    }


def list_rules() -> list[dict]:
    """List all cleanup rules (name, media type, target scope, enabled flag, definition, action).

    Use these as templates for the shape of `definition` and `action` when creating a rule.
    """
    return _all_rules()


def get_rule(rule_id: int) -> dict:
    """Get one cleanup rule by its Reclaimerr ID."""
    return _find_rule(rule_id)


def preview_rule(
    media_type: str,
    target_scope: str,
    definition: dict,
    name: str | None = None,
    outcome: str = "candidate",
    page: int = 1,
    per_page: int = 10,
) -> dict:
    """Dry-run a rule definition: show what it WOULD match, without saving anything (read-only).

    Use this before create_rule or before changing a rule's definition, and tell the user the match
    count and a few examples. See create_rule for the argument formats. outcome is "candidate"
    (flag for reclaim) or "protect". per_page is capped at 25.
    """
    return _preview(media_type, target_scope, definition, name, outcome, page, per_page)


def create_rule(
    name: str,
    media_type: str,
    target_scope: str,
    definition: dict,
    description: str | None = None,
    action: dict | None = None,
) -> dict:
    """Create a cleanup rule. It is ALWAYS saved DISABLED with auto-delete OFF, whatever is passed:
    the user reviews it and enables it in the Reclaimerr UI, so tell them that.

    A dry-run (same as preview_rule) runs first; if it fails, e.g. an invalid definition, nothing is
    created. Report the dry-run match count and examples to the user.

    media_type: "movie" or "series". target_scope: "movie_version", "series", "season" or "episode".
    definition: the condition tree, e.g. {"version": 1, "root": {"type": "group", "op": "and",
    "children": [{"type": "condition", "field": "watch.last_viewed_at", "operator": "exists"}]}}.
    Copy the shape from list_rules; an unknown field or operator comes back as a validation error.
    action: optional dict (outcome, arr_action, media_server_action, arr_tag, ...) - copy from
    list_rules. Any auto_delete_enabled value is ignored and forced to false.
    """
    safe_action = dict(action or {})
    safe_action["auto_delete_enabled"] = False
    outcome = safe_action.get("outcome") if safe_action.get("outcome") in ("candidate", "protect") else "candidate"

    try:
        dry_run = _preview(media_type, target_scope, definition, name, outcome)
    except ToolError as error:
        raise ToolError(f"Dry-run failed, so the rule was NOT created: {error}") from error

    rule = _rules_call(
        "POST",
        "/rules",
        json={
            "name": name,
            "description": description,
            "media_type": media_type,
            "enabled": False,
            "target_scope": target_scope,
            "definition": definition,
            "action": safe_action,
        },
    )
    return {
        "created": rule,
        "dryRun": dry_run,
        "note": (
            "Saved DISABLED with auto-delete OFF. Tell the user to review it and enable it in the "
            "Reclaimerr UI. Report the dry-run match count and examples."
        ),
    }


def _build_update_payload(
    rule: dict,
    *,
    name: str | None,
    description: str | None,
    media_type: str | None,
    target_scope: str | None,
    definition: dict | None,
    action: dict | None,
) -> dict:
    """The fields to send for an update. Never includes `enabled`, never changes auto-delete."""
    payload: dict[str, Any] = {}
    for field, value in (
        ("name", name),
        ("description", description),
        ("media_type", media_type),
        ("target_scope", target_scope),
        ("definition", definition),
    ):
        if value is not None:
            payload[field] = value

    if action is not None:
        current_action = rule.get("action") or {}
        current_auto_delete = current_action.get("auto_delete_enabled") is True
        if "auto_delete_enabled" in action and (action["auto_delete_enabled"] is True) != current_auto_delete:
            raise ToolError(
                "auto_delete_enabled can only be changed in the Reclaimerr UI, not through this server."
            )
        # Reclaimerr replaces `action` wholesale, so merge onto the current one, otherwise leaving a
        # key out would silently reset it (including switching an existing rule's auto-delete off).
        payload["action"] = {**current_action, **action, "auto_delete_enabled": current_auto_delete}

    if not payload:
        raise ToolError(
            "Nothing to update: give at least one of name, description, media_type, target_scope, "
            "definition or action. (Enabling/disabling a rule is only possible in the Reclaimerr UI.)"
        )
    return payload


def _approve_rule_update(
    rule_id: int,
    name: str | None,
    description: str | None,
    media_type: str | None,
    target_scope: str | None,
    definition: dict | None,
    action: dict | None,
) -> Elicit[Approval] | Approval:
    """Ask the human before changing a rule that is currently ENABLED; disabled rules need no prompt.

    Resolvers can re-run between prompt rounds, so this only does idempotent reads.
    """
    rule = _find_rule(rule_id)
    payload = _build_update_payload(
        rule,
        name=name,
        description=description,
        media_type=media_type,
        target_scope=target_scope,
        definition=definition,
        action=action,
    )
    if not rule.get("enabled"):
        return _NO_APPROVAL_NEEDED

    changes = "\n".join(
        f"- {field}: {_short(rule.get(field), 200)} -> {_short(value)}" for field, value in sorted(payload.items())
    )
    return Elicit(
        f"Update rule '{rule.get('name')}' (ID {rule_id}), which is currently ENABLED, so the change "
        f"affects live cleanup behaviour.\n\nChanges:\n{changes}\n\nApprove this change?",
        Approval,
    )


def update_rule(
    rule_id: int,
    name: str | None = None,
    description: str | None = None,
    media_type: str | None = None,
    target_scope: str | None = None,
    definition: dict | None = None,
    action: dict | None = None,
    approval: Annotated[ElicitationResult[Approval], Resolve(_approve_rule_update)] = None,  # type: ignore[assignment]
) -> dict:
    """Update fields of an existing rule; omitted fields are left unchanged.

    If the rule is currently ENABLED the user is asked to approve the change first (a prompt in
    their client); disabled rules update without a prompt. If they decline, nothing changes: tell
    them and don't retry. Enabling/disabling a rule and changing auto_delete_enabled are NOT possible
    here - the user does that in the Reclaimerr UI. `action` is merged onto the rule's current action.
    For a definition change, use preview_rule first and tell the user the match count.
    """
    if not _is_approved(approval):
        return {"updated": False, "reason": "The user declined the change (or the client could not show the approval prompt). Nothing was changed."}

    rule = _find_rule(rule_id)
    payload = _build_update_payload(
        rule,
        name=name,
        description=description,
        media_type=media_type,
        target_scope=target_scope,
        definition=definition,
        action=action,
    )
    return {"updated": True, "rule": _rules_call("POST", f"/rules/{rule_id}", json=payload)}


def _approve_rule_delete(rule_id: int) -> Elicit[Approval]:
    """Always ask the human before a rule is deleted."""
    rule = _find_rule(rule_id)
    state = "ENABLED" if rule.get("enabled") else "disabled"
    return Elicit(
        f"Delete rule '{rule.get('name')}' (ID {rule_id}, currently {state})? This cannot be undone.",
        Approval,
    )


def delete_rule(
    rule_id: int,
    approval: Annotated[ElicitationResult[Approval], Resolve(_approve_rule_delete)] = None,  # type: ignore[assignment]
) -> dict:
    """Permanently delete a cleanup rule. The user is always asked to approve first (a prompt in their
    client). If they decline, nothing is deleted: tell them and don't retry."""
    if not _is_approved(approval):
        return {"deleted": False, "reason": "The user declined the deletion (or the client could not show the approval prompt). Nothing was deleted."}

    rule = _find_rule(rule_id)
    _rules_call("DELETE", f"/rules/{rule_id}")
    return {"deleted": True, "rule": {"id": rule_id, "name": rule.get("name")}}


# ---------------------------------------------------------------------------
# Tool registration: only what is actually usable is listed
# ---------------------------------------------------------------------------

GENERAL_TOOLS = (
    list_candidates,
    candidate_status,
    protect_candidate,
    postpone_candidate,
    cancel_candidate_deletion,
    list_protections,
    list_tasks,
    run_task,
    system_status,
)
RULES_TOOLS = (list_rules, get_rule, preview_rule, create_rule, update_rule, delete_rule)

_registered_tools: set[str] = set()
# sync_tools runs from the startup thread and from build_app, so serialise it.
_tools_lock = threading.Lock()


def _log_tools() -> None:
    """Log which tools are listed, and for an empty group, why. Call with `_tools_lock` held.

    Runs whenever the listed set changes (startup, or the rules tools appearing or
    disappearing), so the log always shows what a client connecting now would see.
    """
    general = [tool.__name__ for tool in GENERAL_TOOLS if tool.__name__ in _registered_tools]
    rules = [tool.__name__ for tool in RULES_TOOLS if tool.__name__ in _registered_tools]
    general_line = ", ".join(general) if general else "none — RECLAIMERR_API_TOKEN is not set"
    rules_line = ", ".join(rules) if rules else f"none — {rules_state.reason or rules_state.status}"
    print(
        f"Tools available ({len(_registered_tools)}):\n"
        f"  always : {rules_status.__name__}\n"
        f"  general: {general_line}\n"
        f"  rules  : {rules_line}",
        file=sys.stderr,
    )


def sync_tools() -> None:
    """Make the listed tools match the configuration and the rules check outcome.

    General tools need RECLAIMERR_API_TOKEN; rules tools need the check to have passed;
    `rules_status` is always listed so the AI can find out why something is missing.
    """
    with _tools_lock:
        wanted: dict[str, Any] = {rules_status.__name__: rules_status}
        if RECLAIMERR_API_TOKEN:
            wanted.update({tool.__name__: tool for tool in GENERAL_TOOLS})
        if rules_state.status == "ready":
            wanted.update({tool.__name__: tool for tool in RULES_TOOLS})

        before = set(_registered_tools)
        for name in sorted(_registered_tools - wanted.keys()):
            mcp.remove_tool(name)
            _registered_tools.discard(name)
        for name, tool in wanted.items():
            if name not in _registered_tools:
                mcp.add_tool(tool)
                _registered_tools.add(name)
        if _registered_tools != before:
            _log_tools()


def _log_rules_state(prefix: str) -> None:
    reason = f" — {rules_state.reason}" if rules_state.reason else ""
    print(f"{prefix}: {rules_state.status}{reason}", file=sys.stderr)


def _check_and_retry_rules(first_check_done: threading.Event) -> None:
    """Background worker: the startup rules check, then retries while Reclaimerr is unreachable."""
    check_rules_access()
    sync_tools()
    _log_rules_state("Rules tools")
    first_check_done.set()
    while rules_state.status == "pending":
        time.sleep(RULES_RECHECK_SECONDS)
        check_rules_access()
        sync_tools()
        _log_rules_state("Rules tools re-check")


def initialize() -> None:
    """Start the rules check without letting it hold up the server.

    The check runs in a background thread, because a Reclaimerr that hangs (or a
    resolver that stalls) must not stop the general tools from coming up. Startup
    waits up to STARTUP_CHECK_WAIT_SECONDS for the first result, so in the normal,
    fast case the rules tools are already listed when the first client connects;
    if it is slower, the state is "pending" and they appear when the check finishes
    (clients see them on their next connect).
    """
    global rules_state
    rules_state = RulesState("pending", "startup check still running")
    first_check_done = threading.Event()
    threading.Thread(
        target=_check_and_retry_rules, args=(first_check_done,), name="rules-check", daemon=True
    ).start()
    if not first_check_done.wait(STARTUP_CHECK_WAIT_SECONDS):
        print(
            f"Rules check still running after {STARTUP_CHECK_WAIT_SECONDS}s; starting the server without waiting",
            file=sys.stderr,
        )
        sync_tools()


# Paths that must stay reachable without MCP_AUTH_TOKEN, so Docker's own
# HEALTHCHECK, Dockhand's health probe, etc. don't need the secret.
UNAUTHENTICATED_PATHS = {"/health", "/ready"}


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> Response:
    """Liveness check: the process is up and serving HTTP. Does not call Reclaimerr."""
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/ready", methods=["GET"])
async def ready(request: Request) -> Response:
    """Readiness check: RECLAIMERR_URL is reachable and the configured credentials work.

    With RECLAIMERR_API_TOKEN set this validates it (with at least the system:read
    scope) and reports the rules tools' state alongside; a rules problem alone never
    turns this unhealthy, because the general tools still work. With only the
    username/password configured, readiness is the rules check itself (cached from
    startup, never a fresh login: Reclaimerr rate-limits those).
    """
    rules = _rules_report()

    if not RECLAIMERR_API_TOKEN:
        is_ready = rules["status"] == "ready"
        return JSONResponse(
            {
                "status": "ok" if is_ready else "error",
                "reachable": rules["status"] != "pending",
                "authenticated": is_ready,
                "rules": rules,
                **({} if is_ready else {"error": rules["reason"]}),
            },
            status_code=200 if is_ready else 503,
        )

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
            {
                "status": "error",
                "reachable": True,
                "authenticated": status_code != 401,
                "error": reason,
                "rules": rules,
            },
            status_code=503,
        )
    except httpx.RequestError as error:
        return JSONResponse(
            {
                "status": "error",
                "reachable": False,
                "authenticated": False,
                "error": f"cannot reach Reclaimerr at {RECLAIMERR_URL}: {error}",
                "rules": rules,
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
            "rules": rules,
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
    sync_tools()
    app = mcp.streamable_http_app(host=MCP_HOST)

    if MCP_AUTH_TOKEN:
        app.add_middleware(BearerTokenMiddleware)
        print("Auth enabled: Authorization: Bearer <token> required", file=sys.stderr)
    else:
        print("WARNING: MCP_AUTH_TOKEN not set — server is open to anyone who can reach it", file=sys.stderr)

    return app


if __name__ == "__main__":
    initialize()
    uvicorn.run(build_app(), host=MCP_HOST, port=MCP_PORT)
