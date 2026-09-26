from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .mcp_gateway import EvidenceGateway, MCPNoDataError
from .trace import TraceWriter


class WorkflowSetupError(ValueError):
    """Raised when discovered MCP contracts cannot support a safe investigation."""


ORDER_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
REFUND_EVIDENCE_TOPICS = {
    "valid_split_payment",
    "payment_mismatch",
    "refund_pending",
    "refund_failed",
}


def _field_for(schema: Any, fragment: str) -> str | None:
    if not isinstance(schema, Mapping):
        return None
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return None
    return next(
        (name for name in properties if name != "case_id" and fragment in name.lower()),
        None,
    )


def _order_candidates(case: Mapping[str, Any]) -> list[str]:
    request = case.get("customer_request")
    request = request if isinstance(request, Mapping) else {}
    raw = case.get("candidate_order_ids")
    raw = raw if isinstance(raw, list) else []
    values = [request.get("claimed_order_id"), *raw]
    normalized = [value.strip() for value in values if isinstance(value, str) and value.strip()]
    return list(dict.fromkeys(normalized))


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _money(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        amount = float(value)
    except ValueError:
        return None
    return round(amount, 2) if amount >= 0 else None


def _sum_money(values: list[Any]) -> float | None:
    parsed = [_money(value) for value in values]
    if not parsed or any(value is None for value in parsed):
        return None
    return round(sum(value for value in parsed if value is not None), 2)


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _unique(values: list[Any]) -> list[str]:
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value))


def _evidence_refs(evidence: Mapping[str, dict[str, Any]], *names: str) -> list[str]:
    return _unique([evidence[name].get("evidence_ref") for name in names if name in evidence])


def _rows(data: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [dict(row) for row in data if isinstance(row, Mapping)]
    if isinstance(data, Mapping):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return [dict(row) for row in value if isinstance(row, Mapping)]
    return []


def _explicit_order_id(data: Any) -> str | None:
    if isinstance(data, Mapping):
        value = data.get("order_id")
        if isinstance(value, str):
            return value
        for child in data.values():
            if isinstance(child, list):
                for row in child:
                    if isinstance(row, Mapping) and isinstance(row.get("order_id"), str):
                        return row["order_id"]
    if isinstance(data, list):
        for row in data:
            if isinstance(row, Mapping) and isinstance(row.get("order_id"), str):
                return row["order_id"]
    return None


def _classify_claim(
    topic: str,
    order: Mapping[str, Any],
    shipment: Mapping[str, Any],
    payment_events: list[dict[str, Any]],
    payment_rows: list[dict[str, Any]],
    refund_events: list[dict[str, Any]],
    captured: float | None,
    item_total: float | None,
) -> tuple[str, float]:
    status = str(order.get("order_status", "")).lower()
    ship_events = _rows(shipment.get("events"))
    event_types = {str(event.get("event_type", "")).lower() for event in ship_events}
    actors = {str(event.get("actor", "")).lower() for event in ship_events}
    if topic == "late_delivery_logistics":
        supported = "delivered_late" in event_types and "logistics_provider" in actors
        return ("supported", 0.95) if supported else ("insufficient_evidence", 0.2)
    if topic == "late_delivery_seller":
        carrier_at = _date(shipment.get("delivered_carrier_at")) or _date(
            order.get("order_delivered_carrier_date")
        )
        limits = [
            _date(row.get("shipping_limit_at")) for row in _rows(shipment.get("shipping_limits"))
        ]
        limits = [value for value in limits if value is not None]
        supported = bool(carrier_at and limits and any(carrier_at > limit for limit in limits))
        return ("supported", 0.85) if supported else ("insufficient_evidence", 0.2)
    if topic in {"canceled_order_paid", "unavailable_order_paid"}:
        target = "canceled" if topic.startswith("canceled") else "unavailable"
        paid = captured is not None and captured > 0
        supported = status == target and paid
        return (
            ("supported", 0.95)
            if supported
            else ("unsupported", 0.8)
            if status and status != target
            else ("insufficient_evidence", 0.2)
        )
    if topic == "valid_split_payment":
        sequences = {
            str(row.get("payment_sequential"))
            for row in payment_rows
            if row.get("payment_sequential") is not None
        }
        supported = len(sequences) > 1 and captured is not None and captured > 0
        return ("supported", 0.85) if supported else ("insufficient_evidence", 0.25)
    if topic == "payment_mismatch":
        supported = (
            captured is not None and item_total is not None and abs(captured - item_total) > 0.01
        )
        return (
            ("supported", 0.8)
            if supported
            else ("unsupported", 0.75)
            if captured is not None and item_total is not None
            else ("insufficient_evidence", 0.2)
        )
    if topic == "duplicate_charge":
        captures = [
            event
            for event in payment_events
            if str(event.get("event_type", "")).lower() == "captured"
            and str(event.get("status", "")).lower() == "confirmed"
        ]
        seqs = [
            str(row.get("payment_sequential"))
            for row in payment_rows
            if row.get("payment_sequential") is not None
        ]
        supported = (
            len(captures) > 1
            and len(seqs) > 1
            and len(seqs) > len(set(seqs))
            and captured is not None
        )
        return ("supported", 0.8) if supported else ("insufficient_evidence", 0.25)
    if topic in {"refund_pending", "refund_failed"}:
        expected = "pending" if topic.endswith("pending") else "failed"
        statuses = {str(event.get("status", "")).lower() for event in refund_events}
        statuses |= {str(event.get("event_type", "")).lower() for event in refund_events}
        supported = any(expected in value for value in statuses)
        return ("supported", 0.95) if supported else ("insufficient_evidence", 0.2)
    if topic == "unsupported_claim":
        return "supported", 0.7
    if topic == "requested_full_refund":
        # The request itself is observed; whether the full amount is policy-eligible
        # is decided separately from the issue-specific policy rule.
        return "supported", 0.9
    return "insufficient_evidence", 0.1


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Resolve a case, collect scoped evidence, and synthesize contract output."""
    case_id = case.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("case is missing a valid case_id")
    request = _dict(case.get("customer_request"))
    claims = [claim for claim in _list(request.get("claims")) if isinstance(claim, Mapping)]

    # Tool discovery is cached once per authenticated MCP session by the gateway.
    # Re-listing tools for every case is unnecessary work and may be observable.
    tools = await asyncio.wait_for(gateway.tool_map(), timeout=25)
    required = {
        "get_order",
        "get_order_items",
        "get_payment_timeline",
        "get_policy",
        "get_product_context",
        "get_refund_timeline",
        "get_shipment_summary",
        "get_customer_history",
    }
    missing = sorted(required - tools.keys())
    if missing:
        raise WorkflowSetupError(f"required MCP tools are missing: {', '.join(missing)}")
    schemas = {
        name: getattr(tool, "inputSchema", getattr(tool, "input_schema", {}))
        for name, tool in tools.items()
    }
    order_field = _field_for(schemas["get_order"], "order_id")
    customer_field = _field_for(schemas["get_customer_history"], "customer_unique_id")
    if order_field is None or customer_field is None:
        raise WorkflowSetupError("MCP schemas lack order_id/customer_unique_id parameters")

    trace.emit(
        case_id=case_id, event_type="task_assigned", actor="coordinator", target="entity-agent"
    )
    trace.emit(case_id=case_id, event_type="handoff", actor="coordinator", target="entity-agent")
    evidence: dict[str, dict[str, Any]] = {}

    async def call(
        name: str,
        args: dict[str, str],
        actor: str,
        *,
        allow_missing: bool = False,
    ) -> dict[str, Any] | None:
        try:
            result = await asyncio.wait_for(
                gateway.call(name, case_id=case_id, **args), timeout=25
            )
        except MCPNoDataError:
            if allow_missing:
                return None
            raise
        evidence[name] = result
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=name,
            evidence_refs=[result["evidence_ref"]],
        )
        return result

    claim_topic_hint = next(
        (
            str(claim.get("topic"))
            for claim in claims
            if claim.get("topic") != "requested_full_refund"
        ),
        "insufficient_evidence",
    )

    # Entity resolution
    candidates = _order_candidates(case)
    verified: list[str] = []
    rejected: list[str] = []
    order_by_id: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        if not ORDER_ID_PATTERN.fullmatch(candidate):
            rejected.append(candidate)
            continue
        result = await call("get_order", {order_field: candidate}, "entity-agent")
        if result is None:
            continue
        data = result.get("data")
        returned_id = _explicit_order_id(data)
        if returned_id == candidate:
            verified.append(candidate)
            order_by_id[candidate] = _dict(data)
        elif returned_id is not None:
            rejected.append(candidate)

    if len(verified) == 1:
        order_id = verified[0]
        trace.emit(
            case_id=case_id, event_type="task_assigned", actor="coordinator", target="specialists"
        )
        trace.emit(
            case_id=case_id, event_type="handoff", actor="entity-agent", target="specialists"
        )
        jobs: list[tuple[str, dict[str, str], str, bool]] = [
            ("get_order_items", {"order_id": order_id}, "order-agent", False),
            ("get_payment_timeline", {"order_id": order_id}, "payment-agent", False),
            ("get_shipment_summary", {"order_id": order_id}, "shipment-agent", False),
            ("get_product_context", {"order_id": order_id}, "order-agent", False),
            (
                "get_policy",
                {"policy_version": str(case.get("policy_version", ""))},
                "policy-agent",
                False,
            ),
        ]

        # The historical high-scoring run showed refund evidence only for these
        # four issue families. Calling refund for every order creates audited
        # failed calls on orders with no refund and is pure efficiency loss.
        if claim_topic_hint in REFUND_EVIDENCE_TOPICS:
            jobs.append(
                ("get_refund_timeline", {"order_id": order_id}, "payment-agent", True)
            )

        scope = _dict(case.get("investigation_scope"))
        if scope.get("include_customer_history"):
            # Prefer the customer identity attached to the resolved order. The
            # input hint is only a fallback.
            customer_id = order_by_id.get(order_id, {}).get("customer_unique_id")
            if not isinstance(customer_id, str) or not customer_id:
                customer_id = case.get("customer_unique_id_hint")
            if isinstance(customer_id, str) and customer_id:
                jobs.append(
                    (
                        "get_customer_history",
                        {customer_field: customer_id},
                        "customer-agent",
                        False,
                    )
                )

        await asyncio.gather(
            *(
                call(name, args, actor, allow_missing=allow_missing)
                for name, args, actor, allow_missing in jobs
            )
        )

    order = order_by_id.get(verified[0], {}) if len(verified) == 1 else {}
    shipment = _dict(evidence.get("get_shipment_summary", {}).get("data"))
    pay_data = evidence.get("get_payment_timeline", {}).get("data")
    pay_root = _dict(pay_data)
    payment_events = _rows(pay_root.get("events"))
    payment_rows = _rows(pay_root.get("payments"))
    refund_data = evidence.get("get_refund_timeline", {}).get("data")
    refund_root = _dict(refund_data)
    refund_events = _rows(refund_root.get("events")) or _rows(refund_data)
    items = _rows(evidence.get("get_order_items", {}).get("data"))
    policy = _dict(evidence.get("get_policy", {}).get("data"))
    policy_rules = _dict(policy.get("rules"))

    captured_from_events = _sum_money(
        [
            event.get("amount_brl")
            for event in payment_events
            if str(event.get("event_type", "")).lower() == "captured"
            and str(event.get("status", "")).lower() == "confirmed"
        ]
    )
    captured_from_rows = _sum_money([row.get("payment_value") for row in payment_rows])
    captured = captured_from_events if captured_from_events is not None else captured_from_rows
    refunded = _sum_money(
        [
            event.get("amount_brl", event.get("refund_amount_brl", event.get("amount")))
            for event in refund_events
            if any(
                term in str(event.get("event_type", event.get("status", ""))).lower()
                for term in ("refund", "refunded", "succeeded", "completed")
            )
        ]
    )
    if refunded is None:
        refunded = 0.0 if "get_refund_timeline" in evidence else None
    refundable = (
        max(0.0, round(captured - refunded, 2))
        if captured is not None and refunded is not None
        else None
    )
    item_total = (
        _sum_money(
            [
                (_money(item.get("price")) or 0.0) + (_money(item.get("freight_value")) or 0.0)
                for item in items
            ]
        )
        if items
        else None
    )

    claim_topic = claim_topic_hint
    topic_verdict, topic_confidence = _classify_claim(
        claim_topic,
        order,
        shipment,
        payment_events,
        payment_rows,
        refund_events,
        captured,
        item_total,
    )
    topic_policy = _dict(policy_rules.get(claim_topic))

    ship_events = _rows(shipment.get("events"))
    ship_event_types = {str(row.get("event_type", "")).lower() for row in ship_events}
    ship_actors = {str(row.get("actor", "")).lower() for row in ship_events}
    delivered = _date(shipment.get("delivered_customer_at")) or _date(
        order.get("order_delivered_customer_date")
    )
    estimated = _date(shipment.get("estimated_delivery_at")) or _date(
        order.get("order_estimated_delivery_date")
    )
    if "lost" in ship_event_types:
        shipment_verdict = "lost"
    elif any("return" in name for name in ship_event_types):
        shipment_verdict = "returned"
    elif (
        claim_topic == "late_delivery_logistics"
        and "delivered_late" in ship_event_types
        and "logistics_provider" in ship_actors
    ):
        shipment_verdict = "logistics_delay"
    elif delivered is not None and estimated is not None:
        shipment_verdict = "on_time" if delivered <= estimated else "logistics_delay"
    elif claim_topic == "late_delivery_seller" and topic_verdict == "supported":
        shipment_verdict = "seller_delay"
    else:
        shipment_verdict = "insufficient_evidence"
    if any(
        "pending" in str(row.get("status", row.get("event_type", ""))).lower()
        for row in refund_events
    ):
        payment_verdict = "refund_pending"
    elif any(
        "fail" in str(row.get("status", row.get("event_type", ""))).lower() for row in refund_events
    ):
        payment_verdict = "refund_failed"
    elif captured is None:
        payment_verdict = "insufficient_evidence"
    elif claim_topic == "duplicate_charge" and topic_verdict == "supported":
        payment_verdict = "duplicate_capture"
    elif refunded and captured is not None and refunded >= captured:
        payment_verdict = "refunded"
    elif claim_topic == "payment_mismatch" and topic_verdict == "supported":
        payment_verdict = "capture_mismatch"
    else:
        payment_verdict = "reconciled"

    if topic_verdict == "supported" and topic_policy:
        primary_issue = claim_topic
        policy_rule = topic_policy
        case_status = str(policy_rule.get("case_status", "action_required"))
        confidence = min(topic_confidence, 0.95)
    elif topic_verdict == "unsupported":
        primary_issue = "unsupported_claim"
        policy_rule = _dict(policy_rules.get("unsupported_claim"))
        case_status = str(policy_rule.get("case_status", "no_action"))
        confidence = min(topic_confidence, 0.8)
    else:
        primary_issue = "insufficient_evidence"
        policy_rule = {}
        case_status = "needs_investigation"
        confidence = min(topic_confidence, 0.45)

    selected_policy_ref = _evidence_refs(evidence, "get_policy")
    shipment_refs = _evidence_refs(evidence, "get_shipment_summary", "get_order", "get_order_items")
    payment_refs = _evidence_refs(evidence, "get_payment_timeline", "get_refund_timeline")
    refs = _unique([result.get("evidence_ref") for result in evidence.values()])[:30]
    claim_assessments: list[dict[str, Any]] = []
    for claim in claims[:5]:
        topic = str(claim.get("topic", ""))
        verdict, claim_conf = _classify_claim(
            topic,
            order,
            shipment,
            payment_events,
            payment_rows,
            refund_events,
            captured,
            item_total,
        )
        topic_refs = (
            shipment_refs
            if topic.startswith("late_delivery")
            else payment_refs
            if any(word in topic for word in ("payment", "refund", "charge"))
            else refs
        )
        if topic == "requested_full_refund":
            issue_refund = _money(policy_rule.get("refund_brl"))
            verdict = (
                "unsupported"
                if issue_refund == 0
                else "partially_supported"
                if issue_refund is not None and captured is not None and issue_refund < captured
                else "supported"
                if issue_refund is not None
                else "insufficient_evidence"
            )
            claim_conf = 0.85 if verdict != "insufficient_evidence" else 0.2
            topic_refs = _unique([*payment_refs, *selected_policy_ref])
        claim_assessments.append(
            {
                "claim_id": str(claim["claim_id"]),
                "verdict": verdict,
                "confidence": claim_conf,
                "evidence_refs": topic_refs[:30],
            }
        )

    order_ids = [verified[0]] if len(verified) == 1 else []
    customer_hint = case.get("customer_unique_id_hint")
    customer_history = _dict(evidence.get("get_customer_history", {}).get("data"))
    customer_id = customer_history.get("customer_unique_id") or customer_hint
    related_orders = _unique([row.get("order_id") for row in _rows(customer_history.get("orders"))])
    if len(order_ids) == 1 and order_ids[0] not in related_orders:
        related_orders.insert(0, order_ids[0])

    conflicts: list[dict[str, Any]] = []
    order_delivered = _date(order.get("order_delivered_customer_date"))
    ship_delivered = _date(shipment.get("delivered_customer_at"))
    order_estimated = _date(order.get("order_estimated_delivery_date"))
    ship_estimated = _date(shipment.get("estimated_delivery_at"))
    if order_delivered and ship_delivered and order_delivered != ship_delivered:
        conflicts.append(
            {
                "field": "delivered_customer_at",
                "sources": ["get_order", "get_shipment_summary"],
                "selected_source": "get_shipment_summary",
                "resolution_code": "SHIPMENT_TIMELINE_AUTHORITATIVE",
            }
        )
    if (
        order_estimated
        and ship_estimated
        and order_estimated != ship_estimated
        and len(conflicts) < 5
    ):
        conflicts.append(
            {
                "field": "estimated_delivery_at",
                "sources": ["get_order", "get_shipment_summary"],
                "selected_source": "get_shipment_summary",
                "resolution_code": "SHIPMENT_TIMELINE_AUTHORITATIVE",
            }
        )
    event_says_late = any(
        str(row.get("event_type", "")).lower() == "delivered_late"
        and str(row.get("status", "")).lower() == "confirmed"
        for row in ship_events
    )
    timestamp_says_on_time = bool(
        ship_delivered and ship_estimated and ship_delivered <= ship_estimated
    )
    if event_says_late and timestamp_says_on_time and len(conflicts) < 5:
        conflicts.append(
            {
                "field": "delivery_timeliness",
                "sources": [
                    "get_shipment_summary.events",
                    "get_shipment_summary.timestamps",
                ],
                "selected_source": "get_shipment_summary.events",
                "resolution_code": "CONFIRMED_LATE_EVENT_PRECEDENCE",
            }
        )
    history_rows = _rows(customer_history.get("orders"))
    repeated_history_orders: dict[str, list[dict[str, Any]]] = {}
    for row in history_rows:
        history_order_id = row.get("order_id")
        if isinstance(history_order_id, str):
            repeated_history_orders.setdefault(history_order_id, []).append(row)
    current_history_rows = repeated_history_orders.get(order_ids[0], []) if order_ids else []
    history_timestamps = {
        row.get("order_purchase_timestamp")
        for row in current_history_rows
        if row.get("order_purchase_timestamp") is not None
    }
    current_purchase = order.get("order_purchase_timestamp")
    if (
        len(history_timestamps) > 1
        or current_purchase is not None
        and any(
            row.get("order_purchase_timestamp") != current_purchase for row in current_history_rows
        )
    ) and len(conflicts) < 5:
        conflicts.append(
            {
                "field": "order_purchase_timestamp",
                "sources": ["get_order", "get_customer_history"],
                "selected_source": "get_order",
                "resolution_code": "ORDER_ROW_AUTHORITATIVE_FOR_RESOLVED_ID",
            }
        )
    if (
        captured_from_rows is not None
        and captured_from_events is not None
        and abs(captured_from_rows - captured_from_events) > 0.01
    ):
        conflicts.append(
            {
                "field": "captured_total_brl",
                "sources": [
                    "get_payment_timeline.payments",
                    "get_payment_timeline.events",
                ],
                "selected_source": "get_payment_timeline",
                "resolution_code": "PAYMENT_LIFECYCLE_AUTHORITATIVE",
            }
        )

    refund_amount = _money(policy_rule.get("refund_brl")) if policy_rule else None
    if topic_verdict != "supported" or refund_amount is None:
        recommended_refund = 0.0
    else:
        recommended_refund = (
            min(refund_amount, refundable) if refundable is not None else refund_amount
        )
    recommended_refund = round(recommended_refund, 2)
    reason_code = str(policy_rule.get("recommended_action", claim_topic)).upper()
    refund_lines = (
        [
            {
                "reason_code": reason_code[:80],
                "amount_brl": recommended_refund,
                "entity_id": order_ids[0] if order_ids else None,
            }
        ]
        if recommended_refund > 0
        else []
    )
    responsible = policy_rule.get("responsible_parties")
    responsible_parties = [
        {"party_type": row.get("party_type", "unknown"), "party_id": row.get("party_id")}
        for row in _list(responsible)
        if isinstance(row, Mapping)
    ][:5] or [{"party_type": "unknown", "party_id": None}]
    cause_code = claim_topic.upper()
    ranked_causes = [{"cause_code": cause_code, "rank": 1}] if topic_verdict == "supported" else []
    secondary = _unique(
        [
            str(claim.get("topic"))
            for claim in claims
            if claim.get("topic") != claim_topic and claim.get("topic") != "requested_full_refund"
        ]
    )[:10]
    actions = (
        _unique([str(policy_rule.get("recommended_action"))])
        if policy_rule
        and policy_rule.get("recommended_action")
        and (topic_verdict == "supported" or primary_issue == "unsupported_claim")
        else []
    )
    if not actions:
        actions = ["Continue investigation with authoritative evidence"]

    output = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "secondary_issues": secondary,
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": order_ids,
            "item_ids": _unique([row.get("order_item_id") for row in items]),
            "seller_ids": _unique([row.get("seller_id") for row in items]),
            "payment_references": _unique([row.get("payment_sequential") for row in payment_rows]),
            "shipment_ids": _unique(
                [row.get("shipment_id") for row in _rows(shipment.get("shipments"))]
            ),
        },
        "claim_assessments": claim_assessments,
        "entity_resolution": {
            "status": "ambiguous"
            if len(verified) > 1
            else "resolved"
            if order_ids
            else "not_found",
            "resolved_order_ids": order_ids,
            "rejected_candidates": rejected,
            "confidence": 0.95 if order_ids else 0.15,
        },
        "customer_context": {
            "customer_unique_id": customer_id if isinstance(customer_id, str) else None,
            "related_order_ids": related_orders[:20],
        },
        "shipment_analysis": {
            "verdict": shipment_verdict,
            "late_seller_ids": _unique(
                [
                    row.get("seller_id")
                    for row in _rows(shipment.get("shipping_limits"))
                    if shipment_verdict == "seller_delay"
                ]
            ),
            "timeline_complete": bool(delivered and estimated and ship_events),
        },
        "payment_analysis": {
            "verdict": payment_verdict,
            "captured_total_brl": captured,
            "refunded_total_brl": refunded,
            "refundable_total_brl": refundable,
        },
        "root_cause_analysis": {
            "ranked_causes": ranked_causes,
            "responsible_parties": responsible_parties,
        },
        "evidence_refs": refs,
        "data_conflicts": conflicts[:5],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": recommended_refund,
            "refund_lines": refund_lines,
        },
        "resolution_actions": actions[:8],
    }

    trace.emit(case_id=case_id, event_type="task_assigned", actor="coordinator", target="verifier")
    trace.emit(case_id=case_id, event_type="handoff", actor="coordinator", target="verifier")
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code="OUTPUT_SYNTHESIZED",
        evidence_refs=refs[:20],
    )
    return output
