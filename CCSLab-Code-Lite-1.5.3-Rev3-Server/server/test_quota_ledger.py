"""Real SQLite quota transactions with an explicit authentication fixture.

These checks exercise ledger correctness, not production authentication or inference.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from .account_errors import AccountError
from .quotas import Quotas, UsageObserver, usage
from .test_support import TEST_ROOT


class StoreFixture:
    def __init__(self, path):
        self.path = path
        with self.connect() as db:
            db.execute("CREATE TABLE users(id INTEGER PRIMARY KEY,role TEXT)")
            db.executemany("INSERT INTO users VALUES (?,?)", [(1,"admin"),(2,"user")])
            db.executescript(Path(__file__).with_name("migrations").joinpath("005_token_quotas.sql").read_text())
        self.quotas = Quotas(self)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _session(self, db, token, *, active=False, admin=False):
        if token not in ("admin-fixture", "user-fixture") or (admin and token != "admin-fixture"):
            raise AccountError("fixture authentication refused", 403)
        return db.execute("SELECT * FROM users WHERE id=?", (1 if token == "admin-fixture" else 2,)).fetchone(), "user"


class QuotaLedgerTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(dir=TEST_ROOT, prefix="quota-ledger-")
        self.addCleanup(self.scratch.cleanup)
        self.store = StoreFixture(Path(self.scratch.name)/"ledger.sqlite3")
        self.quota = self.store.quotas

    def state(self):
        with self.store.connect() as db:
            return self.quota.snapshot(db, 2)

    def grant(self, limit):
        return self.quota.set("admin-fixture", 2, "limited" if limit is not None else "unlimited", limit)

    def test_atomic_reservations_cannot_overspend_concurrently(self):
        self.grant(1000)
        def reserve(_):
            try:
                return self.quota.reserve("user-fixture", "same-caller-id", 600)
            except AccountError as error:
                return error.status_code
        with ThreadPoolExecutor(2) as workers:
            results = list(workers.map(reserve, range(2)))
        self.assertEqual(sum(isinstance(result, str) for result in results), 1)
        self.assertIn(402, results)
        identity = next(result for result in results if isinstance(result, str))
        self.quota.finish(identity, (300, 50), sent=True)
        self.quota.finish(identity, (300, 50), sent=True)
        self.assertEqual((self.state()["used_tokens"],self.state()["remaining_tokens"]), (350,650))

    def test_zero_exhaustion_refill_unlimited_and_old_call_isolation(self):
        self.grant(0)
        with self.assertRaisesRegex(AccountError, "额度已经用完"):
            self.quota.reserve("user-fixture", "one", 1)
        self.grant(1000)
        identity = self.quota.reserve("user-fixture", "old", 1000)
        self.grant(2000)
        self.quota.finish(identity, (800,200), sent=True)
        self.assertEqual(self.state()["remaining_tokens"],2000)
        self.grant(None)
        self.assertIsNone(self.state()["remaining_tokens"])

    def test_unknown_usage_is_held_not_invented_and_survives_restart(self):
        self.grant(1000)
        identity = self.quota.reserve("user-fixture", "aborted", 600)
        self.quota.finish(identity, None, sent=True)
        self.assertTrue(self.state()["unconfirmed"])
        self.assertEqual(self.state()["used_tokens"],0)
        with self.assertRaisesRegex(AccountError,"待核对"):
            Quotas(self.store).reserve("user-fixture", "retry", 1)
        self.grant(1000)
        identity = self.quota.reserve("user-fixture", "rejected", 600)
        self.quota.finish(identity, None, sent=False)
        self.assertEqual(self.state()["remaining_tokens"],1000)

    def test_validation_and_admin_only_target(self):
        for invalid in (-1,True,1.5,10**12+1):
            with self.subTest(invalid=invalid),self.assertRaises(AccountError):
                self.grant(invalid)
        with self.assertRaises(AccountError):
            self.quota.set("user-fixture",2,"limited",1000)
        with self.assertRaises(AccountError):
            self.quota.set("admin-fixture",1,"limited",1000)
        self.assertEqual(self.state()["mode"], "unlimited")

    def test_actual_usage_counts_input_and_output_once_including_cache(self):
        observer = UsageObserver()
        data = {"usage":{"prompt_tokens":350,"completion_tokens":25,"total_tokens":375,
                         "prompt_tokens_details":{"cached_tokens":300}}}
        observer.frame(["data: "+json.dumps(data)])
        observer.frame(["data: "+json.dumps(data)])
        observer.frame(["data: [DONE]"])
        self.assertEqual(observer.result,(350,25))
        self.grant(1000)
        identity = self.quota.reserve("user-fixture","usage",500)
        self.quota.finish(identity,observer.result,sent=True)
        self.assertEqual(self.state()["remaining_tokens"],625)
        for invalid in ({},{"usage":{"prompt_tokens":True,"completion_tokens":1}},
                        {"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":9}}):
            self.assertIsNone(usage(invalid))
        observer.body(json.dumps({"usage":{"prompt_tokens":351,"completion_tokens":25}}))
        self.assertIsNone(observer.result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
