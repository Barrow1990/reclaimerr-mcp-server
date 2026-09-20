"""End-to-end tests of the human-approval flow through the real MCP SDK.

test_rules.py checks the approval *logic* by handing the tools an outcome. These
go through an actual MCP client talking to `server.mcp`, whose `elicitation_callback`
plays the human, so they prove the SDK really injects the answer into the tool
(and really refuses when the client can't show a prompt) rather than assuming it.
"""

import asyncio

import pytest
from mcp.client import Client
from mcp_types import ElicitResult

import server


class Human:
    """Answers approval prompts; records what was asked."""

    def __init__(self, action: str = "accept", approve: bool = True):
        self.action = action
        self.approve = approve
        self.prompts: list[str] = []

    async def __call__(self, context, params) -> ElicitResult:
        self.prompts.append(params.message)
        if self.action == "accept":
            return ElicitResult(action="accept", content={"approve": self.approve})
        return ElicitResult(action=self.action)


def call_tool(tool: str, arguments: dict, human: Human | None):
    """Call a tool as an MCP client would; returns the result, or the exception that refused it."""

    async def run():
        kwargs = {"elicitation_callback": human} if human else {}
        async with Client(server.mcp, **kwargs) as client:
            return await client.call_tool(tool, arguments)

    try:
        return asyncio.run(run())
    except BaseException as error:  # the SDK's task group wraps the real error
        leaf = error
        while getattr(leaf, "exceptions", None):
            leaf = leaf.exceptions[0]
        return leaf


@pytest.fixture
def live(fake_reclaimerr, rules_ready):
    """A Reclaimerr with one enabled and one disabled rule, and the rules tools listed."""
    fake_reclaimerr.add_rule(name="Live rule", enabled=True)
    fake_reclaimerr.add_rule(name="Quiet rule", enabled=False)
    server.sync_tools()
    return fake_reclaimerr


def names(fake) -> list[str]:
    return [rule["name"] for rule in fake.rules]


def test_update_of_enabled_rule_prompts_then_applies_when_approved(live):
    human = Human()

    result = call_tool("update_rule", {"rule_id": 1, "name": "Live renamed"}, human)

    assert not isinstance(result, BaseException) and not result.is_error
    assert len(human.prompts) == 1
    assert "Live rule" in human.prompts[0] and "Live renamed" in human.prompts[0]
    assert names(live) == ["Live renamed", "Quiet rule"]


@pytest.mark.parametrize(
    "human",
    [Human("decline"), Human("cancel"), Human("accept", approve=False)],
    ids=["declined", "cancelled", "accepted-but-unticked"],
)
def test_update_of_enabled_rule_changes_nothing_unless_approved(live, human):
    result = call_tool("update_rule", {"rule_id": 1, "name": "SHOULD NOT APPLY"}, human)

    assert not isinstance(result, BaseException)
    assert len(human.prompts) == 1
    assert names(live) == ["Live rule", "Quiet rule"]
    assert live.calls("POST", "/rules/1") == []


def test_update_of_disabled_rule_does_not_prompt(live):
    human = Human()

    result = call_tool("update_rule", {"rule_id": 2, "name": "Quiet renamed"}, human)

    assert not isinstance(result, BaseException) and not result.is_error
    assert human.prompts == []
    assert names(live) == ["Live rule", "Quiet renamed"]


def test_delete_prompts_and_deletes_when_approved(live):
    human = Human()

    call_tool("delete_rule", {"rule_id": 2}, human)

    assert len(human.prompts) == 1 and "Quiet rule" in human.prompts[0]
    assert names(live) == ["Live rule"]


@pytest.mark.parametrize("human", [Human("decline"), Human("cancel")], ids=["declined", "cancelled"])
def test_delete_does_nothing_unless_approved(live, human):
    call_tool("delete_rule", {"rule_id": 2}, human)

    assert len(human.prompts) == 1
    assert names(live) == ["Live rule", "Quiet rule"]


@pytest.mark.parametrize("tool,arguments", [("delete_rule", {"rule_id": 1}), ("update_rule", {"rule_id": 1, "name": "x"})])
def test_fails_closed_when_the_client_cannot_show_prompts(live, tool, arguments):
    """No elicitation support: the SDK refuses before the tool body runs, and nothing changes."""
    outcome = call_tool(tool, arguments, human=None)

    assert isinstance(outcome, BaseException) or outcome.is_error
    assert "elicitation" in str(outcome).lower()
    assert names(live) == ["Live rule", "Quiet rule"]
    assert live.calls("DELETE", "/rules/1") == [] and live.calls("POST", "/rules/1") == []


def test_create_needs_no_prompt_and_saves_disabled(live):
    human = Human()
    definition = {"version": 1, "root": {"type": "group", "op": "and", "children": []}}

    result = call_tool(
        "create_rule",
        {"name": "New", "media_type": "series", "target_scope": "episode", "definition": definition,
         "action": {"auto_delete_enabled": True}},
        human,
    )

    assert not isinstance(result, BaseException) and not result.is_error
    assert human.prompts == []
    created = live.rules[-1]
    assert created["name"] == "New" and created["enabled"] is False
    assert created["action"]["auto_delete_enabled"] is False
