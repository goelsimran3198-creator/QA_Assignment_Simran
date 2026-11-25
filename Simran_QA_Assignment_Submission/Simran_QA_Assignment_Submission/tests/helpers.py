"""Shared helpers for the Subscription Reliability API tests.

Zero third-party dependencies: only the Python standard library (3.9+).
The suite starts server.py itself if nothing is listening on BASE_URL,
and resets the database before EVERY test (see ApiTestCase.setUp).
"""
import atexit
import http.client
import json
import os
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:18080")
SERVER_PATH = os.environ.get("SERVER_PATH", os.path.join(ROOT, "server.py"))

SIGNATURE = {"X-Signature": "qa-secret"}
INTERNAL = {"X-Internal-Key": "qa-internal"}
LIVE_STATUSES = ("PENDING", "ACTIVE")


# --------------------------------------------------------------------------
# Marking tests that expose a known defect (see BUGS.md)
# --------------------------------------------------------------------------
def defect(bug_id):
    """Tag a test as the automated proof of a defect listed in BUGS.md.

    Such tests are EXPECTED to fail against the supplied server.py and will
    pass once the defect is fixed. `python3 run_tests.py --regression-only`
    skips them.
    """
    def decorator(fn):
        fn.defect_id = bug_id
        return fn
    return decorator


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------
class Response(object):
    def __init__(self, status, text, error=None):
        self.status = status          # None => no HTTP response (connection dropped)
        self.text = text
        self.error = error

    @property
    def json(self):
        try:
            return json.loads(self.text)
        except Exception:
            return None

    def __repr__(self):
        if self.status is None:
            return "<no response: %s>" % self.error
        return "<HTTP %s %s>" % (self.status, self.text[:200])


def request(method, path, body=None, headers=None, raw=None, timeout=15):
    data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    for attempt in range(4):
        req = urllib.request.Request(BASE_URL + path, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return Response(resp.status, resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return Response(exc.code, exc.read().decode("utf-8"))
        except Exception as exc:  # connection reset, server crashed handler, timeout...
            if _is_accept_queue_reset(exc) and attempt < 3:
                time.sleep(0.05 * (attempt + 1))   # flake guard, see README_SUBMISSION.md
                continue
            return Response(None, "", error=repr(exc))


def _is_accept_queue_reset(exc):
    """The stdlib server has a tiny listen backlog (5): a burst of parallel connections can be
    reset BEFORE the request is read. That is test-infrastructure noise, not product behaviour,
    so it is retried. A handler that crashes after reading the request raises RemoteDisconnected,
    which is deliberately NOT retried (that is a real defect, see BUG-09)."""
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, http.client.RemoteDisconnected):
        return False
    return isinstance(reason, (ConnectionResetError, ConnectionRefusedError, ConnectionAbortedError))


# --------------------------------------------------------------------------
# Server lifecycle (only started if nothing is already running)
# --------------------------------------------------------------------------
_proc = None


def server_is_up():
    return request("GET", "/health", timeout=2).status == 200


def stop_server():
    global _proc
    if _proc and _proc.poll() is None:
        _proc.terminate()
        try:
            _proc.wait(timeout=5)
        except Exception:
            _proc.kill()
    _proc = None


def ensure_server():
    global _proc
    if server_is_up():
        return
    if not os.path.exists(SERVER_PATH):
        raise RuntimeError("server not running and %s not found" % SERVER_PATH)
    log = open(os.path.join(ROOT, "server.log"), "w")
    _proc = subprocess.Popen([sys.executable, SERVER_PATH, "--reset"],
                             cwd=os.path.dirname(SERVER_PATH), stdout=log, stderr=log)
    atexit.register(stop_server)
    for _ in range(60):
        if server_is_up():
            return
        time.sleep(0.2)
    raise RuntimeError("server did not become healthy; see server.log")


# --------------------------------------------------------------------------
# API wrappers
# --------------------------------------------------------------------------
def reset():
    return request("POST", "/api/admin/reset", {}, INTERNAL)


def create_trial(user_id="user_1", plan="yearly", amount=100):
    return request("POST", "/api/trials", {"user_id": user_id, "plan": plan, "amount": amount})


def new_order(user_id="user_1", plan="yearly", amount=100):
    """Create a trial and return its order_id (fails loudly if creation fails)."""
    resp = create_trial(user_id, plan, amount)
    assert resp.status == 201, "setup failed, could not create trial: %r" % resp
    return resp.json["order_id"]


def webhook(order_id, status, txn="txn_001", headers=SIGNATURE):
    return request("POST", "/api/webhooks/payment",
                   {"order_id": order_id, "provider_txn_id": txn, "status": status}, headers)


def reconcile(order_id, status, txn="txn_001", headers=INTERNAL):
    return request("POST", "/api/reconcile",
                   {"order_id": order_id, "provider_txn_id": txn, "provider_status": status}, headers)


def get_subscription(user_id):
    return request("GET", "/api/subscriptions/" + urllib.parse.quote(user_id))


def debug_trials(user_id=None):
    path = "/api/debug/trials" + ("?user_id=" + urllib.parse.quote(user_id) if user_id else "")
    return request("GET", path).json["trials"]


def debug_payments(order_id=None):
    path = "/api/debug/payments" + ("?order_id=" + urllib.parse.quote(order_id) if order_id else "")
    return request("GET", path).json["payments"]


def run_parallel(n, fn):
    """Run fn(i) in n threads released at the same instant (maximises race odds)."""
    barrier = threading.Barrier(n)

    def task(i):
        barrier.wait(timeout=10)
        return fn(i)

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(task, range(n)))


# --------------------------------------------------------------------------
# Base test case: server up + clean database for every single test
# --------------------------------------------------------------------------
class ApiTestCase(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        ensure_server()

    def setUp(self):
        resp = reset()
        self.assertEqual(resp.status, 200, "test isolation failed: reset returned %r" % resp)

    # state helpers: assertions are made on stored business state, not only HTTP codes
    def status_of(self, user_id):
        resp = get_subscription(user_id)
        self.assertEqual(resp.status, 200, "GET subscription failed: %r" % resp)
        return resp.json["status"]

    def live_trials(self, user_id, plan=None):
        return [t for t in debug_trials(user_id)
                if t["status"] in LIVE_STATUSES and (plan is None or t["plan"] == plan)]

    def activate(self, order_id, txn="txn_activate"):
        resp = webhook(order_id, "SUCCESS", txn)
        self.assertEqual(resp.status, 200, "setup failed: SUCCESS webhook returned %r" % resp)
