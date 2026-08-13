"""Tests for the tool annotations clients group permissions by.

These assertions exist because the hints are a safety surface, not decoration:
a client that reads `readOnlyHint` decides from it whether a call needs the
user's approval. The failure mode worth guarding is quiet — a tool added with
the decorator's bare defaults would claim to be read-only-adjacent and slip
into the wrong permission group with nothing raising an error.
"""

from __future__ import annotations

import asyncio

import pytest
from mcp.types import Tool

from directa_mcp import server

#: The tools that only ask Darwin questions. Written out rather than derived
#: from the `get_` prefix, so that renaming a tool cannot silently reclassify
#: it and so that the two members with no prefix are stated explicitly.
READ_ONLY = {
    "check_connection",
    "get_account_balance",
    "get_availability",
    "get_candle_data_range",
    "get_daily_candles",
    "get_darwin_status",
    "get_intraday_candles",
    "get_orders",
    "get_portfolio_overview",
    "get_position",
    "get_positions",
    "get_tick_data",
}

#: Everything that puts a command on the wire, with the hints it must carry.
#: `preview_limit_order` is the interesting row: it reads like a query and
#: places nothing, but it does send ACQAZ, so it is not read-only.
WRITES = {
    "start_darwin": {"destructiveHint": False, "idempotentHint": True},
    "preview_limit_order": {"destructiveHint": False, "idempotentHint": True},
    "place_limit_order": {"destructiveHint": True, "idempotentHint": False},
    "modify_order": {"destructiveHint": True, "idempotentHint": False},
    "cancel_order": {"destructiveHint": True, "idempotentHint": True},
    "cancel_all_orders": {"destructiveHint": True, "idempotentHint": True},
}


@pytest.fixture(scope="module")
def tools() -> dict[str, Tool]:
    return {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}


class TestEveryToolIsClassified:
    def test_the_registry_matches_the_expected_tools(self, tools: dict[str, Tool]) -> None:
        """A new tool has to be classified here, which is the point: the
        decorator's defaults would otherwise pass unnoticed."""
        assert set(tools) == READ_ONLY | set(WRITES)

    def test_none_are_missing_annotations(self, tools: dict[str, Tool]) -> None:
        assert [name for name, tool in tools.items() if tool.annotations is None] == []

    def test_open_world_is_stated_rather_than_left_absent(self, tools: dict[str, Tool]) -> None:
        """Absent is the value a client may read either way, and reading it as a
        closed world understates what these tools reach."""
        assert all(tool.annotations.openWorldHint is True for tool in tools.values())


class TestReadOnlyTools:
    def test_they_declare_themselves_read_only(self, tools: dict[str, Tool]) -> None:
        assert [name for name in READ_ONLY if not tools[name].annotations.readOnlyHint] == []

    def test_they_are_not_also_marked_destructive(self, tools: dict[str, Tool]) -> None:
        """The hint is defined as meaningful only where readOnlyHint is false, so
        a read-only tool reporting `destructiveHint: true` is a contradiction a
        client has to resolve on its own."""
        assert [name for name in READ_ONLY if tools[name].annotations.destructiveHint] == []


class TestWritingTools:
    def test_none_claim_to_be_read_only(self, tools: dict[str, Tool]) -> None:
        """The assertion that actually matters. Anything here reaching Darwin
        with `readOnlyHint: true` would be auto-approved by a client that gates
        on the hint — for orders, with real money."""
        assert [name for name in WRITES if tools[name].annotations.readOnlyHint] == []

    @pytest.mark.parametrize("name", sorted(WRITES))
    def test_the_hints_are_the_declared_ones(self, name: str, tools: dict[str, Tool]) -> None:
        annotations = tools[name].annotations
        actual = {hint: getattr(annotations, hint) for hint in WRITES[name]}
        assert actual == WRITES[name]

    def test_the_order_tools_are_all_destructive(self, tools: dict[str, Tool]) -> None:
        """Placing, modifying and cancelling all move real money, whichever
        direction they move it in."""
        orders = ["place_limit_order", "modify_order", "cancel_order", "cancel_all_orders"]
        assert [name for name in orders if not tools[name].annotations.destructiveHint] == []
