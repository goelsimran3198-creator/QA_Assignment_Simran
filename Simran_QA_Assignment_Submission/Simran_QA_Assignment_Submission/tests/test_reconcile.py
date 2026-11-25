"""Reconciliation: internal auth, recovery, idempotency, no-downgrade (rules 11-13)."""
from tests.helpers import (ApiTestCase, debug_payments, defect, new_order,
                           reconcile, request, webhook)


class ReconcileAuthTests(ApiTestCase):

    def test_reconcile_requires_valid_internal_key_and_changes_nothing_otherwise(self):
        order = new_order("u_rauth")
        for name, headers in {"no key": {}, "wrong key": {"X-Internal-Key": "nope"}}.items():
            with self.subTest(case=name):
                resp = reconcile(order, "SUCCESS", "txn_r", headers=headers)
                self.assertEqual(resp.status, 401, resp)
        self.assertEqual(self.status_of("u_rauth"), "PENDING")
        self.assertEqual(debug_payments(order), [])

    def test_reconcile_unknown_order_fails_and_creates_nothing(self):
        resp = reconcile("ord_missing", "SUCCESS", "txn_x")
        self.assertIn(resp.status, (400, 404), resp)
        self.assertEqual(debug_payments(), [])

    def test_reconcile_rejects_invalid_provider_status(self):
        order = new_order("u_rinv")
        self.assertEqual(reconcile(order, "REFUNDED", "txn_x").status, 400)
        self.assertEqual(self.status_of("u_rinv"), "PENDING")


class ReconcileRecoveryTests(ApiTestCase):

    def test_reconcile_recovers_pending_trial_when_webhook_was_missed(self):
        order = new_order("u_rec1")                      # webhook never arrived
        resp = reconcile(order, "SUCCESS", "txn_rec1")
        self.assertEqual(resp.status, 200, resp)
        self.assertEqual(self.status_of("u_rec1"), "ACTIVE")
        pays = debug_payments(order)
        self.assertEqual([(p["provider_txn_id"], p["provider_status"]) for p in pays], [("txn_rec1", "SUCCESS")])

    @defect("BUG-06")
    def test_reconcile_recovers_failed_trial_when_provider_says_success(self):
        order = new_order("u_rec2")
        webhook(order, "FAILED", "txn_early_fail")       # wrong/early event left the trial FAILED
        self.assertEqual(self.status_of("u_rec2"), "FAILED")
        resp = reconcile(order, "SUCCESS", "txn_rec2")
        self.assertEqual(resp.status, 200, resp)
        self.assertEqual(self.status_of("u_rec2"), "ACTIVE",
                         "BUG-06: reconcile ignored FAILED record although provider reports SUCCESS "
                         "(customer paid but has no access)")


class ReconcileIdempotencyTests(ApiTestCase):

    def test_reconcile_success_twice_is_idempotent(self):
        order = new_order("u_ridem")
        reconcile(order, "SUCCESS", "txn_ridem")
        reconcile(order, "SUCCESS", "txn_ridem")
        self.assertEqual(self.status_of("u_ridem"), "ACTIVE")
        self.assertEqual(len(debug_payments(order)), 1)

    @defect("BUG-07")
    def test_reconcile_same_event_repeatedly_does_not_duplicate_payment_rows(self):
        order = new_order("u_ridem2")
        for _ in range(3):
            self.assertEqual(reconcile(order, "PENDING", "txn_ridem2").status, 200)
        pays = debug_payments(order)
        self.assertEqual(len(pays), 1,
                         "BUG-07: reconciling the same payment 3x created %d payment rows" % len(pays))

    @defect("BUG-07")
    def test_reconcile_then_late_webhook_for_same_txn_records_payment_once(self):
        order = new_order("u_cross")
        reconcile(order, "SUCCESS", "txn_cross")
        webhook(order, "SUCCESS", "txn_cross")           # the missed webhook finally shows up
        pays = [p for p in debug_payments(order) if p["provider_txn_id"] == "txn_cross"]
        self.assertEqual(len(pays), 1,
                         "BUG-07: reconcile + late webhook for the same txn gave %d payment rows" % len(pays))
        self.assertEqual(self.status_of("u_cross"), "ACTIVE")

    def test_reconcile_never_downgrades_active_trial(self):
        order = new_order("u_rdown")
        webhook(order, "SUCCESS", "txn_ok")
        for status in ("PENDING", "FAILED"):
            reconcile(order, status, "txn_stale_" + status)
        self.assertEqual(self.status_of("u_rdown"), "ACTIVE")
