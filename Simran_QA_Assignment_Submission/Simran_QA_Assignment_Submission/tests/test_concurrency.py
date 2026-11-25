"""Race conditions: parallel requests must respect rules 1, 7, 8 and 14."""
from tests.helpers import (ApiTestCase, create_trial, debug_payments, defect,
                           new_order, run_parallel, webhook)

PARALLEL = 15


class ConcurrencyTests(ApiTestCase):

    @defect("BUG-01")
    def test_parallel_create_requests_for_same_user_and_plan_yield_one_live_trial(self):
        responses = run_parallel(PARALLEL, lambda i: create_trial("u_race", "yearly", 100))
        codes = sorted(set(str(r.status) for r in responses))
        self.assertTrue(all(c in ("200", "201") for c in codes),
                        "unexpected responses under concurrency: %s" % codes)
        live = self.live_trials("u_race", "yearly")
        self.assertEqual(len(live), 1,
                         "BUG-01: %d parallel identical requests created %d live trials (HTTP codes: %s)"
                         % (PARALLEL, len(live), codes))

    @defect("BUG-04")
    def test_parallel_duplicate_webhooks_create_one_payment_and_one_activation(self):
        order = new_order("u_race_wh")
        responses = run_parallel(PARALLEL, lambda i: webhook(order, "SUCCESS", "txn_race"))
        self.assertTrue(all(r.status == 200 for r in responses), [r for r in responses if r.status != 200])
        self.assertEqual(self.status_of("u_race_wh"), "ACTIVE")
        pays = debug_payments(order)
        self.assertEqual(len(pays), 1,
                         "BUG-04: %d simultaneous deliveries of one event created %d payment rows"
                         % (PARALLEL, len(pays)))
