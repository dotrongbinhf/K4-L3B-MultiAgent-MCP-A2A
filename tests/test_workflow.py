from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import InvestigationState, _classify, solve_case


class FakeGateway:
    def __init__(self, *, canceled: bool = True) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.counter = 0
        self.canceled = canceled

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, arguments))
        self.counter += 1
        data: dict[str, Any] = {}
        if tool_name == "get_order":
            data = {
                "order_id": arguments["order_id"],
                "customer_unique_id": "customer_001",
                "order_status": "canceled" if self.canceled else "delivered",
                "order_total": "25.50",
            }
        elif tool_name == "get_order_payments":
            data = {"payments": [{"payment_value": "25.50", "payment_status": "captured"}]}
        elif tool_name == "get_payment_timeline":
            data = {"events": [{"event_type": "captured", "captured_amount": "25.50"}]}
        elif tool_name == "get_refund_timeline":
            data = {"events": []}
        elif tool_name == "get_shipment_summary":
            data = {"status": "not_shipped"}
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{self.counter:020d}",
            "result_hash": "sha256:" + "a" * 64,
            "domain": "policy" if tool_name == "get_policy" else "order",
            "data": data,
        }


def test_solve_case_collects_case_scoped_evidence_and_validates(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    gateway = FakeGateway()

    output = asyncio.run(
        solve_case({"case_id": "CASE_001", "order_id": "ORDER_001"}, gateway, trace)
    )

    contracts.validate_output(output, "test output")
    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["financial_resolution"]["recommended_refund_brl"] == 25.5
    assert output["evidence_refs"]
    assert all(case_id == "CASE_001" for _, case_id, _ in gateway.calls)
    events = [json.loads(line) for line in trace.path.read_text().splitlines()]
    assert any(event["event_type"] == "tool_result_consumed" for event in events)


def test_unresolved_case_is_reported_without_fabricated_evidence(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    gateway = FakeGateway()

    output = asyncio.run(
        solve_case({"case_id": "CASE_002", "description": "payment issue"}, gateway, trace)
    )

    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["evidence_refs"] == []
    assert output["assessment"]["case_status"] == "needs_investigation"


def test_reconciled_payment_is_not_classified_as_mismatch(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    gateway = FakeGateway(canceled=False)

    output = asyncio.run(
        solve_case({"case_id": "CASE_003", "order_id": "ORDER_001"}, gateway, trace)
    )

    assert output["assessment"]["primary_issue"] == "unsupported_claim"
    assert output["payment_analysis"]["verdict"] == "reconciled"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0


def test_failed_refund_overrides_requested_event() -> None:
    state = InvestigationState(case={"case_id": "CASE_004"})
    state.evidence = {
        "get_order": [
            {
                "data": {
                    "order_status": "delivered",
                    "order_purchase_timestamp": "2018-04-10T09:00:00-03:00",
                    "order_delivered_customer_date": "2018-04-19T09:00:00-03:00",
                    "order_estimated_delivery_date": "2018-04-20T09:00:00-03:00",
                }
            }
        ],
        "get_payment_timeline": [
            {
                "data": {
                    "events": [
                        {
                            "event_type": "captured",
                            "event_at": "2018-04-10T10:00:00-03:00",
                            "amount_brl": "52.00",
                            "status": "confirmed",
                        },
                        {
                            "event_type": "captured",
                            "event_at": "2018-01-21T10:00:00-03:00",
                            "amount_brl": "44.50",
                            "status": "confirmed",
                        },
                    ]
                }
            }
        ],
        "get_refund_timeline": [
            {
                "data": {
                    "events": [
                        {
                            "event_type": "refund_requested",
                            "event_at": "2018-04-21T09:00:00-03:00",
                            "amount_brl": "52.00",
                            "status": "failed",
                        },
                    ]
                }
            }
        ],
    }
    issue, verdict, _, captured, _, _ = _classify(state)
    assert (issue, verdict, captured) == ("refund_failed", "refund_failed", 52)


def test_decoy_refund_and_late_event_do_not_override_current_order() -> None:
    state = InvestigationState(
        case={
            "case_id": "CASE_005",
            "order_id": "ORDER_005",
            "opened_at": "2018-01-19T09:00:00-03:00",
        }
    )
    state.evidence = {
        "get_order": [
            {
                "data": {
                    "order_id": "ORDER_005",
                    "order_status": "delivered",
                    "order_purchase_timestamp": "2018-04-23T09:00:00-03:00",
                    "order_delivered_customer_date": "2018-05-02T09:00:00-03:00",
                    "order_estimated_delivery_date": "2018-05-03T09:00:00-03:00",
                }
            }
        ],
        "get_customer_history": [
            {
                "data": {
                    "order_id": "ORDER_005",
                    "order_status": "delivered",
                    "order_purchase_timestamp": "2018-01-07T09:00:00-03:00",
                    "order_delivered_carrier_date": "2018-01-09T09:00:00-03:00",
                    "order_delivered_customer_date": "2018-01-16T09:00:00-03:00",
                    "order_estimated_delivery_date": "2018-01-17T09:00:00-03:00",
                }
            }
        ],
        "get_order_items": [
            {
                "data": [
                    {
                        "order_item_id": "item-001",
                        "shipping_limit_date": "2018-01-10T09:00:00-03:00",
                        "price": "79.00",
                        "freight_value": "10.00",
                    }
                ]
            }
        ],
        "get_payment_timeline": [
            {
                "data": {
                    "events": [
                        {
                            "event_type": "captured",
                            "event_at": "2018-01-07T10:00:00-03:00",
                            "amount_brl": "35.00",
                            "status": "confirmed",
                        },
                        {
                            "event_type": "reconciliation_mismatch",
                            "event_at": "2018-01-07T12:00:00-03:00",
                            "amount_brl": "35.00",
                            "status": "open",
                        },
                        {
                            "event_type": "captured",
                            "event_at": "2018-04-23T10:00:00-03:00",
                            "amount_brl": "89.00",
                            "status": "confirmed",
                        },
                    ]
                }
            }
        ],
        "get_refund_timeline": [
            {
                "data": {
                    "events": [
                        {
                            "event_type": "refund_requested",
                            "event_at": "2018-05-04T09:00:00-03:00",
                            "amount_brl": "89.00",
                            "status": "pending",
                        },
                    ]
                }
            }
        ],
        "get_shipment_summary": [
            {
                "data": {
                    "events": [
                        {
                            "event_type": "delivered_late",
                            "event_at": "2018-03-24T09:00:00-03:00",
                            "actor": "logistics_provider",
                        },
                    ]
                }
            }
        ],
    }
    original_evidence = state.evidence
    state.evidence = {}
    for index, (tool, envelopes) in enumerate(original_evidence.items()):
        for envelope in envelopes:
            state.add_evidence(tool, {**envelope, "evidence_ref": f"ev_{index:020d}"})
    issue, verdict, _, captured, _, _ = _classify(state)
    assert (issue, verdict, captured) == ("payment_mismatch", "capture_mismatch", 35)


def test_workflow_uses_history_episode_before_future_get_order(tmp_path: Path) -> None:
    selected = {
        "order_id": "ORDER_006",
        "order_status": "delivered",
        "order_purchase_timestamp": "2017-12-20T09:00:00-03:00",
        "order_delivered_carrier_date": "2017-12-22T09:00:00-03:00",
        "order_delivered_customer_date": "2018-01-04T09:00:00-03:00",
        "order_estimated_delivery_date": "2017-12-30T09:00:00-03:00",
    }
    future = {
        "order_id": "ORDER_006",
        "order_status": "delivered",
        "order_purchase_timestamp": "2018-05-11T09:00:00-03:00",
        "order_delivered_carrier_date": "2018-05-13T09:00:00-03:00",
        "order_delivered_customer_date": "2018-05-20T09:00:00-03:00",
        "order_estimated_delivery_date": "2018-05-21T09:00:00-03:00",
    }

    class EpisodeGateway:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.refs: dict[str, str] = {}

        async def call(self, tool_name: str, *, case_id: str, **arguments: str):
            self.calls.append(tool_name)
            payloads = {
                "get_order": future,
                "get_customer_history": {
                    "customer_unique_id": "CUSTOMER_006",
                    "orders": [future, selected],
                },
                "get_order_items": [
                    {
                        "order_item_id": "ITEM_006",
                        "seller_id": "SELLER_006",
                        "shipping_limit_date": "2018-05-14T09:00:00-03:00",
                        "price": 79,
                        "freight_value": 10,
                    },
                    {
                        "order_item_id": "ITEM_006",
                        "seller_id": "SELLER_006",
                        "shipping_limit_date": "2017-12-23T09:00:00-03:00",
                        "price": 10,
                        "freight_value": 6,
                    },
                ],
                "get_payment_timeline": {
                    "payments": [
                        {
                            "payment_sequential": 1,
                            "payment_type": "credit_card",
                            "payment_value": 89,
                        },
                        {
                            "payment_sequential": 1,
                            "payment_type": "credit_card",
                            "payment_value": 16,
                        },
                    ],
                    "events": [
                        {
                            "event_type": "captured",
                            "event_at": "2018-05-11T10:00:00-03:00",
                            "amount_brl": 89,
                        },
                        {
                            "event_type": "captured",
                            "event_at": "2017-12-20T10:00:00-03:00",
                            "amount_brl": 16,
                        },
                    ],
                },
                "get_shipment_summary": {
                    "delivered_carrier_at": "2018-05-13T09:00:00-03:00",
                    "delivered_customer_at": "2018-05-20T09:00:00-03:00",
                    "estimated_delivery_at": "2018-05-21T09:00:00-03:00",
                    "events": [
                        {
                            "event_type": "delivered_late",
                            "actor": "logistics_provider",
                            "event_at": "2018-01-04T09:00:00-03:00",
                        }
                    ],
                },
                "get_policy": {
                    "rules": {
                        "late_delivery_logistics": {
                            "case_status": "action_required",
                            "recommended_action": "refund_freight",
                            "refund_brl": 16,
                            "responsible_parties": [
                                {"party_type": "logistics_provider", "party_id": None}
                            ],
                        }
                    }
                },
            }
            ref = f"ev_{len(self.calls):020d}"
            self.refs[tool_name] = ref
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": ref,
                "result_hash": "sha256:" + "a" * 64,
                "domain": "order",
                "data": payloads[tool_name],
            }

    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    gateway = EpisodeGateway()
    output = asyncio.run(
        solve_case(
            {
                "case_id": "CASE_006",
                "opened_at": "2018-01-01T09:00:00-03:00",
                "customer_unique_id_hint": "CUSTOMER_006",
                "candidate_order_ids": ["ORDER_006", "decoy-006"],
                "customer_request": {
                    "claimed_order_id": "ORDER_006",
                    "claims": [{"claim_id": "claim-006", "topic": "refund_failed"}],
                },
            },
            gateway,
            trace,
        )
    )

    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert output["payment_analysis"]["captured_total_brl"] == 16
    assert output["shipment_analysis"]["verdict"] == "logistics_delay"
    assert output["financial_resolution"]["recommended_refund_brl"] == 16
    assert output["entity_resolution"]["resolved_order_ids"] == ["ORDER_006"]
    assert gateway.refs["get_customer_history"] in output["evidence_refs"]
    assert gateway.refs["get_shipment_summary"] in output["evidence_refs"]
    assert output["data_conflicts"]
    assert output["claim_assessments"][0]["verdict"] == "unsupported"
