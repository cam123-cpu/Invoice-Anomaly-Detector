import time
import unittest
from urllib.parse import parse_qs, urlparse

from cryptography.hazmat.primitives.asymmetric import rsa

from fake_idp import FakeIdP
from invoice_detector.oidc import OIDCClient, Provider, SSOError, StateSealer, new_attempt, providers_from_env


class OIDCTests(unittest.TestCase):
    def setUp(self):
        self.idp = FakeIdP()
        self.provider = Provider("sso", "Okta", self.idp.issuer, self.idp.client_id, "s3cret",
                                 require_verified_email=True)
        self.client = OIDCClient(self.provider, self.idp.get_json, self.idp.post_form)
        self.attempt = new_attempt("sso")

    def test_full_flow(self):
        url = urlparse(self.client.authorization_url("https://app.example/auth/sso/callback", self.attempt))
        q = parse_qs(url.query)
        self.assertEqual(url.netloc + url.path, "idp.example/authorize")
        self.assertEqual((q["code_challenge_method"][0], q["state"][0], q["nonce"][0]),
                         ("S256", self.attempt["state"], self.attempt["nonce"]))
        code = self.idp.approve("Alice@Acme.example", self.attempt["nonce"])
        token = self.client.exchange_code(code, "https://app.example/auth/sso/callback", self.attempt["verifier"])
        sent = self.idp.requests[-1][1]
        self.assertEqual((sent["code_verifier"], sent["client_secret"]), (self.attempt["verifier"], "s3cret"))
        claims = self.client.verify_id_token(token, self.attempt["nonce"])
        self.assertEqual(self.client.email_from(claims), "alice@acme.example")

    def test_code_cannot_be_reused(self):
        code = self.idp.approve("a@b.example", self.attempt["nonce"])
        self.client.exchange_code(code, "https://x/cb", "v")
        with self.assertRaises(SSOError):
            self.client.exchange_code(code, "https://x/cb", "v")

    def assert_rejected(self, token, nonce=None):
        with self.assertRaises(SSOError):
            self.client.verify_id_token(token, nonce or self.attempt["nonce"])

    def test_rejects_forged_and_wrong_tokens(self):
        n = self.attempt["nonce"]
        good = self.idp.claims("a@b.example", n)
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.assert_rejected(self.idp.sign(good, key=other_key))                      # signed by someone else
        self.assert_rejected(self.idp.sign(good, kid="unknown"))                      # unknown key
        self.assert_rejected(self.idp.sign({**good, "iss": "https://evil.example"}))   # wrong issuer
        self.assert_rejected(self.idp.sign({**good, "aud": "another-app"}))           # meant for another app
        self.assert_rejected(self.idp.sign({**good, "exp": time.time() - 3600}))      # expired
        self.assert_rejected(self.idp.sign({**good, "iat": time.time() + 3600}))      # from the future
        self.assert_rejected(self.idp.sign(good), nonce="different-nonce")            # replayed
        self.assert_rejected(self.idp.sign({**good, "aud": [self.idp.client_id, "x"]}))  # multi-aud without azp
        header, body, sig = self.idp.sign(good).split(".")
        self.assert_rejected(f"{header}.{body}.{sig[:-4]}AAAA")                       # tampered signature
        none_header = "eyJhbGciOiJub25lIn0"                                            # {"alg":"none"}
        self.assert_rejected(f"{none_header}.{body}.")
        self.assert_rejected("not-a-token")
        ok = self.client.verify_id_token(self.idp.sign({**good, "aud": [self.idp.client_id, "x"],
                                                        "azp": self.idp.client_id}), n)
        self.assertEqual(ok["email"], "a@b.example")

    def test_email_rules(self):
        n = self.attempt["nonce"]
        with self.assertRaises(SSOError):
            self.client.email_from(self.idp.claims("a@b.example", n, email_verified=False))
        with self.assertRaises(SSOError):
            self.client.email_from({**self.idp.claims("x", n), "email": None})
        ms = OIDCClient(Provider("microsoft", "Microsoft", self.idp.issuer, "c", "s",
                                 email_claims=("email", "preferred_username")), self.idp.get_json)
        self.assertEqual(ms.email_from({"preferred_username": "Bob@Corp.example"}), "bob@corp.example")

    def test_keys_are_cached_and_refreshed_on_rotation(self):
        n = self.attempt["nonce"]
        self.client.verify_id_token(self.idp.sign(self.idp.claims("a@b.example", n)), n)
        self.client.verify_id_token(self.idp.sign(self.idp.claims("a@b.example", n)), n)
        self.assertEqual(self.idp.jwks_fetches, 1)
        self.idp.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.idp.kid = "key-2"  # the provider rotates its signing key
        self.client.verify_id_token(self.idp.sign(self.idp.claims("a@b.example", n)), n)
        self.assertEqual(self.idp.jwks_fetches, 2)

    def test_https_required(self):
        insecure = OIDCClient(Provider("x", "X", "http://idp.local", "c", "s"), self.idp.get_json)
        with self.assertRaises(SSOError):
            insecure.discovery()

    def test_state_cookie(self):
        sealer = StateSealer("k" * 40)
        sealed = sealer.seal(self.attempt)
        self.assertNotIn(self.attempt["verifier"], sealed)  # encrypted, not just signed
        self.assertEqual(sealer.open(sealed)["state"], self.attempt["state"])
        with self.assertRaises(SSOError):
            StateSealer("other" * 10).open(sealed)
        with self.assertRaises(SSOError):
            sealer.open(sealed[:-3] + "AAA")
        with self.assertRaises(SSOError):
            sealer.open(sealed, now=time.time() + 3600)  # took too long

    def test_providers_from_env(self):
        self.assertEqual(providers_from_env({}), {})
        found = providers_from_env({"GOOGLE_CLIENT_ID": "g", "GOOGLE_CLIENT_SECRET": "gs",
                                    "MICROSOFT_CLIENT_ID": "m", "MICROSOFT_TENANT_ID": "t-123",
                                    "OIDC_ISSUER": "https://acme.okta.com/", "OIDC_CLIENT_ID": "o",
                                    "OIDC_LABEL": "Okta"})
        self.assertEqual(sorted(found), ["google", "microsoft", "sso"])
        self.assertEqual(found["microsoft"].issuer, "https://login.microsoftonline.com/t-123/v2.0")
        self.assertEqual((found["sso"].issuer, found["sso"].label), ("https://acme.okta.com", "Okta"))
        self.assertNotIn("gs", repr(found["google"]))  # the secret never appears in logs
        with self.assertRaises(RuntimeError):
            providers_from_env({"MICROSOFT_CLIENT_ID": "m", "MICROSOFT_TENANT_ID": "common"})


if __name__ == "__main__":
    unittest.main()
