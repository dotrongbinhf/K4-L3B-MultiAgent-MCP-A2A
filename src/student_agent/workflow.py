from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from .episode import normalize_episode
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

_ID_KEYS = {
    "order_id",
    "order_ids",
    "candidate_order_id",
    "candidate_order_ids",
    "possible_order_ids",
    "related_order_ids",
    "item_id",
    "item_ids",
    "order_item_id",
    "seller_id",
    "seller_ids",
    "shipment_id",
    "shipment_ids",
    "payment_reference",
    "payment_references",
    "payment_id",
    "payment_ids",
}
_ORDER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,127}$")


@dataclass
class InvestigationState:
    """Case-isolated evidence and decision state shared by the specialist steps."""

    case: dict[str, Any]
    evidence: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    evidence_refs: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    resolved_order_ids: list[str] = field(default_factory=list)
    rejected_candidates: list[str] = field(default_factory=list)
    resolution_ambiguous: bool = False
    normalized: dict[str, list[Any]] = field(default_factory=dict)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    episode_confidence: float = 0.5
    episode_ambiguous: bool = True

    @property
    def case_id(self) -> str:
        return str(self.case["case_id"])

    def add_evidence(self, tool_name: str, response: dict[str, Any]) -> None:
        self.evidence.setdefault(tool_name, []).append(response)
        ref = response["evidence_ref"]
        if ref not in self.evidence_refs:
            self.evidence_refs.append(ref)
        self.refresh_episode()

    def refresh_episode(self) -> None:
        context_case = dict(self.case)
        if self.resolved_order_ids:
            context_case["order_id"] = self.resolved_order_ids[0]
        context = normalize_episode(context_case, self.evidence)
        self.normalized = context.data
        self.conflicts = context.conflicts
        self.episode_confidence = context.confidence
        self.episode_ambiguous = context.ambiguous


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _values(data: Any, keys: set[str]) -> list[Any]:
    found: list[Any] = []
    normalized = {_norm(key) for key in keys}
    for obj in _walk(data):
        for key, value in obj.items():
            if _norm(str(key)) in normalized and value is not None:
                found.append(value)
    return found


def _first(data: Any, *keys: str) -> Any:
    values = _values(data, set(keys))
    return values[0] if values else None


def _id_list(value: Any) -> list[str]:
    candidates: list[str] = []
    if isinstance(value, str):
        candidates.append(value)
    elif isinstance(value, list):
        for item in value:
            candidates.extend(_id_list(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            if _norm(str(key)) in {
                "order_id",
                "candidate_order_id",
                "item_id",
                "order_item_id",
                "seller_id",
                "shipment_id",
                "payment_reference",
                "payment_id",
                "id",
                "candidate_id",
            }:
                candidates.extend(_id_list(item))
    return list(dict.fromkeys(v for v in candidates if _ORDER_ID.fullmatch(v)))


def _case_order_ids(case: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for obj in _walk(case):
        for key, value in obj.items():
            if _norm(str(key)) in _ID_KEYS:
                ids.extend(_id_list(value))
    return list(dict.fromkeys(ids))[:10]


def _data(state: InvestigationState, tool: str) -> list[Any]:
    if tool in state.normalized:
        return state.normalized[tool]
    return [entry.get("data") for entry in state.evidence.get(tool, [])]


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and result >= 0 else None


def _money_values(
    state: InvestigationState, tools: tuple[str, ...], *needles: str
) -> list[Decimal]:
    wanted = tuple(_norm(word) for word in needles)
    result: list[Decimal] = []
    for tool in tools:
        for data in _data(state, tool):
            for obj in _walk(data):
                for key, value in obj.items():
                    name = _norm(str(key))
                    if any(word in name for word in wanted):
                        amount = _decimal(value)
                        if amount is not None:
                            result.append(amount)
    return result


def _order_total(state: InvestigationState) -> Decimal | None:
    direct = _values(_data(state, "get_order"), {"order_total", "total_amount", "total"})
    for value in direct:
        amount = _decimal(value)
        if amount is not None:
            return amount

    purchase = _purchase_time(state)
    rows = [row for data in _data(state, "get_order_items") for row in _walk(data)]
    by_item: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        price = _decimal(row.get("price"))
        freight = _decimal(row.get("freight_value"))
        if price is None or freight is None:
            continue
        item_id = str(row.get("order_item_id", f"row_{index}"))
        current = by_item.get(item_id)
        deadline = _timestamp(row.get("shipping_limit_date"))
        current_deadline = _timestamp(current.get("shipping_limit_date")) if current else None
        if current is None or (
            deadline
            and purchase
            and deadline >= purchase
            and (current_deadline is None or deadline < current_deadline)
        ):
            by_item[item_id] = row
    if by_item:
        return sum(
            (_decimal(row["price"]) or Decimal("0"))
            + (_decimal(row["freight_value"]) or Decimal("0"))
            for row in by_item.values()
        )
    return None


def _purchase_time(state: InvestigationState) -> datetime | None:
    return _timestamp(
        _first(
            _data(state, "get_order"),
            "order_purchase_timestamp",
            "purchase_at",
            "created_at",
        )
    )


def _event_rows(state: InvestigationState, tool: str) -> list[dict[str, Any]]:
    purchase = _purchase_time(state)
    result: list[dict[str, Any]] = []
    for data in _data(state, tool):
        for row in _walk(data):
            if not isinstance(row.get("event_type"), str):
                continue
            occurred = _timestamp(row.get("event_at"))
            if purchase is not None and occurred is not None and occurred < purchase:
                continue
            result.append(row)
    return result


def _payment_events(state: InvestigationState) -> list[dict[str, Any]]:
    return _event_rows(state, "get_payment_timeline")


def _refund_events(state: InvestigationState) -> list[dict[str, Any]]:
    return _event_rows(state, "get_refund_timeline")


def _is_capture(row: dict[str, Any]) -> bool:
    name = _norm(str(row.get("event_type", "")))
    return "captur" in name and not any(
        word in name for word in ("mismatch", "detected", "duplicate")
    )


def _payment_totals(state: InvestigationState) -> tuple[Decimal, Decimal, str]:
    events = _payment_events(state)
    if events:
        captured = sum(
            (
                _decimal(row.get("amount_brl", row.get("captured_amount"))) or Decimal("0")
                for row in events
                if _is_capture(row)
                and _norm(str(row.get("status", ""))) not in {"failed", "rejected"}
            ),
            Decimal("0"),
        )
        signals = " ".join(
            f"{_norm(str(row.get('event_type', '')))} {_norm(str(row.get('status', '')))}"
            for row in events
        )
    else:
        values = _money_values(state, ("get_payment_timeline",), "payment_value")
        captured = sum(values, Decimal("0"))
        signals = _status_text(state, ("get_payment_timeline",))
    refunded_rows = _refund_events(state)
    refunded = sum(
        (
            _decimal(row.get("amount_brl")) or Decimal("0")
            for row in refunded_rows
            if _norm(str(row.get("status", "")))
            in {"completed", "complete", "refunded", "succeeded"}
            or "refund_completed" in _norm(str(row.get("event_type", "")))
        ),
        Decimal("0"),
    )
    return captured, refunded, signals


def _status_text(state: InvestigationState, tools: tuple[str, ...] | None = None) -> str:
    chunks: list[str] = []
    for tool in tools if tools is not None else state.evidence:
        for data in _data(state, tool):
            for obj in _walk(data):
                for key, value in obj.items():
                    if isinstance(value, str) and (
                        "status" in _norm(str(key))
                        or _norm(str(key)) in {"event_type", "payment_type"}
                    ):
                        chunks.append(_norm(value))
    return " ".join(chunks)


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _shipment_late(state: InvestigationState) -> tuple[bool, bool]:
    data = _data(state, "get_shipment_summary") + _data(state, "get_order")
    delivered = _timestamp(
        _first(
            data,
            "delivered_customer_at",
            "delivered_at",
            "delivered_date",
            "order_delivered_customer_date",
        )
    )
    promised = _timestamp(
        _first(
            data,
            "promised_at",
            "estimated_delivery_at",
            "estimated_delivery_date",
            "order_estimated_delivery_date",
        )
    )
    shipped = _timestamp(
        _first(
            data, "delivered_carrier_at", "shipped_at", "handoff_at", "order_delivered_carrier_date"
        )
    )
    purchase = _purchase_time(state)
    limits = [
        _timestamp(row.get("shipping_limit_at", row.get("shipping_limit_date")))
        for value in data + _data(state, "get_order_items")
        for row in _walk(value)
        if isinstance(row, dict)
        and _timestamp(row.get("shipping_limit_at", row.get("shipping_limit_date")))
    ]
    limits = [limit for limit in limits if purchase is None or limit >= purchase]
    deadline = (
        min(limits)
        if limits
        else _timestamp(_first(data, "seller_handoff_deadline", "shipping_limit_date"))
    )
    delivery_late = bool(delivered and promised and delivered > promised)
    seller_late = bool(delivery_late and shipped and deadline and shipped > deadline)
    for event in _event_rows(state, "get_shipment_summary"):
        event_type = _norm(str(event.get("event_type", "")))
        occurred = _timestamp(event.get("event_at"))
        if "late" not in event_type or not delivery_late:
            continue
        if delivered and occurred and abs(occurred - delivered) > timedelta(days=1):
            continue
        if _norm(str(event.get("actor", ""))) == "seller":
            seller_late = True
    return delivery_late, seller_late


def _payment_method_count(state: InvestigationState) -> int:
    methods: set[str] = set()
    amounts = {
        _decimal(row.get("amount_brl", row.get("captured_amount")))
        for row in _payment_events(state)
        if _is_capture(row)
    }
    for data in _data(state, "get_payment_timeline"):
        for obj in _walk(data):
            method = obj.get("payment_type", obj.get("payment_method"))
            amount = _decimal(obj.get("payment_value"))
            if isinstance(method, str) and (not amounts or amount in amounts):
                methods.add(_norm(method))
    return len(methods)


def _policy_rule(state: InvestigationState, issue: str) -> dict[str, Any]:
    for envelope in state.evidence.get("get_policy", []):
        data = envelope.get("data")
        rules = data.get("rules") if isinstance(data, dict) else None
        rule = rules.get(issue) if isinstance(rules, dict) else None
        if isinstance(rule, dict):
            return rule
    return {}


def _find_customer_id(case: dict[str, Any]) -> str | None:
    for obj in _walk(case):
        for key, value in obj.items():
            if _norm(str(key)) in {
                "customer_unique_id",
                "customer_id",
                "customer_unique_id_hint",
            } and isinstance(value, str):
                return value
    return None


def _find_policy_version(case: dict[str, Any]) -> str:
    for obj in _walk(case):
        for key, value in obj.items():
            if (
                "policy" in _norm(str(key))
                and "version" in _norm(str(key))
                and isinstance(value, str)
            ):
                return value
    return "day09-scoring-v2"


def _retryable(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    return any(
        word in name or word in message
        for word in (
            "timeout",
            "connecterror",
            "temporar",
            "429",
            "503",
            "502",
            "504",
            "rate limit",
        )
    )


async def _call(
    state: InvestigationState,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    tool: str,
    actor: str,
    **arguments: str,
) -> dict[str, Any] | None:
    for attempt in range(2):
        try:
            response = await gateway.call(tool, case_id=state.case_id, **arguments)
        except Exception as exc:  # gateway errors are surfaced as varied transport/client types
            if attempt == 0 and _retryable(exc):
                await asyncio.sleep(0.05)
                continue
            state.failures.append(f"{tool}: {type(exc).__name__}")
            return None
        state.add_evidence(tool, response)
        trace.emit(
            case_id=state.case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool,
            evidence_refs=[response["evidence_ref"]],
        )
        return response
    return None


def _entity_ids(state: InvestigationState, field: str, tool_names: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    for tool in tool_names:
        for data in _data(state, tool):
            values.extend(_id_list(_values(data, {field})))
    return list(dict.fromkeys(values))[:20]


async def _resolve_and_collect(
    state: InvestigationState, gateway: EvidenceGateway, trace: TraceWriter
) -> None:
    case = state.case
    candidates = _case_order_ids(case)
    customer_id = _find_customer_id(case)
    history_ids: list[str] = []
    if customer_id:
        trace.emit(
            case_id=state.case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="entity-agent",
            attributes={"task": "resolve_customer_purchase_episode"},
        )
        history = await _call(
            state,
            gateway,
            trace,
            "get_customer_history",
            "entity-agent",
            customer_unique_id=customer_id,
        )
        if history:
            history_ids = _id_list(_values(history.get("data"), {"order_id", "order_ids"}))
    claimed_ids = _id_list(_values(case, {"claimed_order_id"}))
    exact_ids = _id_list(_values(case, {"order_id"}))
    preferred = exact_ids[:1] or claimed_ids[:1]
    if history_ids:
        matched = [candidate for candidate in preferred + candidates if candidate in history_ids]
        preferred = list(dict.fromkeys(matched))[:1]
    checked: list[str] = []
    for candidate in list(dict.fromkeys(preferred + candidates))[:5]:
        trace.emit(
            case_id=state.case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="order-agent",
            attributes={"task": "resolve_candidate"},
        )
        response = await _call(
            state, gateway, trace, "get_order", "order-agent", order_id=candidate
        )
        if response and str(response.get("data", {}).get("order_id")) == candidate:
            checked.append(candidate)
            if candidate in preferred:
                break
        elif candidate in history_ids:
            checked.append(candidate)
            break
    if len(checked) == 1:
        state.resolved_order_ids = checked
        state.rejected_candidates = [
            candidate for candidate in candidates if candidate != checked[0]
        ]
    elif len(checked) > 1:
        state.resolution_ambiguous = True
    else:
        state.rejected_candidates = candidates
    if not state.resolved_order_ids:
        if state.failures and not state.evidence_refs:
            raise RuntimeError(
                f"MCP evidence unavailable for {state.case_id}; preserve prior artifacts"
            )
        return
    state.refresh_episode()

    order_id = state.resolved_order_ids[0]
    for tool, actor in (
        ("get_order_items", "order-agent"),
        ("get_payment_timeline", "payment-agent"),
    ):
        trace.emit(
            case_id=state.case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=actor,
            attributes={"task": tool},
        )
        await _call(state, gateway, trace, tool, actor, order_id=order_id)

    order_status = _status_text(state, ("get_order",))
    delivery_late, seller_late = _shipment_late(state)
    if delivery_late or seller_late:
        trace.emit(
            case_id=state.case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="shipment-agent",
            attributes={"task": "get_shipment_summary"},
        )
        await _call(
            state, gateway, trace, "get_shipment_summary", "shipment-agent", order_id=order_id
        )
    if not any(
        word in order_status for word in ("cancel", "unavailable", "not_available")
    ) and not (delivery_late or seller_late):
        trace.emit(
            case_id=state.case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="payment-agent",
            attributes={"task": "get_refund_timeline"},
        )
        await _call(
            state, gateway, trace, "get_refund_timeline", "payment-agent", order_id=order_id
        )

    if not _entity_ids(state, "seller_id", ("get_order_items",)):
        trace.emit(
            case_id=state.case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="seller-agent",
            attributes={"task": "collect_seller_records"},
        )
        await _call(state, gateway, trace, "get_sellers", "seller-agent", order_id=order_id)

    trace.emit(
        case_id=state.case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="policy-agent",
        attributes={"task": "retrieve_policy"},
    )
    await _call(
        state,
        gateway,
        trace,
        "get_policy",
        "policy-agent",
        policy_version=_find_policy_version(case),
    )
    if not state.evidence.get("get_payment_timeline") or not state.evidence.get("get_policy"):
        raise RuntimeError(f"Required MCP evidence unavailable for {state.case_id}")


def _classify(state: InvestigationState) -> tuple[str, str, list[str], Decimal, Decimal, Decimal]:
    order_status = _status_text(state, ("get_order",))
    item_status = _status_text(state, ("get_order_items", "get_product_context"))
    payment_events = _payment_events(state)
    payment_status = " ".join(
        f"{_norm(str(row.get('event_type', '')))} {_norm(str(row.get('status', '')))}"
        for row in payment_events
    ) or _status_text(state, ("get_payment_timeline", "get_order_payments"))
    refund_events = _refund_events(state)
    dated_refunds = [row for row in refund_events if _timestamp(row.get("event_at"))]
    latest_refund = (
        max(dated_refunds, key=lambda row: _timestamp(row["event_at"]))
        if dated_refunds
        else (refund_events[-1] if refund_events else {})
    )
    refund_status = " ".join(
        _norm(str(latest_refund.get(key, ""))) for key in ("event_type", "status")
    )
    shipment_events = _event_rows(state, "get_shipment_summary")
    shipment_event_text = " ".join(
        f"{_norm(str(row.get('event_type', '')))} {_norm(str(row.get('actor', '')))}"
        for row in shipment_events
    )
    captured, refunded, payment_signals = _payment_totals(state)
    order_total = _order_total(state)
    paid_status = any(
        term in f"{payment_status} {payment_signals}" for term in ("paid", "captured", "approved")
    )
    if not captured and paid_status and order_total is not None:
        captured = order_total
    refundable = max(Decimal("0"), captured - refunded)
    delivery_late, seller_late = _shipment_late(state)

    if "failed" in refund_status:
        issue, verdict = "refund_failed", "refund_failed"
    elif "pending" in refund_status:
        issue, verdict = "refund_pending", "refund_pending"
    elif (
        captured > 0
        and refunded >= captured
        or any(
            token in set(refund_status.split())
            for token in {"refunded", "refund_complete", "refund_completed", "refund_success"}
        )
    ):
        issue, verdict = "unsupported_claim", "refunded"
        refundable = Decimal("0")
    elif "cancel" in order_status and paid_status:
        issue, verdict = "canceled_order_paid", "capture_mismatch"
    elif (
        any(
            term in f"{order_status} {item_status}"
            for term in ("unavailable", "not_available", "out_of_stock")
        )
        and paid_status
    ):
        issue, verdict = "unavailable_order_paid", "capture_mismatch"
    elif any(
        term in payment_status
        for term in ("capture_mismatch", "payment_mismatch", "reconciliation_mismatch")
    ):
        issue, verdict = "payment_mismatch", "capture_mismatch"
    elif "duplicate" in payment_status or (
        order_total is not None
        and captured > order_total
        and len([row for row in payment_events if _is_capture(row)]) > 1
    ):
        issue, verdict = "duplicate_charge", "duplicate_capture"
    elif delivery_late or seller_late:
        seller_delay = seller_late or ("delivered_late seller" in shipment_event_text)
        issue = "late_delivery_seller" if seller_delay else "late_delivery_logistics"
        verdict = "reconciled"
    elif captured > 0 and order_total is not None and captured != order_total:
        issue, verdict = "payment_mismatch", "capture_mismatch"
    elif captured > 0:
        if (
            len([row for row in payment_events if _is_capture(row)]) > 1
            and _payment_method_count(state) > 1
        ):
            issue, verdict = "valid_split_payment", "reconciled"
        else:
            issue, verdict = "unsupported_claim", "reconciled"
    else:
        issue, verdict = "insufficient_evidence", "insufficient_evidence"

    parties: list[str] = []
    if issue == "late_delivery_seller":
        for seller_id in _entity_ids(state, "seller_id", ("get_sellers", "get_order_items")):
            parties.append(seller_id)
        if not parties:
            parties = ["unknown"]
    elif issue == "late_delivery_logistics" or issue in {
        "payment_mismatch",
        "duplicate_charge",
        "refund_pending",
        "refund_failed",
    }:
        parties = ["unknown"]
    elif issue in {"canceled_order_paid", "unavailable_order_paid"}:
        parties = ["platform"]
    return issue, verdict, parties, captured, refunded, refundable


def _cause_code(issue: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", issue.upper()).strip("_")


def _decision_refs(state: InvestigationState, issue: str) -> list[str]:
    tools = {
        "get_order",
        "get_customer_history",
        "get_order_items",
        "get_payment_timeline",
        "get_policy",
    }
    if issue in {"late_delivery_seller", "late_delivery_logistics"}:
        tools.add("get_shipment_summary")
    if state.evidence.get("get_refund_timeline"):
        tools.add("get_refund_timeline")
    return [
        entry["evidence_ref"]
        for tool, entries in state.evidence.items()
        if tool in tools
        for entry in entries
    ][:30]


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinate case-scoped specialists, apply conservative policy rules, and verify output."""
    state = InvestigationState(case=case)
    trace.emit(
        case_id=state.case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="entity-agent",
        attributes={"task": "resolve_order"},
    )
    await _resolve_and_collect(state, gateway, trace)

    trace.emit(
        case_id=state.case_id,
        event_type="handoff",
        actor="coordinator",
        target="policy-agent",
        attributes={"handoff": "evidence_review_complete"},
    )
    issue, payment_verdict, responsible, captured, refunded, refundable = _classify(state)
    policy_rule = _policy_rule(state, issue)
    trace.emit(
        case_id=state.case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=issue,
    )

    confidence = min(0.88, state.episode_confidence) if policy_rule else 0.50
    if state.episode_ambiguous:
        confidence = min(confidence, 0.60)
    if issue in {"refund_pending", "refund_failed"} and not state.evidence.get(
        "get_refund_timeline"
    ):
        confidence -= 0.20
    if not state.evidence.get("get_payment_timeline"):
        confidence -= 0.18
    if issue == "insufficient_evidence":
        confidence = min(confidence, 0.35)
    confidence = round(max(0.0, min(1.0, confidence)), 2)
    entity_confidence = (
        0.95
        if len(state.resolved_order_ids) == 1
        else (0.35 if state.resolution_ambiguous else 0.0)
    )
    resolution_status = (
        "resolved"
        if len(state.resolved_order_ids) == 1
        else ("ambiguous" if state.resolution_ambiguous else "not_found")
    )

    order_data = _data(state, "get_order")
    customer_id = (
        _first(_data(state, "get_customer_history"), "customer_unique_id")
        or _find_customer_id(case)
        or _first(order_data, "customer_unique_id", "customer_id")
    )
    related_orders = list(state.resolved_order_ids)
    if customer_id:
        for data in _data(state, "get_customer_history"):
            related_orders.extend(_id_list(_values(data, {"order_id", "order_ids"})))
    related_orders = list(dict.fromkeys(related_orders))[:20]

    items = _entity_ids(state, "order_item_id", ("get_order_items",))
    if not items:
        items = _entity_ids(state, "item_id", ("get_order_items",))
    sellers = _entity_ids(state, "seller_id", ("get_sellers", "get_order_items"))
    shipments = _entity_ids(state, "shipment_id", ("get_shipment_summary",))
    payments = _entity_ids(
        state, "payment_reference", ("get_order_payments", "get_payment_timeline")
    )
    if not payments:
        payments = _entity_ids(state, "payment_id", ("get_order_payments", "get_payment_timeline"))

    has_shipment = bool(_data(state, "get_order") or _data(state, "get_shipment_summary"))
    shipment_verdict = "insufficient_evidence"
    shipment_data = _data(state, "get_shipment_summary") + _data(state, "get_order")
    shipment_status = _norm(
        str(_first(shipment_data, "order_status", "shipment_status", "status") or "")
    )
    delivered_at = _timestamp(
        _first(
            shipment_data,
            "delivered_customer_at",
            "delivered_at",
            "delivered_date",
            "order_delivered_customer_date",
        )
    )
    estimated_at = _timestamp(
        _first(
            shipment_data,
            "estimated_delivery_at",
            "promised_at",
            "estimated_delivery_date",
            "order_estimated_delivery_date",
        )
    )
    shipment_events = _event_rows(state, "get_shipment_summary")
    shipment_event_status = " ".join(
        f"{_norm(str(row.get('event_type', '')))} {_norm(str(row.get('status', '')))}"
        for row in shipment_events
    )
    timeline_complete = False
    if delivered_at and estimated_at and delivered_at <= estimated_at:
        shipment_verdict = "on_time"
        timeline_complete = True
    if issue == "late_delivery_seller":
        shipment_verdict = "seller_delay"
        timeline_complete = True
    elif issue == "late_delivery_logistics":
        shipment_verdict = "logistics_delay"
        timeline_complete = True
    elif "lost" in shipment_event_status or "lost" in shipment_status:
        shipment_verdict = "lost"
        timeline_complete = True
    elif "returned" in shipment_event_status or "returned" in shipment_status:
        shipment_verdict = "returned"
        timeline_complete = True

    policy_refund = _decimal(policy_rule.get("refund_brl")) if policy_rule else None
    if policy_refund is not None:
        needed_refund = min(policy_refund, refundable)
    elif issue in {"canceled_order_paid", "unavailable_order_paid"}:
        needed_refund = refundable
    elif issue in {"duplicate_charge", "payment_mismatch"}:
        order_total = _order_total(state)
        excess = (
            max(Decimal("0"), captured - order_total) if order_total is not None else Decimal("0")
        )
        needed_refund = min(refundable, excess)
    else:
        needed_refund = Decimal("0")
    needed_refund = needed_refund.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    already_refunded = refunded.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    captured_out = (
        captured.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if any(state.evidence.get(name) for name in ("get_payment_timeline", "get_order_payments"))
        else None
    )
    refunded_out = (
        already_refunded
        if any(
            state.evidence.get(name)
            for name in ("get_refund_timeline", "get_payment_timeline", "get_order_payments")
        )
        else None
    )
    refund_lines = []
    if needed_refund > 0:
        refund_lines.append(
            {
                "reason_code": issue,
                "amount_brl": float(needed_refund),
                "entity_id": state.resolved_order_ids[0] if state.resolved_order_ids else None,
            }
        )
    action = policy_rule.get("recommended_action") if policy_rule else None
    actions = [action] if isinstance(action, str) and action else ["request_additional_evidence"]
    policy_parties = policy_rule.get("responsible_parties") if policy_rule else None
    if not isinstance(policy_parties, list):
        policy_parties = [
            {
                "party_type": p
                if p in {"seller", "platform", "logistics_provider", "payment_provider", "customer"}
                else "unknown",
                "party_id": p if p not in {"platform", "unknown"} else None,
            }
            for p in responsible
        ]
    if sellers:
        policy_parties = [
            {**party, "party_id": sellers[0]}
            if party.get("party_type") == "seller" and party.get("party_id") not in sellers
            else party
            for party in policy_parties
        ]
    policy_status = policy_rule.get("case_status") if policy_rule else None
    if policy_status not in {"action_required", "no_action", "needs_investigation"}:
        policy_status = (
            "needs_investigation"
            if issue == "insufficient_evidence"
            else (
                "no_action"
                if issue in {"unsupported_claim", "valid_split_payment"}
                else "action_required"
            )
        )

    claims = []
    raw_claims = _first(case, "claims", "claim_assessments") or []
    if isinstance(raw_claims, list):
        for index, claim in enumerate(raw_claims[:5]):
            claim_id = (
                claim.get("claim_id", claim.get("id", f"claim_{index + 1}"))
                if isinstance(claim, dict)
                else f"claim_{index + 1}"
            )
            topic = _norm(str(claim.get("topic", ""))) if isinstance(claim, dict) else ""
            if issue == "insufficient_evidence":
                verdict = "insufficient_evidence"
            elif topic == "requested_full_refund":
                if needed_refund <= 0:
                    verdict = "unsupported"
                elif needed_refund >= captured and action != "refund_freight":
                    verdict = "supported"
                else:
                    verdict = "partially_supported"
            elif topic in {
                "valid_split_payment",
                "canceled_order_paid",
                "unavailable_order_paid",
                "late_delivery_seller",
                "late_delivery_logistics",
                "payment_mismatch",
                "duplicate_charge",
                "refund_pending",
                "refund_failed",
                "unsupported_claim",
            }:
                verdict = "supported" if topic == issue else "unsupported"
            else:
                verdict = "unsupported" if issue == "unsupported_claim" else "supported"
            claims.append(
                {
                    "claim_id": str(claim_id)[:64],
                    "verdict": verdict,
                    "confidence": confidence,
                    "evidence_refs": _decision_refs(state, issue),
                }
            )

    output = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": state.case_id,
        "assessment": {
            "primary_issue": issue,
            "secondary_issues": [],
            "case_status": policy_status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": state.resolved_order_ids,
            "item_ids": items,
            "seller_ids": sellers,
            "payment_references": payments,
            "shipment_ids": shipments,
        },
        "entity_resolution": {
            "status": resolution_status,
            "resolved_order_ids": state.resolved_order_ids,
            "rejected_candidates": state.rejected_candidates,
            "confidence": entity_confidence,
        },
        "customer_context": {
            "customer_unique_id": customer_id,
            "related_order_ids": related_orders,
        },
        "shipment_analysis": {
            "verdict": shipment_verdict,
            "late_seller_ids": sellers if issue == "late_delivery_seller" else [],
            "timeline_complete": has_shipment and timeline_complete,
        },
        "payment_analysis": {
            "verdict": payment_verdict,
            "captured_total_brl": float(captured_out) if captured_out is not None else None,
            "refunded_total_brl": float(refunded_out) if refunded_out is not None else None,
            "refundable_total_brl": float(
                max(Decimal("0"), captured - refunded).quantize(Decimal("0.01"))
            )
            if captured_out is not None
            else None,
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": _cause_code(issue), "rank": 1}],
            "responsible_parties": policy_parties,
        },
        "evidence_refs": _decision_refs(state, issue),
        "data_conflicts": state.conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": float(needed_refund),
            "refund_lines": refund_lines,
        },
        "resolution_actions": actions,
    }
    if claims:
        output["claim_assessments"] = claims

    trace.emit(
        case_id=state.case_id,
        event_type="handoff",
        actor="policy-agent",
        target="verifier-agent",
        attributes={"handoff": "decision_ready"},
    )
    line_total = sum((Decimal(str(line["amount_brl"])) for line in refund_lines), Decimal("0"))
    if needed_refund < 0 or needed_refund > refundable:
        raise ValueError(f"case {state.case_id}: recommended refund is outside allowable balance")
    if line_total != needed_refund:
        raise ValueError(f"case {state.case_id}: refund lines do not match the refund total")
    if not set(output["evidence_refs"]).issubset(state.evidence_refs):
        raise ValueError(
            f"case {state.case_id}: output evidence refs do not match consumed MCP refs"
        )
    trace.contracts.validate_output(output, f"case {state.case_id}")
    trace.emit(
        case_id=state.case_id,
        event_type="verification_completed",
        actor="verifier-agent",
        decision_code="valid",
    )
    return output
