"""Tests for the rules tools: availability gating, login/session handling, the
startup admin check, each tool's safety policy, and the human-approval logic.

Like test_tools.py these call the tool functions directly (registration via
`add_tool` returns nothing that wraps them), against `FakeReclaimerr` from
conftest.py, which stands in for Reclaimerr's cookie-authenticated /api routes.
"""

import asyncio
import threading
import time

import httpx
import pytest
from mcp.server.mcpserver import AcceptedElicitation, CancelledElicitation, DeclinedElicitation, Elicit
from mcp.server.mcpserver.exceptions import ToolError

import server

APPROVED = AcceptedElicitation(data=server.Approval(approve=True))
# What the SDK injects when an approval resolver returns a plain value instead of
# asking: it wraps it as an accepted outcome (mcp.server.mcpserver.resolve._accepted).
NO_PROMPT_NEEDED = AcceptedElicitation(data=server._NO_APPROVAL_NEEDED)
UNTICKED = AcceptedElicitation(data=server.Approval(approve=False))

DEFINITION = {
    "version": 1,
    "root": {
        "type": "group",
        "op": "and",
        "children": [{"type": "condition", "field": "watch.last_viewed_at", "operator": "exists"}],
    },
}

GENERAL = {
    "list_candidates",
    "candidate_status",
    "protect_candidate",
    "postpone_candidate",
    "cancel_candidate_deletion",
    "list_protections",
    "list_tasks",
    "run_task",
    "system_status",
}
RULES = {"list_rules", "get_rule", "preview_rule", "create_rule", "update_rule", "delete_rule"}


def listed_tools() -> set[str]:
    return {tool.name for tool in asyncio.run(server.mcp.list_tools())}


# --- which tools are listed -------------------------------------------------


def test_general_only_when_rules_not_ready(monkeypatch):
    monkeypatch.setattr(server, "RECLAIMERR_API_TOKEN", "rcl_x")
    server.sync_tools()
    assert listed_tools() == GENERAL | {"rules_status"}


def test_rules_tools_listed_only_once_ready(monkeypatch, rules_ready):
    monkeypatch.setattr(server, "RECLAIMERR_API_TOKEN", "rcl_x")
    server.sync_tools()
    assert listed_tools() == GENERAL | RULES | {"rules_status"}


def test_rules_only_mode_hides_general_tools(monkeypatch, rules_ready):
    monkeypatch.setattr(server, "RECLAIMERR_API_TOKEN", None)
    server.sync_tools()
    assert listed_tools() == RULES | {"rules_status"}


def test_tools_disappear_again_if_rules_stop_being_ready(monkeypatch, rules_ready):
    monkeypatch.setattr(server, "RECLAIMERR_API_TOKEN", "rcl_x")
    server.sync_tools()
    assert RULES <= listed_tools()

    monkeypatch.setattr(server, "rules_state", server.RulesState("disabled", "changed"))
    server.sync_tools()
    assert not RULES & listed_tools()


def test_pending_state_keeps_rules_tools_hidden(monkeypatch):
    monkeypatch.setattr(server, "RECLAIMERR_API_TOKEN", "rcl_x")
    monkeypatch.setattr(server, "rules_state", server.RulesState("pending", "unreachable"))
    server.sync_tools()
    assert not RULES & listed_tools()


def test_approval_parameter_is_not_exposed_to_the_ai(rules_ready):
    server.sync_tools()
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    assert "approval" not in tools["update_rule"].input_schema["properties"]
    assert "approval" not in tools["delete_rule"].input_schema["properties"]


def test_rules_status_explains_missing_config(monkeypatch):
    monkeypatch.setattr(server, "RECLAIMERR_API_TOKEN", None)
    monkeypatch.setattr(
        server, "rules_state", server.RulesState("disabled", "MCP_AUTH_TOKEN is not set")
    )
    status = server.rules_status()

    assert status["generalTools"] == {"enabled": False, "reason": "RECLAIMERR_API_TOKEN is not set"}
    assert status["rulesTools"]["enabled"] is False
    assert status["rulesTools"]["reason"] == "MCP_AUTH_TOKEN is not set"


# --- the startup admin check ------------------------------------------------


def test_check_passes_for_admin(fake_reclaimerr, with_auth):
    state = server.check_rules_access()
    assert state.status == "ready"
    assert state.account == "admin"


def test_check_rejects_non_admin_permanently(fake_reclaimerr, with_auth):
    fake_reclaimerr.role = "user"
    state = server.check_rules_access()
    assert state.status == "disabled"
    assert "role 'user'" in state.reason
    assert "admin" in state.reason


def test_check_wrong_password_is_permanent(fake_reclaimerr, with_auth):
    fake_reclaimerr.login_status = 401
    state = server.check_rules_access()
    assert state.status == "disabled"
    assert "username/password" in state.reason


def test_check_disabled_account_is_permanent(fake_reclaimerr, with_auth):
    fake_reclaimerr.login_status = 403
    assert server.check_rules_access().status == "disabled"


def test_check_rate_limited_is_retried(fake_reclaimerr, with_auth):
    fake_reclaimerr.login_status = 429
    assert server.check_rules_access().status == "pending"


def test_check_reclaimerr_error_is_retried(fake_reclaimerr, with_auth):
    fake_reclaimerr.login_status = 503
    assert server.check_rules_access().status == "pending"


def test_check_unreachable_is_retried(monkeypatch, with_auth):
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(
        server,
        "session",
        httpx.Client(base_url=f"{server.RECLAIMERR_URL}/api", transport=httpx.MockTransport(handler)),
    )
    state = server.check_rules_access()
    assert state.status == "pending"
    assert "cannot reach" in state.reason


def test_check_without_mcp_auth_token_never_touches_the_network(monkeypatch, no_auth):
    def handler(request):
        raise AssertionError("must not log in when the rules tools can't be enabled")

    monkeypatch.setattr(
        server,
        "session",
        httpx.Client(base_url=f"{server.RECLAIMERR_URL}/api", transport=httpx.MockTransport(handler)),
    )
    state = server.check_rules_access()
    assert state.status == "disabled"
    assert "MCP_AUTH_TOKEN" in state.reason


def test_check_without_credentials_is_disabled(monkeypatch, with_auth):
    monkeypatch.setattr(server, "RECLAIMERR_USERNAME", None)
    monkeypatch.setattr(server, "RECLAIMERR_PASSWORD", None)
    state = server.check_rules_access()
    assert state.status == "disabled"
    assert "RECLAIMERR_USERNAME" in state.reason


def test_check_missing_rules_route_is_disabled(fake_reclaimerr, with_auth):
    fake_reclaimerr.rules_status = 404
    state = server.check_rules_access()
    assert state.status == "disabled"
    assert "/api/rules" in state.reason


def test_check_forbidden_rules_route_is_disabled(fake_reclaimerr, with_auth):
    fake_reclaimerr.rules_status = 403
    assert server.check_rules_access().status == "disabled"


# --- session handling -------------------------------------------------------


def test_logs_in_lazily_once_and_reuses_the_cookie(fake_reclaimerr, rules_ready):
    server.list_rules()
    server.list_rules()
    assert fake_reclaimerr.logins == 1


def test_logs_in_again_when_the_session_expires(fake_reclaimerr, rules_ready):
    fake_reclaimerr.add_rule(name="Kept")
    server.list_rules()
    fake_reclaimerr.expire_session()

    rules = server.list_rules()

    assert [rule["name"] for rule in rules] == ["Kept"]
    assert fake_reclaimerr.logins == 2


def test_relogin_happens_at_most_once_per_call(fake_reclaimerr, rules_ready):
    """Reclaimerr rate-limits login to 5/minute, so a session it never accepts must not loop."""
    server.list_rules()
    fake_reclaimerr.always_unauthorized = True

    with pytest.raises(ToolError, match="authenticate"):
        server.list_rules()

    assert fake_reclaimerr.logins == 2  # the first login, plus exactly one retry


def test_failed_login_surfaces_a_readable_error(fake_reclaimerr, rules_ready):
    fake_reclaimerr.login_status = 401
    with pytest.raises(ToolError, match="username/password"):
        server.list_rules()


def test_forbidden_means_not_admin(fake_reclaimerr, rules_ready):
    fake_reclaimerr.rules_status = 403
    with pytest.raises(ToolError, match="not an admin"):
        server.list_rules()


def test_reclaimerr_validation_detail_reaches_the_ai(fake_reclaimerr, rules_ready):
    fake_reclaimerr.preview_status = 422
    with pytest.raises(ToolError, match="unknown field 'nope'"):
        server.preview_rule("series", "episode", DEFINITION)


# --- read tools -------------------------------------------------------------


def test_list_and_get_rule(fake_reclaimerr, rules_ready):
    first = fake_reclaimerr.add_rule(name="One")
    fake_reclaimerr.add_rule(name="Two")

    assert [rule["name"] for rule in server.list_rules()] == ["One", "Two"]
    assert server.get_rule(first["id"])["name"] == "One"


def test_get_rule_not_found(fake_reclaimerr, rules_ready):
    with pytest.raises(ToolError, match="Rule 99 not found"):
        server.get_rule(99)


def test_preview_rule_shapes_the_result_and_caps_page_size(fake_reclaimerr, rules_ready):
    result = server.preview_rule("series", "episode", DEFINITION, name="X", per_page=500)

    assert result["totalMatches"] == 3
    assert result["items"] == [{"title": "Some Show S01E01"}]
    sent = fake_reclaimerr.calls("POST", "/rules/preview")[0]
    assert sent["per_page"] == 25
    assert sent["definition"] == DEFINITION
    assert fake_reclaimerr.calls("POST", "/rules") == []  # a preview never saves anything


# --- create_rule: always disabled, always dry-run first ----------------------


def test_create_rule_is_always_saved_disabled_with_auto_delete_off(fake_reclaimerr, rules_ready):
    result = server.create_rule(
        "Old shows",
        "series",
        "episode",
        DEFINITION,
        action={"outcome": "candidate", "auto_delete_enabled": True, "auto_delete_delay_days": 7},
    )

    sent = fake_reclaimerr.calls("POST", "/rules")[0]
    assert sent["enabled"] is False
    assert sent["action"]["auto_delete_enabled"] is False
    assert sent["action"]["auto_delete_delay_days"] == 7  # other action fields are passed through
    assert result["created"]["enabled"] is False
    assert result["created"]["action"]["auto_delete_enabled"] is False


def test_create_rule_without_an_action_still_disables_auto_delete(fake_reclaimerr, rules_ready):
    server.create_rule("Plain", "series", "episode", DEFINITION)
    assert fake_reclaimerr.calls("POST", "/rules")[0]["action"]["auto_delete_enabled"] is False


def test_create_rule_runs_the_dry_run_first_and_returns_it(fake_reclaimerr, rules_ready):
    fake_reclaimerr.preview_total = 312

    result = server.create_rule("Old shows", "series", "episode", DEFINITION)

    order = [(method, path) for method, path, _ in fake_reclaimerr.requests if path.startswith("/rules")]
    assert order == [("POST", "/rules/preview"), ("POST", "/rules")]
    assert result["dryRun"]["totalMatches"] == 312
    assert "DISABLED" in result["note"]


def test_create_rule_is_not_created_when_the_dry_run_fails(fake_reclaimerr, rules_ready):
    fake_reclaimerr.preview_status = 422

    with pytest.raises(ToolError, match="NOT created"):
        server.create_rule("Bad", "series", "episode", {"version": 1, "root": {}})

    assert fake_reclaimerr.calls("POST", "/rules") == []
    assert fake_reclaimerr.rules == []


def test_create_rule_previews_with_the_protect_outcome_when_asked(fake_reclaimerr, rules_ready):
    server.create_rule("Keep", "series", "episode", DEFINITION, action={"outcome": "protect"})
    assert fake_reclaimerr.calls("POST", "/rules/preview")[0]["outcome"] == "protect"


# --- update_rule ------------------------------------------------------------


def test_update_disabled_rule_needs_no_prompt(fake_reclaimerr, rules_ready):
    rule = fake_reclaimerr.add_rule(name="Old", enabled=False)

    assert isinstance(server._approve_rule_update(rule["id"], "New", None, None, None, None, None), server.Approval)
    result = server.update_rule(rule["id"], name="New", approval=NO_PROMPT_NEEDED)

    assert result["updated"] is True
    assert result["rule"]["name"] == "New"


def test_update_never_sends_enabled(fake_reclaimerr, rules_ready):
    rule = fake_reclaimerr.add_rule(enabled=False)
    server.update_rule(rule["id"], name="Renamed", approval=NO_PROMPT_NEEDED)

    sent = fake_reclaimerr.calls("POST", f"/rules/{rule['id']}")[0]
    assert "enabled" not in sent
    assert sent == {"name": "Renamed"}
    assert rule["enabled"] is False


def test_update_action_is_merged_so_existing_keys_and_auto_delete_survive(fake_reclaimerr, rules_ready):
    rule = fake_reclaimerr.add_rule(
        enabled=False,
        action={
            "outcome": "candidate",
            "arr_action": "unmonitor",
            "arr_tag": "rec-keepme",
            "auto_delete_enabled": True,
            "auto_delete_delay_days": 30,
        },
    )

    server.update_rule(rule["id"], action={"arr_action": "delete"}, approval=NO_PROMPT_NEEDED)

    stored = fake_reclaimerr.rules[0]["action"]
    assert stored["arr_action"] == "delete"  # the requested change
    assert stored["arr_tag"] == "rec-keepme"  # untouched keys survive the wholesale replace
    assert stored["auto_delete_delay_days"] == 30
    assert stored["auto_delete_enabled"] is True  # an existing setting is preserved, not reset


def test_update_cannot_turn_auto_delete_on(fake_reclaimerr, rules_ready):
    rule = fake_reclaimerr.add_rule(enabled=False, action={"outcome": "candidate", "auto_delete_enabled": False})

    with pytest.raises(ToolError, match="Reclaimerr UI"):
        server.update_rule(rule["id"], action={"auto_delete_enabled": True}, approval=NO_PROMPT_NEEDED)

    assert fake_reclaimerr.calls("POST", f"/rules/{rule['id']}") == []


def test_update_cannot_turn_auto_delete_off_either(fake_reclaimerr, rules_ready):
    """Any change to auto-delete is UI-only, so the AI can't quietly weaken an existing setting either."""
    rule = fake_reclaimerr.add_rule(enabled=False, action={"outcome": "candidate", "auto_delete_enabled": True})

    with pytest.raises(ToolError, match="Reclaimerr UI"):
        server.update_rule(rule["id"], action={"auto_delete_enabled": False}, approval=NO_PROMPT_NEEDED)


def test_update_with_no_fields_is_an_error(fake_reclaimerr, rules_ready):
    rule = fake_reclaimerr.add_rule()
    with pytest.raises(ToolError, match="Nothing to update"):
        server.update_rule(rule["id"], approval=NO_PROMPT_NEEDED)


def test_update_missing_rule(fake_reclaimerr, rules_ready):
    with pytest.raises(ToolError, match="Rule 5 not found"):
        server.update_rule(5, name="x", approval=NO_PROMPT_NEEDED)


def test_update_prompts_for_an_enabled_rule_and_shows_the_changes(fake_reclaimerr, rules_ready):
    rule = fake_reclaimerr.add_rule(name="Live rule", enabled=True)

    marker = server._approve_rule_update(rule["id"], "Renamed", None, None, None, None, None)

    assert isinstance(marker, Elicit)
    assert marker.schema is server.Approval
    assert "Live rule" in marker.message
    assert "ENABLED" in marker.message
    assert "name" in marker.message and "Renamed" in marker.message


def test_update_prompt_validates_before_asking(fake_reclaimerr, rules_ready):
    """A change that would be rejected anyway shouldn't bother the human with a prompt."""
    rule = fake_reclaimerr.add_rule(enabled=True)
    with pytest.raises(ToolError):
        server._approve_rule_update(rule["id"], None, None, None, None, None, None)


@pytest.mark.parametrize("outcome", [DeclinedElicitation(), CancelledElicitation(), UNTICKED, None])
def test_update_refused_unless_explicitly_approved(fake_reclaimerr, rules_ready, outcome):
    rule = fake_reclaimerr.add_rule(name="Live", enabled=True)

    result = server.update_rule(rule["id"], name="Changed", approval=outcome)

    assert result["updated"] is False
    assert fake_reclaimerr.calls("POST", f"/rules/{rule['id']}") == []
    assert fake_reclaimerr.rules[0]["name"] == "Live"


def test_update_applied_when_approved(fake_reclaimerr, rules_ready):
    rule = fake_reclaimerr.add_rule(name="Live", enabled=True)

    result = server.update_rule(rule["id"], name="Changed", approval=APPROVED)

    assert result["updated"] is True
    assert fake_reclaimerr.rules[0]["name"] == "Changed"
    assert fake_reclaimerr.rules[0]["enabled"] is True  # untouched


# --- delete_rule ------------------------------------------------------------


def test_delete_always_prompts(fake_reclaimerr, rules_ready):
    disabled = fake_reclaimerr.add_rule(name="Quiet", enabled=False)
    enabled = fake_reclaimerr.add_rule(name="Live", enabled=True)

    quiet_prompt = server._approve_rule_delete(disabled["id"])
    live_prompt = server._approve_rule_delete(enabled["id"])

    assert isinstance(quiet_prompt, Elicit) and isinstance(live_prompt, Elicit)
    assert "Quiet" in quiet_prompt.message and "disabled" in quiet_prompt.message
    assert "Live" in live_prompt.message and "ENABLED" in live_prompt.message


@pytest.mark.parametrize("outcome", [DeclinedElicitation(), CancelledElicitation(), UNTICKED, None])
def test_delete_refused_unless_explicitly_approved(fake_reclaimerr, rules_ready, outcome):
    rule = fake_reclaimerr.add_rule()

    result = server.delete_rule(rule["id"], approval=outcome)

    assert result["deleted"] is False
    assert len(fake_reclaimerr.rules) == 1
    assert fake_reclaimerr.calls("DELETE", f"/rules/{rule['id']}") == []


def test_delete_applied_when_approved(fake_reclaimerr, rules_ready):
    rule = fake_reclaimerr.add_rule(name="Doomed")

    result = server.delete_rule(rule["id"], approval=APPROVED)

    assert result == {"deleted": True, "rule": {"id": rule["id"], "name": "Doomed"}}
    assert fake_reclaimerr.rules == []


def test_delete_missing_rule(fake_reclaimerr, rules_ready):
    with pytest.raises(ToolError, match="Rule 9 not found"):
        server.delete_rule(9, approval=APPROVED)


# --- startup: the check must never hold the server up -----------------------


def wait_for(condition, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_initialize_lists_rules_tools_before_returning_when_check_is_fast(fake_reclaimerr, with_auth, monkeypatch):
    monkeypatch.setattr(server, "RECLAIMERR_API_TOKEN", "rcl_x")

    server.initialize()

    assert server.rules_state.status == "ready"
    assert RULES <= listed_tools()


def test_initialize_does_not_wait_for_a_hung_reclaimerr(with_auth, monkeypatch):
    release = threading.Event()

    def hung(request):
        release.wait(5)  # a Reclaimerr that accepts the connection and never answers
        return httpx.Response(500)

    monkeypatch.setattr(
        server,
        "session",
        httpx.Client(base_url=f"{server.RECLAIMERR_URL}/api", transport=httpx.MockTransport(hung)),
    )
    monkeypatch.setattr(server, "STARTUP_CHECK_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(server, "RECLAIMERR_API_TOKEN", "rcl_x")

    started = time.monotonic()
    try:
        server.initialize()
        elapsed = time.monotonic() - started

        assert elapsed < 2  # returned long before the 5s hang ended
        assert server.rules_state.status == "pending"
        assert GENERAL <= listed_tools()  # the general tools are up regardless
        assert not RULES & listed_tools()
    finally:
        release.set()  # let the background thread finish


def test_initialize_disabled_config_needs_no_network(monkeypatch, no_auth):
    def handler(request):
        raise AssertionError("no network call expected")

    monkeypatch.setattr(
        server,
        "session",
        httpx.Client(base_url=f"{server.RECLAIMERR_URL}/api", transport=httpx.MockTransport(handler)),
    )
    server.initialize()
    assert server.rules_state.status == "disabled"


def test_unreachable_at_startup_is_retried_and_tools_appear_once_it_recovers(fake_reclaimerr, with_auth, monkeypatch):
    monkeypatch.setattr(server, "RECLAIMERR_API_TOKEN", "rcl_x")
    monkeypatch.setattr(server, "RULES_RECHECK_SECONDS", 0.05)
    fake_reclaimerr.login_status = 503  # Reclaimerr is still booting

    server.initialize()
    assert server.rules_state.status == "pending"
    assert not RULES & listed_tools()

    fake_reclaimerr.login_status = 200  # ...and now it's up

    assert wait_for(lambda: server.rules_state.status == "ready")
    assert wait_for(lambda: RULES <= listed_tools())


def test_permanent_failure_at_startup_is_not_retried(fake_reclaimerr, with_auth, monkeypatch):
    monkeypatch.setattr(server, "RULES_RECHECK_SECONDS", 0.05)
    fake_reclaimerr.role = "user"

    server.initialize()
    logins_after_startup = fake_reclaimerr.logins
    time.sleep(0.3)

    assert server.rules_state.status == "disabled"
    assert fake_reclaimerr.logins == logins_after_startup  # no retry loop, no login-limit burn


# --- the log says which tools are available --------------------------------


def test_log_lists_the_tools_and_why_a_group_is_missing(monkeypatch, capsys):
    monkeypatch.setattr(server, "RECLAIMERR_API_TOKEN", "rcl_x")
    monkeypatch.setattr(server, "_registered_tools", set())
    monkeypatch.setattr(server, "rules_state", server.RulesState("disabled", "Reclaimerr rejected the username/password (HTTP 401)"))

    server.sync_tools()

    log = capsys.readouterr().err
    assert "Tools available (10):" in log
    assert "always : rules_status" in log
    assert "list_candidates" in log and "run_task" in log
    assert "rules  : none — Reclaimerr rejected the username/password (HTTP 401)" in log


def test_log_lists_the_rules_tools_once_they_are_available(monkeypatch, capsys, rules_ready):
    monkeypatch.setattr(server, "RECLAIMERR_API_TOKEN", "rcl_x")
    monkeypatch.setattr(server, "_registered_tools", set())

    server.sync_tools()

    log = capsys.readouterr().err
    assert "Tools available (16):" in log
    assert "create_rule" in log and "delete_rule" in log
    assert "none —" not in log


def test_log_says_when_the_general_tools_are_off(monkeypatch, capsys, rules_ready):
    monkeypatch.setattr(server, "RECLAIMERR_API_TOKEN", None)
    monkeypatch.setattr(server, "_registered_tools", set())

    server.sync_tools()

    assert "general: none — RECLAIMERR_API_TOKEN is not set" in capsys.readouterr().err


def test_log_is_quiet_when_nothing_changed(monkeypatch, capsys, rules_ready):
    monkeypatch.setattr(server, "RECLAIMERR_API_TOKEN", "rcl_x")
    server.sync_tools()
    capsys.readouterr()

    server.sync_tools()

    assert capsys.readouterr().err == ""


def test_log_reports_the_change_when_rules_tools_disappear(monkeypatch, capsys, rules_ready):
    monkeypatch.setattr(server, "RECLAIMERR_API_TOKEN", "rcl_x")
    server.sync_tools()
    capsys.readouterr()

    monkeypatch.setattr(server, "rules_state", server.RulesState("disabled", "account 'x' has role 'user'"))
    server.sync_tools()

    log = capsys.readouterr().err
    assert "Tools available (10):" in log
    assert "rules  : none — account 'x' has role 'user'" in log
