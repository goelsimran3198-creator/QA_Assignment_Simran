"""Payment webhooks: state transitions, signature, idempotency, out-of-order (rules 4-10)."""
from tests.helpers import (ApiTestCase, debug_payments, debug_trials, defect,
                           new_order, request, webhook)


class WebhookStateTests(ApiTestCase):

    def test_success_webhook_activates_trial_and_records_payment(self):
        order = new_order("u_ok")
        resp = webhook(order, "SUCCESS", "txn_ok")
        self.assertEqual(resp.status, 200, resp)
        self.assertEqual(self.status_of("u_ok"), "ACTIVE")
        pays = debug_payments(order)
        self.assertEqual([(p["provider_txn_id"], p["provider_status"]) for p in pays], [("txn_ok", "SUCCESS")])

    def test_failed_webhook_marks_pending_trial_failed(self):
        order = new_order("u_fail")
        self.assertEqual(webhook(order, "FAILED", "txn_fail").status, 200)
        self.assertEqual(self.status_of("u_fail"), "FAILED")

    def test_pending_webhook_keeps_trial_pending(self):
        order = new_order("u_pend")
        self.assertEqual(webhook(order, "PENDING", "txn_pend").status, 200)
        self.assertEqual(self.status_of("u_pend"), "PENDING")

    def test_failed_then_success_for_same_order_recovers_to_active(self):
        order = new_order("u_retrypay")
        webhook(order, "FAILED", "txn_a")
        webhook(order, "SUCCESS", "txn_b")        # user retried payment, new provider txn
        self.assertEqual(self.status_of("u_retrypay"), "ACTIVE")

    def test_invalid_webhook_payloads_are_rejected_without_state_change(self):
        order = new_order("u_inv")
        bodies = {
            "unsupported status": {"order_id": order, "provider_txn_id": "t", "status": "REFUNDED"},
            "missing status": {"order_id": order, "provider_txn_id": "t"},
            "missing order_id": {"provider_txn_id": "t", "status": "SUCCESS"},
        }
        for name, body in bodies.items():
            with self.subTest(case=name):
                resp = request("POST", "/api/webhooks/payment", body, {"X-Signature": "qa-secret"})
                self.assertEqual(resp.status, 400, resp)
        self.assertEqual(self.status_of("u_inv"), "PENDING")
        self.assertEqual(debug_payments(order), [])

    def test_unknown_order_id_creates_no_payment_or_subscription_state(self):
        resp = webhook("ord_does_not_exist", "SUCCESS", "txn_ghost")
        self.assertIn(resp.status, (200, 404), resp)      # contract does not prescribe the code
        self.assertEqual(debug_payments(), [], "rule 10: unknown order must not create a payment")
        self.assertEqual(debug_trials(), [], "rule 10: unknown order must not create a trial")


class WebhookSecurityTests(ApiTestCase):

    @defect("BUG-03")
    def test_webhook_without_signature_is_rejected_with_401_and_changes_nothing(self):
        order = new_order("u_nosig")
        resp = webhook(order, "SUCCESS", "txn_nosig", headers={})
        self.assertEqual(resp.status, 401, "BUG-03: unsigned webhook got %r" % resp)
        self.assertEqual(self.status_of("u_nosig"), "PENDING",
                         "BUG-03: unsigned webhook activated the trial (free subscription for anyone)")
        self.assertEqual(debug_payments(order), [], "BUG-03: unsigned webhook wrote a payment row")

    @defect("BUG-03")
    def test_webhook_with_wrong_signature_is_rejected_with_401_and_changes_nothing(self):
        order = new_order("u_badsig")
        resp = webhook(order, "SUCCESS", "txn_badsig", headers={"X-Signature": "wrong"})
        self.assertEqual(resp.status, 401, "BUG-03: wrongly signed webhook got %r" % resp)
        self.assertEqual(self.status_of("u_badsig"), "PENDING")
        self.assertEqual(debug_payments(order), [])


class WebhookIdempotencyTests(ApiTestCase):

    @defect("BUG-04")
    def test_duplicate_webhook_delivery_is_idempotent(self):
        order = new_order("u_dup")
        statuses = [webhook(order, "SUCCESS", "txn_dup").status for _ in range(3)]
        self.assertEqual(statuses, [200, 200, 200])
        self.assertEqual(self.status_of("u_dup"), "ACTIVE")
        pays = debug_payments(order)
        self.assertEqual(len(pays), 1,
                         "BUG-04: same event delivered 3x produced %d payment rows" % len(pays))

    @defect("BUG-05")
    def test_same_provider_txn_progressing_pending_to_success_is_one_payment_record(self):
        order = new_order("u_prog")
        webhook(order, "PENDING", "txn_prog")
        webhook(order, "SUCCESS", "txn_prog")
        pays = [p for p in debug_payments(order) if p["provider_txn_id"] == "txn_prog"]
        self.assertEqual(len(pays), 1,
                         "BUG-05: one provider_txn_id must be ONE payment; found rows: %s"
                         % [p["provider_status"] for p in pays])
        self.assertEqual(pays[0]["provider_status"], "SUCCESS")
        self.assertEqual(self.status_of("u_prog"), "ACTIVE")

    @defect("BUG-05")
    def test_provider_txn_id_cannot_be_reused_for_a_different_order(self):
        order_a = new_order("u_a")
        order_b = new_order("u_b")
        webhook(order_a, "SUCCESS", "txn_shared")
        webhook(order_b, "SUCCESS", "txn_shared")
        all_rows = [p for p in debug_payments() if p["provider_txn_id"] == "txn_shared"]
        self.assertEqual(len(all_rows), 1,
                         "BUG-05: txn_shared recorded for %d orders: %s"
                         % (len(all_rows), [p["order_id"] for p in all_rows]))
        self.assertEqual(self.status_of("u_b"), "PENDING",
                         "BUG-05: a txn already used for another order activated an unrelated trial")


class WebhookOutOfOrderTests(ApiTestCase):

    @defect("BUG-02")
    def test_late_pending_event_does_not_downgrade_active_trial(self):
        order = new_order("u_ooo1")
        webhook(order, "SUCCESS", "txn_s")
        webhook(order, "PENDING", "txn_p")       # delayed event arrives after success
        self.assertEqual(self.status_of("u_ooo1"), "ACTIVE",
                         "BUG-02: late PENDING downgraded an ACTIVE trial")

    @defect("BUG-02")
    def test_late_failed_event_does_not_downgrade_active_trial(self):
        order = new_order("u_ooo2")
        webhook(order, "SUCCESS", "txn_s")
        webhook(order, "FAILED", "txn_f")
        self.assertEqual(self.status_of("u_ooo2"), "ACTIVE",
                         "BUG-02: late FAILED downgraded a paid, ACTIVE trial")

    @defect("BUG-02")
    def test_replayed_old_events_after_success_never_downgrade(self):
        order = new_order("u_ooo3")
        for status, txn in [("PENDING", "t1"), ("SUCCESS", "t1"), ("PENDING", "t1"), ("FAILED", "t0")]:
            webhook(order, status, txn)
        self.assertEqual(self.status_of("u_ooo3"), "ACTIVE",
                         "BUG-02: final state after replayed events must stay ACTIVE")
