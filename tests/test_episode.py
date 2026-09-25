from copy import deepcopy

from student_agent.episode import normalize_episode


def envelope(data):
    return {"data": data, "evidence_ref": "ev_preserved_original_reference"}


def order(purchase, **fields):
    return {"order_id": "ORDER_001", "order_purchase_timestamp": purchase, **fields}


def test_history_selects_purchase_before_case_and_preserves_future_resolution_events():
    selected = order(
        "2017-12-20T09:00:00-03:00",
        order_status="delivered",
        order_delivered_customer_date="2018-01-04T09:00:00-03:00",
        order_estimated_delivery_date="2017-12-30T09:00:00-03:00",
    )
    later = order("2018-05-11T09:00:00-03:00", order_status="delivered")
    evidence = {
        "get_order": [envelope(later)],
        "get_customer_history": [envelope({"orders": [later, selected]})],
        "get_shipment_summary": [
            envelope(
                {
                    "delivered_customer_at": "2018-05-20T09:00:00-03:00",
                    "estimated_delivery_at": "2018-05-21T09:00:00-03:00",
                    "events": [
                        {"event_type": "delivered_late", "event_at": "2018-01-04T09:00:00-03:00"}
                    ],
                }
            )
        ],
    }
    original = deepcopy(evidence)
    context = normalize_episode(
        {"order_id": "ORDER_001", "opened_at": "2018-01-01T09:00:00-03:00"}, evidence
    )
    assert context.data["get_order"] == [selected]
    assert context.data["get_shipment_summary"][0]["delivered_customer_at"].startswith("2018-01-04")
    assert len(context.data["get_shipment_summary"][0]["events"]) == 1
    assert context.conflicts[0]["selected_source"] == "get_customer_history"
    assert evidence == original


def test_stale_item_cannot_block_correct_purchase_item():
    evidence = {
        "get_order": [envelope(order("2018-03-01T09:00:00Z"))],
        "get_order_items": [
            envelope(
                [
                    {
                        "order_item_id": "item-1",
                        "price": 12,
                        "shipping_limit_date": "2018-01-05T09:00:00Z",
                    },
                    {
                        "order_item_id": "item-1",
                        "price": 35,
                        "shipping_limit_date": "2018-03-04T09:00:00Z",
                    },
                    {
                        "order_item_id": "item-1",
                        "price": 89,
                        "shipping_limit_date": "2018-06-04T09:00:00Z",
                    },
                ]
            )
        ],
    }
    context = normalize_episode({"order_id": "ORDER_001"}, evidence)
    assert context.data["get_order_items"][0][0]["price"] == 35


def test_same_time_payment_batches_keep_coherent_split_and_allow_partial_refund():
    evidence = {
        "get_order": [envelope(order("2018-02-28T09:00:00Z"))],
        "get_payment_timeline": [
            envelope(
                {
                    "payments": [
                        {
                            "payment_sequential": 1,
                            "payment_value": "52",
                            "payment_type": "credit_card",
                        },
                        {
                            "payment_sequential": 1,
                            "payment_value": "44.5",
                            "payment_type": "credit_card",
                        },
                        {
                            "payment_sequential": 2,
                            "payment_value": "44.5",
                            "payment_type": "voucher",
                        },
                    ],
                    "events": [
                        {
                            "event_type": "captured",
                            "event_at": "2018-02-28T10:00:00Z",
                            "amount_brl": 52,
                        },
                        {
                            "event_type": "captured",
                            "event_at": "2018-02-28T10:00:00Z",
                            "amount_brl": 44.5,
                        },
                        {
                            "event_type": "captured",
                            "event_at": "2018-02-28T11:00:00Z",
                            "amount_brl": 44.5,
                        },
                    ],
                }
            )
        ],
        "get_refund_timeline": [
            envelope(
                {
                    "events": [
                        {
                            "event_type": "refund_requested",
                            "event_at": "2018-03-11T10:00:00Z",
                            "amount_brl": 52,
                        },
                        {
                            "event_type": "refund_requested",
                            "event_at": "2018-03-11T11:00:00Z",
                            "amount_brl": 20,
                        },
                    ]
                }
            )
        ],
    }
    context = normalize_episode({"order_id": "ORDER_001"}, evidence)
    payment = context.data["get_payment_timeline"][0]
    assert [row["amount_brl"] for row in payment["events"]] == [44.5, 44.5]
    assert len(payment["payments"]) == 2
    assert [row["amount_brl"] for row in context.data["get_refund_timeline"][0]["events"]] == [20]
    assert context.ambiguous


def test_refund_reference_can_link_to_old_episode_after_next_purchase():
    first = order("2018-01-01T09:00:00Z")
    second = order("2018-02-01T09:00:00Z")
    evidence = {
        "get_order": [envelope(second)],
        "get_customer_history": [envelope({"orders": [first, second]})],
        "get_payment_timeline": [
            envelope(
                {
                    "events": [
                        {
                            "event_type": "captured",
                            "event_at": "2018-01-01T10:00:00Z",
                            "amount_brl": 89,
                            "payment_id": "pay-1",
                        },
                        {
                            "event_type": "captured",
                            "event_at": "2018-02-01T10:00:00Z",
                            "amount_brl": 52,
                            "payment_id": "pay-2",
                        },
                    ]
                }
            )
        ],
        "get_refund_timeline": [
            envelope(
                {
                    "events": [
                        {
                            "event_type": "refund_completed",
                            "event_at": "2018-02-04T10:00:00Z",
                            "amount_brl": 20,
                            "payment_id": "pay-1",
                        },
                        {
                            "event_type": "refund_failed",
                            "event_at": "2018-02-04T10:00:00Z",
                            "amount_brl": 52,
                            "payment_id": "pay-2",
                        },
                    ]
                }
            )
        ],
    }
    context = normalize_episode(
        {"order_id": "ORDER_001", "opened_at": "2018-01-12T09:00:00Z"}, evidence
    )
    assert context.data["get_refund_timeline"][0]["events"][0]["payment_id"] == "pay-1"
    assert len(context.data["get_refund_timeline"][0]["events"]) == 1


def test_same_time_conflicting_history_is_explicitly_ambiguous():
    first = order("2018-01-01T09:00:00Z", order_status="canceled")
    second = order("2018-01-01T09:00:00Z", order_status="delivered")
    context = normalize_episode(
        {"order_id": "ORDER_001", "opened_at": "2018-01-12T09:00:00Z"},
        {"get_customer_history": [envelope({"orders": [first, second]})]},
    )
    assert context.data["get_order"] == [second]
    assert context.ambiguous
    assert context.confidence < 0.8


def test_identical_payment_batches_do_not_make_selected_episode_ambiguous():
    first = order("2018-01-01T09:00:00Z")
    second = order("2018-02-01T09:00:00Z")
    payment = {"payment_sequential": "1", "payment_value": "89", "payment_type": "credit_card"}
    context = normalize_episode(
        {"order_id": "ORDER_001", "opened_at": "2018-02-12T09:00:00Z"},
        {
            "get_order": [envelope(first)],
            "get_customer_history": [envelope({"orders": [first, second]})],
            "get_payment_timeline": [
                envelope(
                    {
                        "payments": [payment, deepcopy(payment)],
                        "events": [
                            {
                                "event_type": "captured",
                                "event_at": "2018-01-01T10:00:00Z",
                                "amount_brl": "89",
                            },
                            {
                                "event_type": "captured",
                                "event_at": "2018-02-01T10:00:00Z",
                                "amount_brl": "89",
                            },
                        ],
                    }
                )
            ],
        },
    )
    assert not context.ambiguous
    assert context.confidence > 0.9
    assert len(context.data["get_payment_timeline"][0]["events"]) == 1


def test_already_due_episode_precedes_newer_future_fulfillment_snapshot():
    older = order(
        "2018-08-05T09:00:00Z",
        order_status="delivered",
        order_estimated_delivery_date="2018-08-15T09:00:00Z",
        order_delivered_customer_date="2018-08-20T09:00:00Z",
    )
    newer = order(
        "2018-08-14T09:00:00Z",
        order_status="delivered",
        order_estimated_delivery_date="2018-08-24T09:00:00Z",
        order_delivered_customer_date="2018-08-23T09:00:00Z",
    )
    evidence = {
        "get_order": [envelope(newer)],
        "get_customer_history": [envelope({"orders": [newer, older]})],
        "get_payment_timeline": [
            envelope(
                {
                    "events": [
                        {
                            "event_type": "captured",
                            "event_at": "2018-08-05T10:00:00Z",
                            "amount_brl": 16,
                        },
                        {
                            "event_type": "captured",
                            "event_at": "2018-08-14T10:00:00Z",
                            "amount_brl": 89,
                        },
                    ]
                }
            )
        ],
        "get_shipment_summary": [
            envelope(
                {
                    "events": [
                        {"event_type": "delivered_late", "event_at": "2018-08-20T09:00:00Z"},
                        {"event_type": "delivered", "event_at": "2018-08-23T09:00:00Z"},
                    ]
                }
            )
        ],
    }
    context = normalize_episode(
        {"order_id": "ORDER_001", "opened_at": "2018-08-17T09:00:00Z"}, evidence
    )
    assert context.data["get_order"] == [older]
    assert [row["amount_brl"] for row in context.data["get_payment_timeline"][0]["events"]] == [16]
    assert [row["event_at"] for row in context.data["get_shipment_summary"][0]["events"]] == [
        "2018-08-20T09:00:00Z"
    ]
