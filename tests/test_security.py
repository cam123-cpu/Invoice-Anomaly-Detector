import unittest

from invoice_detector.security import (AuthError, RateLimiter, RateLimitError, TokenSigner,
                                       hash_password, verify_password)


class PasswordTests(unittest.TestCase):
    def test_roundtrip_and_wrong_password(self):
        h = hash_password("correct horse battery staple")
        self.assertTrue(verify_password("correct horse battery staple", h))
        self.assertFalse(verify_password("wrong", h))

    def test_salts_differ_and_plaintext_not_stored(self):
        a, b = hash_password("same-password"), hash_password("same-password")
        self.assertNotEqual(a, b)
        self.assertNotIn("same-password", a)

    def test_garbage_hash_is_rejected_not_crashed(self):
        self.assertFalse(verify_password("x", "not-a-hash"))


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.signer = TokenSigner("s" * 40)

    def test_roundtrip(self):
        data = self.signer.read(self.signer.make("alice", "admin"))
        self.assertEqual(data, {"sub": "alice", "role": "admin"})

    def test_tampered_token_rejected(self):
        body, sig = self.signer.make("alice", "auditor").split(".")
        forged = self.signer.make("alice", "admin").split(".")[0] + "." + sig
        with self.assertRaises(AuthError):
            self.signer.read(forged)

    def test_expired_token_rejected(self):
        token = self.signer.make("alice", "admin", ttl=60, now=1000)
        self.assertEqual(self.signer.read(token, now=1030)["sub"], "alice")
        with self.assertRaises(AuthError):
            self.signer.read(token, now=1100)

    def test_other_secret_rejected(self):
        token = self.signer.make("alice", "admin")
        with self.assertRaises(AuthError):
            TokenSigner("different-secret-" + "z" * 30).read(token)

    def test_garbage_rejected(self):
        for bad in ["", "abc", "a.b", "a.b.c"]:
            with self.assertRaises(AuthError):
                self.signer.read(bad)


class RateLimiterTests(unittest.TestCase):
    def test_lockout_after_max_failures(self):
        rl = RateLimiter(max_attempts=3, window=100, lockout=50)
        for t in (0, 1, 2):
            rl.check("k", now=t)
            rl.record_failure("k", now=t)
        with self.assertRaises(RateLimitError):
            rl.check("k", now=3)
        rl.check("k", now=60)  # lockout over

    def test_keys_are_independent(self):
        rl = RateLimiter(max_attempts=2)
        rl.record_failure("a", now=0)
        rl.record_failure("a", now=1)
        with self.assertRaises(RateLimitError):
            rl.check("a", now=2)
        rl.check("b", now=2)

    def test_old_failures_expire(self):
        rl = RateLimiter(max_attempts=3, window=10, lockout=10)
        rl.record_failure("k", now=0)
        rl.record_failure("k", now=1)
        rl.record_failure("k", now=100)  # first two are outside the window
        rl.check("k", now=101)

    def test_success_resets(self):
        rl = RateLimiter(max_attempts=3)
        rl.record_failure("k", now=0)
        rl.record_failure("k", now=1)
        rl.record_success("k")
        rl.record_failure("k", now=2)
        rl.check("k", now=3)


if __name__ == "__main__":
    unittest.main()
