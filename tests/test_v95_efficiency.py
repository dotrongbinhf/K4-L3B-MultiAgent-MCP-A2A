from pathlib import Path


def _source(name: str) -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / "src" / "student_agent" / name).read_text(encoding="utf-8")


def test_refund_timeline_is_claim_gated() -> None:
    source = _source("workflow.py")
    assert 'REFUND_EVIDENCE_TOPICS = {' in source
    assert 'if claim_topic_hint in REFUND_EVIDENCE_TOPICS:' in source
    # It must not be part of the unconditional base jobs list.
    base = source[source.index('jobs: list['):source.index('if claim_topic_hint in REFUND_EVIDENCE_TOPICS:')]
    assert 'get_refund_timeline' not in base


def test_tool_discovery_is_cached_by_gateway() -> None:
    gateway = _source("mcp_gateway.py")
    workflow = _source("workflow.py")
    assert 'self._tools: dict[str, Any] | None = None' in gateway
    assert 'async def tool_map(' in gateway
    assert 'tools = await asyncio.wait_for(gateway.tool_map()' in workflow
    assert 'gateway._session.list_tools()' not in workflow


def test_no_independent_payment_call_was_added() -> None:
    # The >90 reference workflow did not need an extra get_order_payments call.
    # Adding it to every case previously cost efficiency without changing the
    # core semantic decision.
    source = _source("workflow.py")
    assert 'get_order_payments' not in source


def test_unsupported_no_action_uses_policy_action_when_available() -> None:
    source = _source("workflow.py")
    assert 'or primary_issue == "unsupported_claim"' in source
