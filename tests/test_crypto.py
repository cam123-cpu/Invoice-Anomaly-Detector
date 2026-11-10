import secrets
import unittest

from invoice_detector.crypto import Vault


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.k1, self.k2, self.idx = (secrets.token_bytes(32) for _ in range(3))
        self.vault = Vault({1: self.k1}, self.idx)

    def test_roundtrip_and_nondeterministic(self):
        a = self.vault.encrypt("DE89370400440532013000", "ctx")
        b = self.vault.encrypt("DE89370400440532013000", "ctx")
        self.assertNotEqual(a, b)
        self.assertNotIn("DE89", a)
        self.assertEqual(self.vault.decrypt(a, "ctx"), "DE89370400440532013000")

    def test_tampering_detected(self):
        token = self.vault.encrypt("secret", "ctx")
        bad = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
        with self.assertRaises(ValueError):
            self.vault.decrypt(bad, "ctx")

    def test_wrong_context_rejected(self):
        token = self.vault.encrypt("secret", "invoices.bank:A1")
        with self.assertRaises(ValueError):
            self.vault.decrypt(token, "invoices.bank:B2")

    def test_key_rotation(self):
        old = self.vault.encrypt("secret", "ctx")
        rotated_vault = Vault({1: self.k1, 2: self.k2}, self.idx)
        self.assertEqual(rotated_vault.decrypt(old, "ctx"), "secret")  # old data still readable
        new = rotated_vault.rotate(old, "ctx")
        self.assertTrue(new.startswith("2:"))
        with self.assertRaises(ValueError):
            Vault({1: self.k1}, self.idx).decrypt(new, "ctx")  # needs key 2

    def test_blind_index(self):
        self.assertEqual(self.vault.blind_index("X"), self.vault.blind_index("X"))
        self.assertNotEqual(self.vault.blind_index("X"), self.vault.blind_index("Y"))
        other = Vault({1: self.k1}, secrets.token_bytes(32))
        self.assertNotEqual(self.vault.blind_index("X"), other.blind_index("X"))


if __name__ == "__main__":
    unittest.main()
