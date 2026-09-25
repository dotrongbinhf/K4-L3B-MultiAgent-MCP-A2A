from __future__ import annotations

import asyncio
from typing import Any

from student_agent.mcp_gateway import EvidenceGateway


class FakeContracts:
    def validate_evidence(self, value: Any, label: str) -> None:
        assert value["evidence_ref"].startswith("ev_")
        assert label == "MCP tool get_order"


class FakeSession:
    async def call_tool(self, tool_name: str, *, arguments: dict[str, str]) -> Any:
        assert tool_name == "get_order"
        assert arguments == {"case_id": "CASE_001", "order_id": "ORDER_001"}
        return type(
            "MCPResult",
            (),
            {
                "is_error": False,
                "structuredContent": {"evidence_ref": "ev_" + "a" * 20},
                "content": [],
            },
        )()


def test_gateway_accepts_current_mcp_sdk_error_flag() -> None:
    gateway = EvidenceGateway(FakeSession(), FakeContracts())  # type: ignore[arg-type]
    result = asyncio.run(gateway.call("get_order", case_id="CASE_001", order_id="ORDER_001"))
    assert result["evidence_ref"] == "ev_" + "a" * 20
