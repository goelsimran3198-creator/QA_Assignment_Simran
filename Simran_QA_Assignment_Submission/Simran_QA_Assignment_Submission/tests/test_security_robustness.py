"""Authorization of internal endpoints and robustness against malformed input."""
from tests.helpers import (ApiTestCase, INTERNAL, SIGNATURE, debug_trials, defect,
                           new_order, request)


class InternalEndpointSecurityTests(ApiTestCase):

    def test_admin_reset_requires_internal_key_and_does_not_wipe_data_without_it(self):
        new_order("u_keep")
        for name, headers in {"no key": {}, "wrong key": {"X-Internal-Key": "x"}}.items():
            with self.subTest(case=name):
                self.assertEqual(request("POST", "/api/admin/reset", {}, headers).status, 401)
        self.assertEqual(len(debug_trials("u_keep")), 1, "unauthorised reset must not delete data")


class MalformedInputTests(ApiTestCase):

    def test_invalid_json_returns_400(self):
        for path, headers in [("/api/trials", {}), ("/api/webhooks/payment", SIGNATURE),
                              ("/api/reconcile", INTERNAL)]:
            with self.subTest(path=path):
                resp = request("POST", path, raw=b"{not json", headers=headers)
                self.assertEqual(resp.status, 400, resp)

    @defect("BUG-09")
    def test_valid_json_that_is_not_an_object_returns_400_instead_of_crashing(self):
        for path, headers in [("/api/trials", {}), ("/api/webhooks/payment", SIGNATURE),
                              ("/api/reconcile", INTERNAL)]:
            with self.subTest(path=path):
                resp = request("POST", path, raw=b"[]", headers=headers)
                self.assertEqual(resp.status, 400,
                                 "BUG-09: body '[]' to %s should be a clean 400, got %r" % (path, resp))
