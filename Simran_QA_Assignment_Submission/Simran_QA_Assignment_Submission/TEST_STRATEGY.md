# TEST_STRATEGY.md - Subscription / payment-webhook flow

Scope: `POST /api/trials`, `POST /api/webhooks/payment`, `POST /api/reconcile`, `GET /api/subscriptions/{user}`
and the `trials` / `payments` tables. Rule numbers refer to the 14 business rules in the assignment README.

## 1. What can hurt the business most (risk ranking)
| # | Risk | Why it is dangerous |
|---|------|--------------------|
| R1 | Paid customer without access (wrong downgrade, lost webhook, reconcile that cannot repair) | Direct churn/support/chargeback cost; the worst trust failure |
| R2 | Access without payment (forged/unsigned webhook, txn reuse) | Revenue loss and fraud |
| R3 | Duplicate trials / duplicate payments (retries, double click, parallel requests, at-least-once delivery) | Double charges, wrong reporting, duplicated side effects |
| R4 | Silent failure (API says 200 but state not fixed) | Incident invisible until a customer complains |
| R5 | Data integrity drift between provider and our DB | Reconciliation and finance become unreliable |
| R6 | Open internal/debug endpoints, weak input validation | Data leak, garbage data, error-path noise |

The distinguishing property of this system: webhooks are **at-least-once, delayed, duplicated and unordered**,
so most important tests are about *sequences of events* and *final state*, not single requests.

## 2. Prioritised scenarios
**P0 - blocks release** (money, access, security)
- Success webhook activates the trial and records exactly one payment (rules 4, 5).
- `ACTIVE` is never downgraded by later `PENDING`/`FAILED` webhooks or reconcile, including replayed/out-of-order sequences (6, 13).
- Missing/invalid `X-Signature` -> 401 and *zero* state change (9). Missing/invalid internal key on reconcile and reset -> 401 (11).
- Reconcile recovers `PENDING` **and** `FAILED` records when provider says `SUCCESS` (12).
- One live trial per user+plan: sequential retry, retry after activation, and parallel requests (1, 14).
- Unknown `order_id` creates nothing (10).

**P1 - fix before release** (integrity, validation)
- Duplicate webhook delivery (sequential and parallel) -> one payment record (7).
- One `provider_txn_id` = one payment; progression `PENDING -> SUCCESS` of the same txn updates, not duplicates; same txn on another order is rejected (8).
- Amount must be a positive integer: 0, negative, boolean, string, float, null (2).
- Reconcile + late webhook for the same txn -> single payment (13).
- `FAILED -> SUCCESS` for the same order (payment retry) ends `ACTIVE`; a new trial is allowed after a failed one.

**P2 - nice to have / robustness**
- Malformed JSON and non-object JSON -> clean 400, never a dropped connection.
- Case-insensitivity of status, extra/unknown fields, very long ids, unicode user ids.
- `GET /subscriptions` for unknown user -> 404; response shape.

## 3. Coverage by test type
- **Happy path:** create -> webhook SUCCESS -> ACTIVE; reconcile as missed-webhook recovery.
- **Negative:** invalid plan/amount/user, invalid status, bad JSON, bad signature/key, unknown order.
- **Retries / duplicates / out-of-order:** repeated create, repeated webhook, SUCCESS->PENDING, SUCCESS->FAILED, replayed old events, FAILED->SUCCESS.
- **Concurrency:** N simultaneous creates (same user+plan) and N simultaneous identical webhooks, released together with a barrier. Assertions are on end-state invariants (count of live trials / payment rows), which stay deterministic even though the interleaving is not.
- **Reconciliation:** auth, recovery from PENDING and FAILED, idempotency, never downgrade, unknown order, invalid status, interaction with a late webhook.
- **Data integrity (DB-level assertions):** after every scenario assert rows in `trials` and `payments` via the debug endpoints (same data as the SQLite file): row counts, `status`, `amount`, `provider_txn_id` uniqueness, no rows created by rejected calls. HTTP status alone is never the only assertion.
- **Security:** signature enforcement (missing/wrong), internal key on reconcile and reset, unauthenticated read endpoints (reported as observation), replay/forgery risk of a static secret.

## 4. Automate vs. manual
**Automate (done, `run_tests.py`):** everything above that is deterministic - state machine, idempotency, auth, validation, race invariants. Runs in seconds, no third-party dependencies, DB reset before each test.
**Automate next:** randomised event-sequence (model-based) test: any permutation of PENDING/FAILED/SUCCESS events, with duplicates, must satisfy "ACTIVE is sticky, payments unique by txn".
**Manual / exploratory:** the real provider sandbox (true delays, real retry schedule, real signature scheme), unusual client behaviour, production-like volume, reviewing logs/alerts for readability, anything needing human judgement (support copy, refunds).

## 5. Observability to check after release
Dashboards/alerts (details in `README_SUBMISSION.md`): webhook 2xx/4xx/5xx and latency; signature failures; trials stuck in `PENDING` > N minutes; provider-SUCCESS vs app-ACTIVE mismatch count (must trend to 0); count of users with >1 live trial per plan (must be 0); payment rows per `provider_txn_id` > 1 (must be 0); blocked-downgrade counter; unknown-order webhooks; reconcile job runs/recoveries/failures.
Release strategy: canary/small cohort, watch the mismatch and duplicate counters for 24h, keep a run-book to re-run reconcile (safe because idempotent).

## 6. Assumptions / out of scope
"Live" trial = `PENDING` or `ACTIVE`. The contract does not define the response of unknown-order webhooks or of a safe retry on create, so tests accept `200/201` and `200/404` respectively and assert state instead. Load/performance, UI and provider-side behaviour are out of scope for this time box.
