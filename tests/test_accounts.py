import os
import tempfile
import time
import unittest
from dataclasses import replace
from urllib.parse import parse_qs, unquote, urlparse

from fake_idp import FakeIdP
from invoice_detector import totp
from invoice_detector.accounts import AccountError, Accounts
from invoice_detector.mailer import FolderMailer
from invoice_detector.oidc import OIDCClient, Provider
from invoice_detector.security import TokenSigner
from support import DbTestCase, make_settings

PW = "alice-password-123"


def now_code(secret: str) -> str:
    return totp.code_at(secret.replace(" ", ""), totp.current_step())


class AccountsTestCase(DbTestCase):
    def setUp(self):
        super().setUp()
        self.mail_dir = tempfile.mkdtemp()
        self.idp = FakeIdP()
        settings = replace(make_settings(), public_url="https://app.example", mail_dir=self.mail_dir)
        sso = {"sso": OIDCClient(Provider("sso", "Okta", self.idp.issuer, self.idp.client_id, "s"),
                                 self.idp.get_json, self.idp.post_form)}
        self.signer = TokenSigner(settings.secret_key)
        self.acc = Accounts(self.app, self.signer, settings, FolderMailer(self.mail_dir), sso)
        self.app.create_user(self.a, "alice@acme.example", PW, "admin")
        self.app.create_user(self.a, "bob", "bob-password-12345", "accountant")
        self.app.create_user(self.b, "carol@globex.example", "carol-password-1", "admin")

    def sign_in(self, username="alice@acme.example", password=PW):
        return self.acc.authenticate(self.acc.login(username, password, "1.2.3.4")["access_token"])

    def turn_on_2fa(self, user):
        secret = self.acc.setup_2fa(user)["secret"]
        enabled = self.acc.enable_2fa(user, now_code(secret))
        return secret.replace(" ", ""), enabled

    def emails(self):
        files = sorted(os.listdir(self.mail_dir))
        return [open(os.path.join(self.mail_dir, f), encoding="utf-8").read() for f in files]


class PasswordSignInTests(AccountsTestCase):
    def test_sign_in_and_session(self):
        user = self.sign_in("ALICE@acme.example")  # case doesn't matter
        self.assertEqual((user["sub"], user["role"], user["org"], user["mfa"]), ("alice@acme.example", "admin", self.a, False))

    def test_wrong_password_and_unknown_user_look_the_same(self):
        for username, password in (("alice@acme.example", "wrong"), ("nobody", "x")):
            with self.assertRaises(AccountError) as cm:
                self.acc.login(username, password, "1.2.3.4")
            self.assertEqual((cm.exception.status, cm.exception.message), (401, "Wrong username or password."))

    def test_lockout(self):
        for _ in range(5):
            with self.assertRaises(AccountError):
                self.acc.login("bob", "wrong", "9.9.9.9")
        with self.assertRaises(AccountError) as cm:
            self.acc.login("bob", "bob-password-12345", "9.9.9.9")
        self.assertEqual(cm.exception.status, 429)
        self.assertTrue(self.acc.login("bob", "bob-password-12345", "8.8.8.8")["access_token"])

    def test_one_address_trying_many_usernames_is_stopped(self):
        """A bot spraying passwords across many accounts from one address."""
        for i in range(30):
            with self.assertRaises(AccountError):
                self.acc.login(f"user{i}@acme.example", "guess", "6.6.6.6")
        with self.assertRaises(AccountError) as cm:
            self.acc.login("bob", "bob-password-12345", "6.6.6.6")
        self.assertEqual(cm.exception.status, 429)
        self.assertTrue(self.acc.login("bob", "bob-password-12345", "7.7.7.7")["access_token"])

    def test_sso_only_user_has_no_password(self):
        self.app.create_user(self.a, "sso@acme.example", None, "auditor")
        with self.assertRaises(AccountError):
            self.acc.login("sso@acme.example", "!sso", "1.2.3.4")

    def test_changing_the_password_ends_other_sessions(self):
        old = self.acc.login("bob", "bob-password-12345", "1.2.3.4")["access_token"]
        user = self.acc.authenticate(old)
        with self.assertRaises(AccountError):
            self.acc.change_password(user, "wrong", "new-password-12345")
        new = self.acc.change_password(user, "bob-password-12345", "new-password-12345")["access_token"]
        with self.assertRaises(AccountError) as cm:
            self.acc.authenticate(old)
        self.assertEqual(cm.exception.status, 401)
        self.assertEqual(self.acc.authenticate(new)["sub"], "bob")
        self.assertTrue(self.acc.login("bob", "new-password-12345", "1.2.3.4")["access_token"])

    def test_tampered_and_foreign_tokens(self):
        token = self.acc.login("bob", "bob-password-12345", "1.2.3.4")["access_token"]
        other = TokenSigner("x" * 40).make("bob", "admin", self.a)
        for bad in (token[:-2] + "xx", other, "nonsense"):
            with self.assertRaises(AccountError):
                self.acc.authenticate(bad)
        # a valid signature for the wrong company is still refused
        forged = self.signer.make("bob", "admin", self.b, pwf="anything")
        with self.assertRaises(AccountError):
            self.acc.authenticate(forged)


class TwoFactorTests(AccountsTestCase):
    def test_set_up_and_sign_in(self):
        user = self.sign_in()
        setup = self.acc.setup_2fa(user)
        self.assertTrue(setup["uri"].startswith("otpauth://totp/Invoice%20Review%3Aalice%40acme.example"))
        self.assertTrue(setup["qr"].startswith("data:image/svg+xml;base64,"))
        with self.assertRaises(AccountError):
            self.acc.enable_2fa(user, "000000")
        enabled = self.acc.enable_2fa(user, now_code(setup["secret"]))
        self.assertEqual(len(enabled["recovery_codes"]), 10)
        self.assertTrue(self.acc.authenticate(enabled["access_token"])["mfa"])
        stored = self.app.user_security(self.a, "alice@acme.example")
        self.assertNotIn(setup["secret"].replace(" ", ""), str(stored))  # secret encrypted at rest
        self.assertNotIn(enabled["recovery_codes"][0], str(stored))      # codes stored hashed

        first = self.acc.login("alice@acme.example", PW, "1.2.3.4")
        self.assertTrue(first["mfa_required"])
        self.assertNotIn("access_token", first)
        with self.assertRaises(AccountError):
            self.acc.authenticate(first["mfa_token"])  # the pending token isn't a session
        with self.assertRaises(AccountError):
            self.acc.login_second_factor(first["mfa_token"], "123456", "1.2.3.4")
        secret = setup["secret"].replace(" ", "")
        next_code = totp.code_at(secret, totp.current_step() + 1)  # the enable step is used up
        done = self.acc.login_second_factor(first["mfa_token"], next_code, "1.2.3.4")
        self.assertTrue(self.acc.authenticate(done["access_token"])["mfa"])
        with self.assertRaises(AccountError):  # the same code can't be used twice
            self.acc.login_second_factor(self.acc.login("alice@acme.example", PW, "1.2.3.4")["mfa_token"],
                                         next_code, "1.2.3.4")

    def test_recovery_codes(self):
        _, enabled = self.turn_on_2fa(self.sign_in())
        code = enabled["recovery_codes"][0]
        pending = self.acc.login("alice@acme.example", PW, "1.2.3.4")["mfa_token"]
        done = self.acc.login_second_factor(pending, code.upper(), "1.2.3.4")
        self.assertEqual(done["recovery_codes_left"], 9)
        pending = self.acc.login("alice@acme.example", PW, "1.2.3.4")["mfa_token"]
        with self.assertRaises(AccountError):
            self.acc.login_second_factor(pending, code, "1.2.3.4")  # used up
        self.assertIn("recovery code (9 left)", str(self.app.list_audit(self.a)))

    def test_code_guessing_is_rate_limited(self):
        self.turn_on_2fa(self.sign_in())
        pending = self.acc.login("alice@acme.example", PW, "1.2.3.4")["mfa_token"]
        for _ in range(5):
            with self.assertRaises(AccountError):
                self.acc.login_second_factor(pending, "000000", "1.2.3.4")
        with self.assertRaises(AccountError) as cm:
            self.acc.login_second_factor(pending, "000000", "1.2.3.4")
        self.assertEqual(cm.exception.status, 429)

    def test_expired_pending_sign_in(self):
        self.turn_on_2fa(self.sign_in())
        stale = self.signer.issue({"sub": "alice@acme.example", "org": self.a, "pwf": "x"}, "mfa", 300,
                                  now=time.time() - 600)
        with self.assertRaises(AccountError) as cm:
            self.acc.login_second_factor(stale, "123456", "1.2.3.4")
        self.assertIn("timed out", cm.exception.message)

    def test_turning_it_off_needs_password_and_code(self):
        user = self.sign_in()
        secret, enabled = self.turn_on_2fa(user)
        user = self.acc.authenticate(enabled["access_token"])
        spare = enabled["recovery_codes"][0]
        with self.assertRaises(AccountError):
            self.acc.disable_2fa(user, "wrong-password", spare)
        with self.assertRaises(AccountError):
            self.acc.disable_2fa(user, PW, "000000")
        self.acc.disable_2fa(user, PW, spare)  # a recovery code also proves who you are
        self.assertFalse(self.app.user_security(self.a, "alice@acme.example")["totp_enabled"])
        self.assertIn("access_token", self.acc.login("alice@acme.example", PW, "1.2.3.4"))

    def test_company_can_require_it(self):
        self.app.set_org_settings(self.a, require_2fa=True)
        token = self.acc.login("bob", "bob-password-12345", "1.2.3.4")["access_token"]
        with self.assertRaises(AccountError) as cm:
            self.acc.authenticate(token)
        self.assertEqual((cm.exception.status, cm.exception.requires_2fa), (403, True))
        user = self.acc.authenticate(token, enforce_2fa=False)  # can still reach set-up
        secret, enabled = self.turn_on_2fa(user)
        self.assertEqual(self.acc.authenticate(enabled["access_token"])["sub"], "bob")
        strong = self.acc.authenticate(enabled["access_token"])
        with self.assertRaises(AccountError) as cm:
            self.acc.disable_2fa(strong, "bob-password-12345", totp.code_at(secret, totp.current_step() + 1))
        self.assertEqual(cm.exception.status, 409)
        self.assertFalse(self.app.get_org(self.b)["require_2fa"])

    def test_new_recovery_codes(self):
        user = self.sign_in()
        secret, first = self.turn_on_2fa(user)
        with self.assertRaises(AccountError):
            self.acc.new_recovery_codes(user, first["recovery_codes"][0])  # needs the app, not a recovery code
        fresh = self.acc.new_recovery_codes(user, totp.code_at(secret, totp.current_step() + 1))["recovery_codes"]
        self.assertEqual(len(set(fresh) & set(first["recovery_codes"])), 0)
        pending = self.acc.login("alice@acme.example", PW, "1.2.3.4")["mfa_token"]
        with self.assertRaises(AccountError):
            self.acc.login_second_factor(pending, first["recovery_codes"][1], "1.2.3.4")  # old set retired


class ResetTests(AccountsTestCase):
    def test_email_reset_link(self):
        self.assertEqual(self.acc.forgot_password("alice@acme.example", "1.2.3.4"), {"email_enabled": True})
        mail = self.emails()
        self.assertEqual(len(mail), 1)
        self.assertIn("To: alice@acme.example", mail[0])
        link = next(line for line in mail[0].splitlines() if "#/reset?token=" in line)
        self.assertTrue(link.startswith("https://app.example/#/reset?token="))
        token = unquote(link.split("token=")[1])
        with self.assertRaises(AccountError):
            self.acc.authenticate(token)  # a reset link is not a session
        self.acc.reset_password(token, "brand-new-password-1")
        self.assertTrue(self.acc.login("alice@acme.example", "brand-new-password-1", "1.2.3.4")["access_token"])
        with self.assertRaises(AccountError):
            self.acc.reset_password(token, "another-password-12")  # links work once

    def test_reset_emails_to_one_person_are_capped(self):
        for i in range(6):  # from different addresses, so only the per-person cap applies
            self.assertEqual(self.acc.forgot_password("alice@acme.example", f"10.0.0.{i}"), {"email_enabled": True})
        self.assertEqual(len(self.emails()), 3)

    def test_no_clues_about_who_has_an_account(self):
        for name in ("nobody@acme.example", "bob"):  # unknown, and a user with no email address
            self.assertEqual(self.acc.forgot_password(name, "5.5.5.5"), {"email_enabled": True})
        self.assertEqual(self.emails(), [])

    def test_bad_links_and_no_email_configured(self):
        for bad in ("nonsense", self.signer.issue({"sub": "alice@acme.example", "org": self.a, "pwf": "x"}, "reset", 60)):
            with self.assertRaises(AccountError):
                self.acc.reset_password(bad, "brand-new-password-1")
        quiet = Accounts(self.app, self.signer, make_settings(), sso_clients={})
        self.assertEqual(quiet.forgot_password("alice@acme.example", "1.2.3.4"), {"email_enabled": False})

    def test_admin_resets(self):
        admin = self.sign_in()
        self.acc.admin_set_password(admin, "bob", "temporary-password-1")
        self.assertTrue(self.acc.login("bob", "temporary-password-1", "1.2.3.4")["access_token"])
        bob = self.acc.authenticate(self.acc.login("bob", "temporary-password-1", "1.2.3.4")["access_token"])
        self.turn_on_2fa(bob)
        self.acc.admin_reset_2fa(admin, "bob")
        self.assertIn("access_token", self.acc.login("bob", "temporary-password-1", "1.2.3.4"))
        for action in (lambda: self.acc.admin_set_password(admin, "carol@globex.example", "x" * 12),
                       lambda: self.acc.admin_reset_2fa(admin, "carol@globex.example")):
            with self.assertRaises(AccountError) as cm:
                action()  # another company's user
            self.assertEqual(cm.exception.status, 404)


class SingleSignOnTests(AccountsTestCase):
    def go(self, email, **tamper):
        url, cookie = self.acc.sso_start("sso")
        q = parse_qs(urlparse(url).query)
        self.assertEqual(q["redirect_uri"][0], "https://app.example/auth/sso/callback")
        code = self.idp.approve(email, q["nonce"][0])
        query = {"code": code, "state": q["state"][0], **tamper.pop("query", {})}
        return self.acc.sso_callback(tamper.pop("name", "sso"), query, tamper.pop("cookie", cookie), "1.2.3.4")

    def test_sign_in(self):
        self.assertEqual(self.acc.sso_providers(), [{"name": "sso", "label": "Okta"}])
        where, token = self.go("Alice@Acme.example")
        self.assertEqual(where, "/#/dashboard")  # the token goes in a cookie, never the address
        user = self.acc.authenticate(token)
        self.assertEqual((user["sub"], user["mfa"]), ("alice@acme.example", True))
        self.assertIn("single sign-on (Okta)", str(self.app.list_audit(self.a)))

    def test_refusals_go_back_to_the_sign_in_page(self):
        cases = {
            "unknown person": self.go("stranger@acme.example"),
            "forged state": self.go("alice@acme.example", query={"state": "forged"}),
            "no cookie": self.go("alice@acme.example", cookie=None),
            "other provider": self.go("alice@acme.example", name="google"),
            "cancelled": self.go("alice@acme.example", query={"error": "access_denied"}),
        }
        for case, (where, token) in cases.items():
            self.assertTrue(where.startswith("/#/login?error="), case)
            self.assertIsNone(token, case)
        self.assertIn("no%20account%20for%20stranger%40acme.example", cases["unknown person"][0])
        with self.assertRaises(AccountError):
            self.acc.sso_start("google")


if __name__ == "__main__":
    unittest.main()
