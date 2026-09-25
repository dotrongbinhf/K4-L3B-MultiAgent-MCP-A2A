from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.evidence_cache import RecordingGateway


class FakeGateway:
    def __init__(self, error: str | None = None) -> None:
        self.calls = 0
        self.error = error

    async def list_tools(self) -> list[str]:
        return ["get_order"]

    async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
        self.calls += 1
        await asyncio.sleep(0)
        if self.error:
            raise RuntimeError(self.error)
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{self.calls:020d}",
            "result_hash": "sha256:" + "a" * 64,
            "domain": "order",
            "data": {"order_id": arguments["order_id"]},
        }


@pytest.fixture
def contracts() -> Contracts:
    return Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")


def test_replay_preserves_evidence_and_request_scope(tmp_path: Path, contracts: Contracts) -> None:
    path = tmp_path / "evidence.jsonl"
    live = FakeGateway()
    cache = RecordingGateway(live, path, contracts)

    async def scenario() -> None:
        original, simultaneous = await asyncio.gather(
            cache.call("get_order", case_id="CASE_001", order_id="ORDER_001", detail="full"),
            cache.call("get_order", case_id="CASE_001", detail="full", order_id="ORDER_001"),
        )
        assert original == simultaneous
        original["data"]["order_id"] = "mutated"
        replay = RecordingGateway(live, path, contracts)
        restored = await replay.call(
            "get_order", case_id="CASE_001", order_id="ORDER_001", detail="full"
        )
        assert restored["data"]["order_id"] == "ORDER_001"
        assert live.calls == 1
        other_case = await replay.call(
            "get_order", case_id="CASE_002", order_id="ORDER_001", detail="full"
        )
        assert other_case["evidence_ref"] != restored["evidence_ref"]
        await replay.call("get_order", case_id="CASE_001", order_id="ORDER_002", detail="full")
        assert live.calls == 3
        assert await replay.list_tools() == ["get_order"]

    asyncio.run(scenario())
    assert len(path.read_text().splitlines()) == 3


@pytest.mark.parametrize(
    ("message", "expected_calls"),
    [
        ("MCP tool get_refund_timeline failed: Refund timeline not found for order ORDER_001", 1),
        ("MCP tool get_refund_timeline failed: Error executing tool get_refund_timeline", 2),
        ("503 temporary failure: refund timeline not found", 2),
    ],
)
def test_only_semantic_missing_records_replay(
    tmp_path: Path, contracts: Contracts, message: str, expected_calls: int
) -> None:
    path = tmp_path / "evidence.jsonl"
    live = FakeGateway(message)
    for _ in range(2):
        cache = RecordingGateway(live, path, contracts)
        with pytest.raises(RuntimeError, match=".*"):
            asyncio.run(cache.call("get_refund_timeline", case_id="CASE_001", order_id="ORDER_001"))
    assert live.calls == expected_calls
    assert len(path.read_text().splitlines()) == 1


def test_cache_rejects_ref_reused_across_cases(tmp_path: Path, contracts: Contracts) -> None:
    path = tmp_path / "evidence.jsonl"
    cache = RecordingGateway(FakeGateway(), path, contracts)
    asyncio.run(cache.call("get_order", case_id="CASE_001", order_id="ORDER_001"))
    record = json.loads(path.read_text())
    record["case_id"] = "CASE_002"
    with path.open("a") as stream:
        stream.write(json.dumps(record) + "\n")
    with pytest.raises(ValueError, match="invalid evidence cache record"):
        RecordingGateway(FakeGateway(), path, contracts)
