# L3B Architecture Record

## System overview

`solve_case` is a deterministic coordinator. It creates state for each case, resolves the relevant purchase from customer history and supplied identifiers, delegates evidence collection, applies policy rules, and validates the output. `cli.py` owns the outer `case_received` and `case_finalized` events and publishes a completed batch only after artifact validation.

```text
Input → Coordinator / Customer History → Purchase record selection
                                      → Order and Payment specialists
                                      ↘ Conditional Shipment and Seller specialists
             MCP responses → case-local evidence + evidence refs → Policy decision
                 → Verifier / schema validation → output and trace
```

The specialist roles are ordinary async workflow steps. This keeps routing and tool access inspectable without adding an LLM dependency. Each gateway request passes the current `case_id`; evidence is never shared between case states.

## Agent ownership

| Actor | Responsibility | Tool access | Handoff |
| --- | --- | --- | --- |
| Coordinator | Inspect identifiers, assign tasks, sequence work | None directly | Entity and specialist steps |
| Entity agent | Match candidates to customer history and case opening time | `get_customer_history` | Candidate order IDs to order agent |
| Order agent | Resolve candidate rows and collect order and item context | `get_order`, `get_order_items` | Resolved entities to specialists |
| Payment agent | Collect payment and refund lifecycle evidence | `get_payment_timeline`, `get_refund_timeline` | Payment state to policy agent |
| Shipment agent | Collect delivery timeline and status | `get_shipment_summary` | Shipment state to policy agent |
| Seller agent | Collect seller records associated with the order | `get_sellers` | Seller state to policy agent |
| Policy agent | Retrieve policy and classify with deterministic rules | `get_policy` | Proposed decision to verifier |
| Verifier agent | Check public output schema, bounds, refs, and field consistency | No MCP access | Valid result to coordinator |

Calls within each case run sequentially; `--jobs 1` through `--jobs 4` controls concurrent cases, with one as the default. The first recoverable timeout or temporary service error receives one short retry. Missing required evidence stops the batch instead of replacing existing artifacts with incomplete results.

## Entity resolution and A2A protocol

Case identifiers come from known ID fields and candidate collections. Customer history helps prioritize candidates, and `get_order` checks the selected ID. Candidate lookup is capped at five rows. Multiple valid candidates remain `ambiguous`; no order ID is fabricated.

`episode.py` constructs a separate normalized view of the original responses. For the resolved ID, it selects the latest history purchase at or before `opened_at`, bounds events by the next recorded purchase, and matches item and payment records to that purchase. Refund events are reconciled with the selected payment history. Conflicting records at the same purchase time lower confidence. The customer's claimed issue does not determine the classification.

Handoffs are trace events with actor, target, and a short operational attribute. Every event remains correlated through `case_id`; there are no autonomous loops. Events from concurrent cases may interleave while each case preserves its lifecycle order. Trace contains no private reasoning or prompt content.

## Evidence and conflict lifecycle

The gateway validates each response envelope against `mcp-evidence-response-v1`. The workflow preserves the original envelope and exact `evidence_ref`, and emits `tool_result_consumed` when it consumes a response. Final citations include order, customer history, item, payment, and policy evidence used for the decision, plus relevant shipment and collected refund evidence, up to the schema limit of 30 refs.

Differences between the direct order and the selected history record appear in `data_conflicts`, with source names, selected source, and a resolution code. Normalization changes only the working view; it never rewrites the underlying MCP evidence.

The public scoring policy defines weights and workflow requirements but does not expose the private decision table. Classification uses status and timeline evidence, and confidence reflects source ambiguity and missing evidence. Monetary values use `Decimal`; recommended refunds are bounded by the remaining captured balance and serialized to two decimal places.

## Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace behavior |
| --- | ---: | --- | --- |
| Timeout, temporary transport failure, selected 5xx, or rate limit | 1 retry | Stop batch if required evidence remains unavailable | No fabricated result; successful retries emit consumption events |
| Invalid identifier, authorization, malformed response, or unknown tool | 0 | Record failure; stop if required evidence is unavailable | Successful earlier evidence remains auditable |
| No unique order resolution | 0 | `not_found` or `ambiguous`, `insufficient_evidence` | Entity handoff and later verifier events remain present |
| Ambiguous purchase records | 0 | Reduce confidence and retain source conflicts | Policy and verification events remain tied to the case |

Customer history, order, item, payment, and policy evidence form the core. Shipment evidence is requested for observed delay, refund evidence for eligible orders, and seller records when item evidence lacks seller IDs. Calls remain bounded rather than querying every domain for every case.

The CLI stages all 100 outputs and their trace under `traces/`, validates the complete batch, then replaces the current artifacts. A gateway failure during collection leaves previous outputs, trace, and ZIP available. Packaging is a separate command.

## Active runs and optional evidence cache

`day09 start-run` opens an L3B evidence run through the competition API and records its metadata in ignored `traces/active-run.json`. Open a new run before collecting evidence for a new submission after the previous submission was uploaded.

`day09 run --evidence-cache traces/evidence-run-01.jsonl` optionally records full MCP envelopes and reuses exact case/tool/argument matches. The wrapper validates cached envelopes, preserves genuine refs, rejects refs shared across cases, and coalesces simultaneous identical requests. Only explicit semantic not-found errors can be replayed; generic or transient failures remain visible, and repeated identical error records are deduplicated.

**A cache belongs to one team and one active run. Never reuse it after submission or after `start-run` opens another run.** Use a fresh filename for each new run. The cache does not independently verify server run identity, and an old ref can fail the scorer's provenance gate even when it passes the local schema. Cache files are ignored runtime data and are excluded from the submission ZIP.

## Verification invariants

- Output is validated against `l3b-output-v2.schema.json` before it leaves `solve_case`.
- All output refs are original MCP refs, unique, and each successful response has a matching consumption event.
- Order evidence requests include the current case ID and a case-derived order ID.
- Resolved, rejected, seller, payment, item, and shipment IDs come only from case or gateway evidence.
- Confidence and financial values obey schema bounds; refund values cannot be negative or exceed the remaining captured amount used by the current rules.
- Refund line items sum to the recommended refund by construction.
- Ambiguous source selection lowers reported confidence; missing required evidence prevents batch publication.

## Reproducibility

The workflow uses Python async orchestration, deterministic rules, bounded concurrency, and a one-retry maximum. Runtime dependencies and the CLI entry point are declared in `pyproject.toml`. With Python 3.11+, run `day09 validate-inputs`, `day09 start-run`, `day09 run --jobs 1`, `day09 validate`, then `day09 package --output dist/submission.zip`. Local validation checks structure and invariants; only competition scoring measures private semantic constraints. Credentials remain in `.env` and are excluded from output artifacts.
