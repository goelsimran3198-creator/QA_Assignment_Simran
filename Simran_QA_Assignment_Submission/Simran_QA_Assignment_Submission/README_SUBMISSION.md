# Submission - QA Engineer Take-Home (Subscription Reliability Challenge)

## 1. Contents
| File | Purpose |
|------|---------|
| `TEST_STRATEGY.md` | Risk-based strategy, P0/P1/P2, automate vs manual, monitoring |
| `BUGS.md` | 9 defects (4 x P0) + 7 observations, each with steps, expected/actual, evidence, impact |
| `run_tests.py`, `tests/` | Automated API suite (36 tests, Python standard library only) |
| `evidence/` | `test_report.txt` (summary), `full_console_output.txt` (all failure details), `manual_repro_evidence.txt` (curl requests/responses), `validation_against_fixed_reference_server.txt` |
| `.github/workflows/qa.yml` | Example CI pipeline |
| `server.py` | The supplied server, **unmodified** (included only so the folder runs out of the box) |

## 2. How to run (Python 3.9+, no installs needed)
```bash
python3 run_tests.py                     # full suite; starts server.py itself if nothing is listening on :18080
python3 run_tests.py --regression-only   # only tests that pass on the current server (19)
python3 run_tests.py -k reconcile        # filter by name
python3 -m unittest discover -s tests -t . -v   # plain unittest also works (pytest can run the same files)
```
Optional: start the server yourself first (`python3 server.py --reset`); the suite then reuses it.
Env vars: `BASE_URL` (default `http://127.0.0.1:18080`), `SERVER_PATH`.
Exit code is non-zero when anything fails; a readable summary is written to `evidence/test_report.txt`.

**Isolation:** `POST /api/admin/reset` before *every* test, and each test uses its own user ids.
**Assertions:** on stored business state (trial status, payment rows via the debug endpoints), not only HTTP codes.
**Failure output:** every defect test has a message starting with its bug id, e.g.
`BUG-02: late FAILED downgraded a paid, ACTIVE trial`.

## 3. Result on the supplied server
36 tests: **19 pass, 17 fail**. The 17 failures are the automated proof of BUG-01 ... BUG-09 (tagged `@defect`); there are no untagged/unexpected failures. The same suite passes 36/36 on a locally fixed copy of the server, so the failures are real defects and not test mistakes.
Top findings: unsigned webhooks activate trials (BUG-03), late webhooks downgrade paid trials (BUG-02), reconcile cannot repair `FAILED` (BUG-06), duplicate live trials (BUG-01).

## 4. Engineering / release thinking
**How would you run this in CI/CD?**
On every pull request: start the service in an ephemeral environment (fresh DB), run `python3 run_tests.py --regression-only` as a blocking gate, publish `evidence/` as artifacts. Nightly: full suite plus the concurrency tests repeated e.g. 50x to surface rare races, and a smoke run against staging with the real provider sandbox. After deploy to production: read-only smoke (health + one signed synthetic webhook against a test order). See `.github/workflows/qa.yml`.

**Which tests should block a production release?**
All P0: happy-path activation; no downgrade of `ACTIVE` (webhook and reconcile, incl. out-of-order); signature/internal-key enforcement; reconcile recovery of `PENDING` and `FAILED`; single live trial per user+plan (sequential **and** concurrent); unknown order creates nothing. P1 integrity tests (idempotent webhooks, unique txn, amount validation) should also block for a payment flow; P2 may be tracked.

**How would you prevent flaky tests?**
Reset state before each test and use unique ids per test; no sleeps or timing assumptions - assert final state; concurrency tests release all threads together with a barrier and assert invariants (counts), not order; retry only on infrastructure-level connect resets (the stdlib server has a listen backlog of 5 - done in `helpers.request`, deliberately not on dropped-after-request errors); wait for `/health`, never a fixed delay; run concurrency tests repeatedly in nightly CI; quarantine and fix (never ignore) a test that flips.

**If production shows provider=SUCCESS but app=PENDING, what do you inspect first?**
1. `payments` rows for that `order_id`/`provider_txn_id`: did a webhook ever arrive and when (`received_at`)?
2. Webhook gateway/app logs and the provider dashboard's delivery log for that order: 401 (signature), 5xx/timeouts, retries exhausted, or a `200 ignored` because the order was unknown at that moment (see OBS-2).
3. The trial row history: was it ever `ACTIVE` and then downgraded by a late event (BUG-02)? Compare `updated_at` with payment timestamps.
4. Does the user have duplicate trials, so the read model shows the wrong one (BUG-01)? Is the `order_id` the one the provider has?
5. Then repair with reconcile (idempotent) and confirm the state; if reconcile does nothing, the record is probably `FAILED` (BUG-06) and needs a fix, not a retry.

**What metrics / alerts would you add?**
- Webhook: request rate, 2xx/4xx/5xx, p95 latency, signature-failure rate (spike = attack or secret rotation), unknown-order rate, duplicate-delivery rate.
- Correctness (page on non-zero): users with >1 live trial per plan; `provider_txn_id` with >1 payment row; `ACTIVE -> non-ACTIVE` transitions or blocked-downgrade attempts; provider-SUCCESS vs app-not-ACTIVE mismatch.
- Freshness: trials in `PENDING` older than N minutes; age of oldest unreconciled payment; reconcile job success/failure and number of recoveries per run (a sudden rise means webhooks are being lost).
- Infra: DB lock/timeout errors, error-rate per endpoint, queue depth if processing becomes async.

**What would you improve with one more day?**
Model-based/property tests that fire random event sequences (with duplicates and reordering) and check invariants; a contract test for the API/OpenAPI and DB-level constraint tests (UNIQUE `provider_txn_id`, partial unique index on live trials); run against the real provider sandbox; basic load/soak with realistic webhook bursts; migrate to pytest with parametrisation and an HTML/Allure report; mutation testing to check the suite catches regressions; propose adding an event id/timestamp to webhooks so ordering can be resolved properly instead of only "ACTIVE is sticky".

**Which AI/tools did you use?**
Claude (Anthropic's AI assistant) helped draft the test code, the documents and the reproduction scripts, and to build a throw-away fixed copy of the server used only to validate that the tests are correct. All tests were executed locally against the supplied `server.py`; results and evidence in `evidence/` come from those runs. Tools: Python 3 standard library (`unittest`, `urllib`), `curl`, SQLite debug endpoints.

## 5. What I did not do (time box)
No load/performance testing, no real provider sandbox, no UI. Contract gaps (response of safe retry on create, unknown-order webhook code) are handled by asserting state rather than codes and are listed in `BUGS.md` observations.
