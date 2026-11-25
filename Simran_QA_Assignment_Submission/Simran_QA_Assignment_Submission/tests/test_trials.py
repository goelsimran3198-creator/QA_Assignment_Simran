"""Trial creation: happy path, validation, single-live-trial rule (business rules 1-3)."""
from tests.helpers import (ApiTestCase, create_trial, defect, debug_trials,
                           get_subscription, new_order, webhook, request)


class TrialCreationTests(ApiTestCase):

    def test_create_trial_happy_path_persists_pending_trial(self):
        resp = create_trial("u_happy", "yearly", 100)
        self.assertEqual(resp.status, 201, resp)
        self.assertEqual(resp.json["status"], "PENDING")
        self.assertTrue(resp.json["order_id"].startswith("ord_"))

        rows = debug_trials("u_happy")
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(
            (rows[0]["order_id"], rows[0]["user_id"], rows[0]["plan"], rows[0]["amount"], rows[0]["status"]),
            (resp.json["order_id"], "u_happy", "yearly", 100, "PENDING"))

    def test_subscription_endpoint_returns_created_trial_and_404_for_unknown_user(self):
        order_id = new_order("u_read", "monthly", 50)
        sub = get_subscription("u_read")
        self.assertEqual(sub.status, 200, sub)
        self.assertEqual((sub.json["order_id"], sub.json["status"]), (order_id, "PENDING"))
        self.assertEqual(get_subscription("nobody").status, 404)

    def test_invalid_requests_are_rejected_and_create_nothing(self):
        bad_bodies = {
            "missing user_id": {"plan": "yearly", "amount": 100},
            "blank user_id": {"user_id": "  ", "plan": "yearly", "amount": 100},
            "unknown plan": {"user_id": "u_bad", "plan": "weekly", "amount": 100},
            "missing plan": {"user_id": "u_bad", "amount": 100},
            "missing amount": {"user_id": "u_bad", "plan": "yearly"},
            "amount as string": {"user_id": "u_bad", "plan": "yearly", "amount": "100"},
            "amount as float": {"user_id": "u_bad", "plan": "yearly", "amount": 10.5},
            "amount null": {"user_id": "u_bad", "plan": "yearly", "amount": None},
        }
        for name, body in bad_bodies.items():
            with self.subTest(case=name):
                resp = request("POST", "/api/trials", body)
                self.assertEqual(resp.status, 400, "%s should be rejected, got %r" % (name, resp))
        self.assertEqual(debug_trials(), [], "rejected requests must not create any trial")

    @defect("BUG-08")
    def test_amount_must_be_a_positive_integer(self):
        # Rule 2: positive integer in paise. 0, negatives and JSON booleans (True is an int in Python) are invalid.
        for i, amount in enumerate([0, -1, -100, True, False]):
            with self.subTest(amount=amount):
                user = "u_amt_%d" % i
                resp = create_trial(user, "yearly", amount)
                self.assertEqual(
                    resp.status, 400,
                    "BUG-08: amount=%r must be rejected with 400, got %r; stored rows: %s"
                    % (amount, resp, debug_trials(user)))
                self.assertEqual(debug_trials(user), [], "BUG-08: invalid amount must not create a trial")

    @defect("BUG-01")
    def test_repeated_create_request_does_not_create_duplicate_live_trial(self):
        first = create_trial("u_retry", "yearly", 100)
        second = create_trial("u_retry", "yearly", 100)   # client retry
        self.assertEqual(first.status, 201, first)
        self.assertIn(second.status, (200, 201), second)
        live = self.live_trials("u_retry", "yearly")
        self.assertEqual(
            len(live), 1,
            "BUG-01: expected exactly 1 live trial for same user+plan after a retry, found %d: %s"
            % (len(live), [t["order_id"] for t in live]))

    @defect("BUG-01")
    def test_create_while_trial_is_active_does_not_create_second_live_trial(self):
        order_id = new_order("u_active_retry", "monthly", 100)
        self.activate(order_id)
        create_trial("u_active_retry", "monthly", 100)    # user taps "start trial" again
        live = self.live_trials("u_active_retry", "monthly")
        self.assertEqual(len(live), 1, "BUG-01: found %d live trials: %s"
                         % (len(live), [(t["order_id"], t["status"]) for t in live]))
        self.assertEqual(self.status_of("u_active_retry"), "ACTIVE",
                         "BUG-01: GET /subscriptions must keep showing the ACTIVE trial, not a newer PENDING duplicate")

    def test_same_user_can_hold_trials_for_different_plans_and_users_are_independent(self):
        new_order("u_multi", "monthly", 100)
        new_order("u_multi", "yearly", 1000)
        new_order("u_other", "yearly", 1000)
        self.assertEqual(len(self.live_trials("u_multi")), 2)
        self.assertEqual(len(self.live_trials("u_other")), 1)

    def test_user_can_start_a_new_trial_after_previous_one_failed(self):
        # Guards against an over-eager fix of BUG-01: only LIVE trials may block a new one.
        first = new_order("u_failed", "yearly", 100)
        self.assertEqual(webhook(first, "FAILED", "txn_f1").status, 200)
        second = create_trial("u_failed", "yearly", 100)
        self.assertEqual(second.status, 201, second)
        self.assertEqual(len(self.live_trials("u_failed", "yearly")), 1)
