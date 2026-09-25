"""Reconcile repeated order records against the case's purchase episode.

The returned view is separate from the original MCP envelopes so evidence and
provenance remain intact. Customer statements never determine the selected issue.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass
class EpisodeContext:
    data: dict[str, list[Any]]
    conflicts: list[dict[str, Any]]
    confidence: float
    ambiguous: bool


def _rows(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _rows(child)
    elif isinstance(value, list):
        for child in value:
            yield from _rows(child)


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.tzinfo else result.replace(tzinfo=UTC)


def _purchase(row: dict[str, Any]) -> datetime | None:
    for key in ("order_purchase_timestamp", "purchase_at", "created_at"):
        value = _time(row.get(key))
        if value is not None:
            return value
    return None


def _different(rows: list[dict[str, Any]]) -> bool:
    return any(row != rows[0] for row in rows[1:])


def _amount(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and result >= 0 else None


def _capture(row: dict[str, Any]) -> bool:
    event_type = str(row.get("event_type", "")).lower()
    return "captur" in event_type and not any(
        marker in event_type for marker in ("mismatch", "detected", "reconciliation")
    )


def _references(row: dict[str, Any]) -> set[str]:
    return {
        str(row[key])
        for key in (
            "payment_reference",
            "payment_id",
            "capture_id",
            "capture_reference",
            "original_transaction_id",
        )
        if row.get(key) is not None
    }


def _payment_batches(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """A restarted payment sequence identifies another recorded payment batch."""
    batches: list[list[dict[str, Any]]] = []
    previous: int | None = None
    for row in rows:
        value = row.get("payment_sequential")
        try:
            sequence = int(value) if value is not None else None
        except (ValueError, TypeError):
            sequence = None
        if not batches or (sequence is not None and previous is not None and sequence <= previous):
            batches.append([])
        batches[-1].append(row)
        previous = sequence
    return batches


def normalize_episode(
    case: dict[str, Any], evidence: dict[str, list[dict[str, Any]]]
) -> EpisodeContext:
    """Select a case-relevant order episode and give each tool a consistent view."""
    data = {
        tool: [deepcopy(envelope.get("data")) for envelope in envelopes]
        for tool, envelopes in evidence.items()
    }
    conflicts: list[dict[str, Any]] = []
    ambiguous = False

    def conflict(field: str, sources: list[str], selected: str, code: str) -> None:
        if len(conflicts) < 5 and not any(item["field"] == field for item in conflicts):
            conflicts.append(
                {
                    "field": field,
                    "sources": sources,
                    "selected_source": selected,
                    "resolution_code": code,
                }
            )

    order_rows = [
        row
        for payload in data.get("get_order", [])
        for row in _rows(payload)
        if isinstance(row.get("order_id"), str)
    ]
    request = case.get("customer_request", {})
    order_id = case.get("order_id") or request.get("claimed_order_id")
    if not order_id and order_rows:
        order_id = order_rows[0]["order_id"]
    original_orders = [row for row in order_rows if row.get("order_id") == order_id]
    history_rows = [
        row
        for payload in data.get("get_customer_history", [])
        for row in _rows(payload)
        if row.get("order_id") == order_id and _purchase(row) is not None
    ]
    opened = _time(case.get("opened_at"))
    eligible = [row for row in history_rows if opened is None or _purchase(row) <= opened]
    selected_source = "get_order"
    selected: dict[str, Any] | None = None
    if eligible:
        # Repeated identifiers can include a newer fulfillment snapshot whose
        # promised delivery is still in the future when this complaint opened.
        # Prefer the most recent already-due purchase when one exists.
        already_due = [
            row
            for row in eligible
            if opened is not None
            and (promised := _time(row.get("order_estimated_delivery_date"))) is not None
            and promised <= opened
        ]
        relevant = already_due or eligible
        latest = max(_purchase(row) for row in relevant)
        same_time = [row for row in relevant if _purchase(row) == latest]
        ambiguous = _different(same_time)
        selected = same_time[-1]
        selected_source = "get_customer_history"
    elif original_orders:
        selected = original_orders[-1]
        ambiguous = bool(opened and _purchase(selected) and _purchase(selected) > opened)
    if selected is None:
        return EpisodeContext(data, conflicts, 0.35, True)

    selected = deepcopy(selected)
    purchase = _purchase(selected)
    other_purchases = sorted(
        {_purchase(row) for row in history_rows + original_orders if _purchase(row) is not None}
    )
    next_purchase = next(
        (moment for moment in other_purchases if purchase is not None and moment > purchase),
        None,
    )
    if original_orders and selected_source == "get_customer_history":
        for field in (
            "order_purchase_timestamp",
            "order_status",
            "order_delivered_customer_date",
            "order_delivered_carrier_date",
            "order_estimated_delivery_date",
        ):
            if field in selected and any(
                field in row and row[field] != selected[field] for row in original_orders
            ):
                conflict(
                    field,
                    ["get_order", "get_customer_history"],
                    selected_source,
                    "purchase_episode_matches_case_opened_at",
                )
    data["get_order"] = [selected]

    def in_episode(row: dict[str, Any]) -> bool:
        at = _time(row.get("event_at", row.get("created_at")))
        if at is None or purchase is None:
            return True
        return at >= purchase and (next_purchase is None or at < next_purchase)

    item_rows = [
        row
        for payload in data.get("get_order_items", [])
        for row in _rows(payload)
        if "price" in row
        and ("order_item_id" in row or "item_id" in row)
        and row.get("order_id", order_id) == order_id
    ]
    item_groups: dict[str, list[dict[str, Any]]] = {}
    for index, row in enumerate(item_rows):
        key = str(row.get("order_item_id", row.get("item_id", index)))
        item_groups.setdefault(key, []).append(row)
    selected_items: list[dict[str, Any]] = []
    for group in item_groups.values():
        eligible_items = [
            row
            for row in group
            if (deadline := _time(row.get("shipping_limit_date", row.get("shipping_limit_at"))))
            is None
            or purchase is None
            or deadline >= purchase
        ]
        if not eligible_items:
            continue
        dated = [
            row
            for row in eligible_items
            if _time(row.get("shipping_limit_date", row.get("shipping_limit_at"))) is not None
        ]
        if dated and purchase:
            nearest = min(
                _time(row.get("shipping_limit_date", row.get("shipping_limit_at"))) for row in dated
            )
            chosen = [
                row
                for row in dated
                if _time(row.get("shipping_limit_date", row.get("shipping_limit_at"))) == nearest
            ]
        else:
            chosen = eligible_items
        ambiguous = ambiguous or _different(chosen)
        selected_items.append(deepcopy(chosen[-1]))
    if "get_order_items" in data:
        data["get_order_items"] = [selected_items]

    all_captures = [
        row
        for payload in data.get("get_payment_timeline", [])
        for row in _rows(payload)
        if _capture(row)
    ]
    all_capture_refs = {ref for row in all_captures for ref in _references(row)}
    excluded_amounts: set[Decimal] = set()
    selected_milestones = {
        moment
        for key in ("order_delivered_customer_date", "order_delivered_carrier_date")
        if (moment := _time(selected.get(key))) is not None
    }
    for tool in ("get_payment_timeline", "get_shipment_summary"):
        for payload in data.get(tool, []):
            if isinstance(payload, dict) and isinstance(payload.get("events"), list):
                payload["events"] = [
                    row
                    for row in payload["events"]
                    if in_episode(row)
                    or (
                        tool == "get_shipment_summary"
                        and _time(row.get("event_at")) in selected_milestones
                    )
                ]

    for payload in data.get("get_payment_timeline", []):
        if not isinstance(payload, dict):
            continue
        events = payload.get("events", [])
        captures = [row for row in events if _capture(row)]
        capture_amounts = {
            amount
            for row in captures
            if (amount := _amount(row.get("amount_brl", row.get("captured_amount")))) is not None
        }
        batches = _payment_batches(payload.get("payments", []))
        viable = [
            batch
            for batch in batches
            if batch and all(_amount(row.get("payment_value")) in capture_amounts for row in batch)
        ]
        if viable:
            # Source order breaks a same-time tie; retain uncertainty about it.
            chosen_batch = viable[-1]
            ambiguous = ambiguous or any(batch != chosen_batch for batch in viable)
            payload["payments"] = chosen_batch
            chosen_amounts = {_amount(row.get("payment_value")) for row in chosen_batch}
            excluded_amounts.update(
                amount for amount in capture_amounts if amount not in chosen_amounts
            )
            payload["events"] = [
                row
                for row in events
                if not _capture(row)
                or _amount(row.get("amount_brl", row.get("captured_amount"))) in chosen_amounts
            ]
        elif captures and batches:
            # Payment metadata that cannot be linked to this episode is unsafe.
            payload["payments"] = []
            ambiguous = True

    selected_captures = [
        row
        for payload in data.get("get_payment_timeline", [])
        for row in _rows(payload)
        if _capture(row)
    ]
    selected_refs = {ref for row in selected_captures for ref in _references(row)}
    selected_amounts = {
        amount
        for row in selected_captures
        if (amount := _amount(row.get("amount_brl", row.get("captured_amount")))) is not None
    }
    excluded_amounts.update(
        amount
        for row in all_captures
        if (amount := _amount(row.get("amount_brl", row.get("captured_amount")))) is not None
        and amount not in selected_amounts
    )

    def relevant_refund(row: dict[str, Any]) -> bool:
        references = _references(row)
        if references & selected_refs:
            return True
        if references & all_capture_refs:
            return False
        amount = _amount(row.get("amount_brl"))
        return in_episode(row) and amount not in excluded_amounts

    for payload in data.get("get_refund_timeline", []):
        if isinstance(payload, dict) and isinstance(payload.get("events"), list):
            payload["events"] = [row for row in payload["events"] if relevant_refund(row)]

    for summary in data.get("get_shipment_summary", []):
        if not isinstance(summary, dict):
            continue
        for source_key, target_key in (
            ("order_delivered_customer_date", "delivered_customer_at"),
            ("order_delivered_carrier_date", "delivered_carrier_at"),
            ("order_estimated_delivery_date", "estimated_delivery_at"),
        ):
            if source_key in selected:
                summary[target_key] = selected[source_key]
        for source_key, aliases in (
            ("order_delivered_customer_date", ("delivered_at", "delivered_date")),
            ("order_delivered_carrier_date", ("shipped_at", "handoff_at")),
            ("order_estimated_delivery_date", ("promised_at", "estimated_delivery_date")),
        ):
            if source_key in selected:
                for alias in aliases:
                    if alias in summary:
                        summary[alias] = selected[source_key]
        if "order_status" in selected:
            for key in ("order_status", "shipment_status", "status"):
                if key in summary:
                    summary[key] = selected["order_status"]
        summary["shipping_limits"] = [
            {
                key: value
                for key, value in {
                    "seller_id": row.get("seller_id"),
                    "shipping_limit_at": row.get(
                        "shipping_limit_date", row.get("shipping_limit_at")
                    ),
                }.items()
                if value is not None
            }
            for row in selected_items
        ]
    confidence = 0.68 if ambiguous else (0.96 if eligible else 0.80)
    return EpisodeContext(data, conflicts, confidence, ambiguous)
