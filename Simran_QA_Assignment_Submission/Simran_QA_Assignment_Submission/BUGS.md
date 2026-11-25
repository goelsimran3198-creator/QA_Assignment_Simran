# BUGS.md - Subscription Reliability API

Tested build: the supplied `server.py` (unmodified). All defects below were reproduced manually
(`evidence/manual_repro_evidence.txt`) **and** are proven by automated tests tagged `@defect("BUG-xx")`
(`evidence/test_report.txt`: 36 tests, 19 pass, 17 fail - every failure maps to a defect below).
To prove the tests themselves are not wrong, the same suite was also run against a locally fixed copy of
the server: 36/36 passed (`evidence/validation_against_fixed_reference_server.txt`).

**Severity:** P0 = money / access / security impact, block release. P1 = data integrity or serious
validation gap, fix before release. P2 = robustness / minor.

## Priority summary

| ID | Title | Sev | Rule broken | Automated proof |
|----|-------|-----|-------------|-----------------|
| BUG-03 | Webhook signature is never checked | **P0** | 9 | `test_webhook_without_signature...`, `test_webhook_with_wrong_signature...` |
| BUG-02 | Late `PENDING`/`FAILED` webhook downgrades an `ACTIVE` trial | **P0** | 6 | `test_late_pending...`, `test_late_failed...`, `test_replayed_old_events...` |
| BUG-06 | Reconciliation cannot recover a `FAILED` trial (and still answers 200) | **P0** | 12 | `test_reconcile_recovers_failed_trial_when_provider_says_success` |
| BUG-01 | Duplicate live trials for same user + plan (retry, after activation, concurrent) | **P0** | 1, 14 | `test_repeated_create...`, `test_create_while_trial_is_active...`, `test_parallel_create_requests...` |
| BUG-04 | Webhook is not idempotent: every delivery adds a payment row | P1 | 7 | `test_duplicate_webhook_delivery...`, `test_parallel_duplicate_webhooks...` |
| BUG-05 | `provider_txn_id` is not unique (progression rows; reuse across orders) | P1 | 8 | `test_same_provider_txn_progressing...`, `test_provider_txn_id_cannot_be_reused...` |
| BUG-08 | Amount validation accepts `0`, negatives and booleans | P1 | 2 | `test_amount_must_be_a_positive_integer` |
| BUG-07 | Reconciliation is not idempotent for non-success events / vs late webhook | P2 | 13 | `test_reconcile_same_event_repeatedly...`, `test_reconcile_then_late_webhook...` |
| BUG-09 | JSON body that is not an object (e.g. `[]`) crashes the handler, no HTTP response | P2 | - | `test_valid_json_that_is_not_an_object...` |

BUG-02 + BUG-06 compound each other: BUG-02 makes the wrong `FAILED` state reachable, BUG-06 makes it
unrecoverable by the very process meant to repair it - a customer who paid ends up with no access
*and* the repair job reports success.

---

## BUG-03 - Webhook signature is never validated (P0, security)
- **Preconditions:** a trial in `PENDING`.
- **Steps:** `POST /api/webhooks/payment` for that `order_id` with `status=SUCCESS` and **no** `X-Signature` header (or `X-Signature: wrong`).
- **Expected:** `401`, no state change, no payment row (rule 9).
- **Actual:** `200 {"status":"processed"}`; trial becomes `ACTIVE`; payment row written.
- **Evidence:** `evidence/manual_repro_evidence.txt` E2; tests `test_webhook_without_signature_is_rejected_with_401_and_changes_nothing`, `test_webhook_with_wrong_signature_...`.
- **Impact:** anyone who knows/guesses an `order_id` can activate a subscription for free, or fail someone's real payment. Direct revenue loss and fraud exposure.

## BUG-02 - Late/duplicate `PENDING` or `FAILED` downgrades an `ACTIVE` trial (P0)
- **Preconditions:** trial `ACTIVE` after a `SUCCESS` webhook.
- **Steps:** send `SUCCESS`, then a delayed `FAILED` (or `PENDING`) webhook for the same order.
- **Expected:** trial stays `ACTIVE` (rule 6).
- **Actual:** status is overwritten with the last event (`FAILED` / `PENDING`). The update statement maps any non-success status straight onto the trial.
- **Evidence:** E3 (order `ord_f090be8f873a`: SUCCESS, SUCCESS, FAILED -> trial `FAILED`); tests in `WebhookOutOfOrderTests`.
- **Impact:** paying customers lose access because of normal at-least-once / out-of-order delivery - the most common real-world webhook behaviour. Support tickets, churn, chargebacks.

## BUG-06 - Reconcile cannot recover a `FAILED` trial; still returns 200 "reconciled" (P0)
- **Preconditions:** trial `FAILED` (e.g. via BUG-02 or an early failed attempt), provider says `SUCCESS`.
- **Steps:** `POST /api/reconcile` with `provider_status=SUCCESS`, valid internal key.
- **Expected:** trial becomes `ACTIVE` (rule 12).
- **Actual:** trial stays `FAILED`. The code only touches records that are `PENDING`, and the API still answers `200 {"status":"reconciled"}`, so the repair job believes it succeeded.
- **Evidence:** E4; test `test_reconcile_recovers_failed_trial_when_provider_says_success`.
- **Impact:** customer paid but has no access, and the safety net silently fails - the incident stays invisible until the customer complains.

## BUG-01 - Duplicate live trials for the same user + plan (P0)
- **Preconditions:** none.
- **Steps:** (a) `POST /api/trials` twice with identical body; (b) create, activate via webhook, create again; (c) 15 identical requests in parallel.
- **Expected:** only one live (`PENDING`/`ACTIVE`) trial per user+plan; retry returns the existing one (rules 1, 14).
- **Actual:** every request inserts a new `PENDING` trial. Parallel run: 15 simultaneous identical requests -> 15 live trials, all `201`. No uniqueness check, `DB_LOCK` is defined but never used, no DB constraint.
- **Evidence:** E1 (`user_123` has `ord_6cb6...` and `ord_075b...`); tests `test_repeated_create_...`, `test_create_while_trial_is_active_...`, `test_parallel_create_requests_...`.
- **Impact:** double-trial/double-billing risk, orphan orders, and `GET /api/subscriptions/{user}` returns the *newest* row, so an activated customer can be shown a fresh `PENDING` duplicate instead of their `ACTIVE` subscription. Fix needs both an application check and a DB-level partial unique index, otherwise the race remains.

## BUG-04 - Webhook is not idempotent (P1)
- **Steps:** deliver the identical `SUCCESS` webhook 3 times (or 15 times in parallel).
- **Expected:** one payment record, state unchanged after the first (rule 7).
- **Actual:** 3 payment rows (15 in the parallel case).
- **Evidence:** E3 payments list; tests `test_duplicate_webhook_delivery_is_idempotent`, `test_parallel_duplicate_webhooks_create_one_payment_and_one_activation`.
- **Impact:** inflated payment/revenue figures, broken reconciliation counts, and any downstream "on SUCCESS row" logic (invoices, emails) would fire multiple times.

## BUG-05 - `provider_txn_id` does not identify one payment (P1)
- **Steps:** (a) send `txn_prog` as `PENDING`, then as `SUCCESS`; (b) send the same `txn_shared` for two different orders.
- **Expected:** one payment record per `provider_txn_id` whose status progresses; a txn already used by another order must not create/activate anything (rule 8).
- **Actual:** (a) two rows (`PENDING`, `SUCCESS`); (b) two rows for two orders and the unrelated order becomes `ACTIVE`.
- **Evidence:** tests `test_same_provider_txn_progressing_pending_to_success_is_one_payment_record`, `test_provider_txn_id_cannot_be_reused_for_a_different_order`.
- **Impact:** one provider payment can activate several subscriptions; payment ledger is unreliable. No UNIQUE constraint exists on `provider_txn_id`.

## BUG-08 - Amount validation too weak (P1)
- **Steps:** `POST /api/trials` with `amount` = `0`, `-500`, `true`.
- **Expected:** `400` (positive integer paise, rule 2).
- **Actual:** `201`; stored amounts `0`, `-500` and `1` (JSON `true` is an `int` in Python).
- **Evidence:** E5; test `test_amount_must_be_a_positive_integer`.
- **Impact:** zero/negative-value orders reach the payment provider; booleans silently become 1 paise.

## BUG-07 - Reconcile not idempotent for non-success statuses / vs late webhook (P2)
- **Steps:** reconcile the same `PENDING` event 3 times; or reconcile `SUCCESS` then receive the same txn's webhook.
- **Expected:** one payment row (rule 13).
- **Actual:** 3 rows, respectively 2 rows (reconcile row + webhook row).
- **Evidence:** tests `test_reconcile_same_event_repeatedly_does_not_duplicate_payment_rows`, `test_reconcile_then_late_webhook_for_same_txn_records_payment_once`.
- **Impact:** duplicated ledger rows from a job that is expected to be re-run safely. (`SUCCESS` repeated on an already `ACTIVE` trial *is* safe today - covered by a passing test.)

## BUG-09 - Non-object JSON body crashes the request handler (P2)
- **Steps:** `POST /api/trials` (also webhook, reconcile) with body `[]`.
- **Expected:** `400 invalid_request`.
- **Actual:** unhandled `AttributeError` (`list` has no `.get`); the connection is closed with **no HTTP response** (`curl` exit 52) and a stack trace in the server log.
- **Evidence:** E6; test `test_valid_json_that_is_not_an_object_returns_400_instead_of_crashing`.
- **Impact:** clients and providers see network errors instead of a clear 4xx, which triggers retries/noise; log spam, and a cheap way to hammer error paths.

---

## Observations (design / production-readiness risks, no failing test)
| # | Observation | Why it matters |
|---|-------------|----------------|
| OBS-1 | `GET /api/subscriptions/{user_id}` and `/api/debug/*` need no authentication (E7 returns every payment). | Any caller can read any user's subscription (IDOR) and all payment data. Debug endpoints must be disabled/protected outside local use. |
| OBS-2 | Webhook for an unknown `order_id` returns `200 "ignored"` (state is correctly untouched, rule 10). | If the webhook beats the trial's commit, the provider sees success and never retries - the event is lost. Prefer `404`/`202` or park it as an orphan for reconciliation. |
| OBS-3 | `provider_txn_id` is optional (verified: a SUCCESS webhook without it returns 200 and stores `NULL`). | Such payments cannot be de-duplicated at all. Should be a required field. |
| OBS-4 | "Signature" is a static shared string, no HMAC of the body and no timestamp. | Replayable and leak-prone even after BUG-03 is fixed. |
| OBS-5 | `GET /api/subscriptions/{user}` returns only the newest trial of any plan. | A user with monthly and yearly trials only sees one; contract does not say which is intended. |
| OBS-6 | All handlers do unlocked read-modify-write; no transactions, no unique indexes. | Root cause shared by BUG-01/04/05; fix at DB level (UNIQUE `provider_txn_id`, partial unique index on live trials) and use transactions. |
| OBS-7 | Under a burst of >5 simultaneous connections some are reset by the server's listen backlog. | Test-infra noise handled in the helper (retry on connect-time resets only), but worth knowing when load-testing this server. |
