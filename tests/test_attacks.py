"""Attacks from the penetration test, kept as tests.

Each test is something an attacker would try, written so that it FAILS if the hole it found
ever reopens. They exercise the real sign-in service, database layer, file readers and
detector directly (the HTTP versions are in test_api.py)."""
import io
import time
import unittest
import zipfile
from difflib import SequenceMatcher
from unittest import mock

from invoice_detector import detection
from invoice_detector.accounts import AccountError
from invoice_detector.detection import DEFAULT_CONFIG, Invoice, find_similar_vendor_names
from invoice_detector.importer import FileProblem, read_table
from invoice_detector.intake import DocumentIntake
from invoice_detector.oidc import OIDCClient, Provider, SSOError
from invoice_detector.security import password_problem
import test_accounts as base   # imported as a module so its own tests aren't collected twice
from test_importer import xlsx_bytes

PW = base.PW


class StolenSessionTests(base.AccountsTestCase):
    def test_a_copied_token_dies_at_sign_out(self):
        """Someone copies the session token (shared computer, proxy log, malware). The owner
        signs out. The copy must stop working, not live on until it expires."""
        token = self.acc.login("alice@acme.example", PW, "1.2.3.4")["access_token"]
        other_device = self.acc.login("alice@acme.example", PW, "5.6.7.8")["access_token"]
        self.acc.logout(self.acc.authenticate(token))
        for stolen in (token, other_device):
            with self.assertRaises(AccountError) as cm:
                self.acc.authenticate(stolen)
            self.assertEqual(cm.exception.status, 401)
        self.assertTrue(self.sign_in())   # signing in again still works

    def test_tokens_for_one_purpose_cannot_be_used_for_another(self):
        access = self.acc.login("alice@acme.example", PW, "1.2.3.4")["access_token"]
        draft = self.signer.issue({"org": self.a, "sub": "alice@acme.example", "role": "admin"}, "doc", 600)
        notice = self.signer.issue({"m": "hi", "sub": "alice@acme.example", "org": self.a, "role": "admin"}, "notice", 60)
        for wrong in (draft, notice):
            with self.assertRaises(AccountError):
                self.acc.authenticate(wrong)
        with self.assertRaises(AccountError):
            self.acc.notice(access)
        intake = DocumentIntake(self.app, self.signer, self.acc.settings, isolate=False)
        result = intake.import_drafts({"sub": "alice@acme.example", "org": self.a},
                                      [{"draft": access, "id": "X-1", "vendor": "V", "amount": 1, "date": "2026-01-01"}])
        self.assertEqual(result["added"], 0)


class RemovedAccessTests(base.AccountsTestCase):
    """A person leaves the company. Every way in must close at once."""
    go = base.SingleSignOnTests.go   # a single sign-on attempt

    def setUp(self):
        super().setUp()
        self.admin = self.sign_in()
        self.app.create_user(self.a, "dave@acme.example", "dave-password-123", "accountant")
        self.daves_session = self.acc.login("dave@acme.example", "dave-password-123", "9.9.9.9")["access_token"]
        self.acc.admin_set_access(self.admin, "dave@acme.example", False)

    def test_every_door_is_closed(self):
        with self.assertRaises(AccountError):                      # the session he already had
            self.acc.authenticate(self.daves_session)
        with self.assertRaises(AccountError) as cm:                # his password
            self.acc.login("dave@acme.example", "dave-password-123", "9.9.9.9")
        self.assertEqual(cm.exception.message, "Wrong username or password.")   # no hint that he exists
        where, token = self.go("dave@acme.example")                # single sign-on
        self.assertIsNone(token)
        self.acc.forgot_password("dave@acme.example", "9.9.9.9")   # a reset link
        self.assertEqual(self.emails(), [])
        self.assertTrue(next(u for u in self.app.list_users(self.a) if u["username"] == "dave@acme.example")["disabled"])
        self.assertIn("access_removed", str(self.app.list_audit(self.a)))

    def test_access_can_be_given_back(self):
        self.acc.admin_set_access(self.admin, "dave@acme.example", True)
        self.assertTrue(self.acc.login("dave@acme.example", "dave-password-123", "9.9.9.9")["access_token"])
        with self.assertRaises(AccountError):                      # but the old session stays dead
            self.acc.authenticate(self.daves_session)

    def test_guards(self):
        cases = {"yourself": ("alice@acme.example", 409), "another company's user": ("carol@globex.example", 404),
                 "nobody": ("ghost", 404)}
        for case, (username, status) in cases.items():
            with self.assertRaises(AccountError, msg=case) as cm:
                self.acc.admin_set_access(self.admin, username, False)
            self.assertEqual(cm.exception.status, status, case)
        # The last admin can't be removed, even by a second admin who was just removed.
        self.app.create_user(self.a, "erin@acme.example", "erin-password-123", "admin")
        erin = self.sign_in("erin@acme.example", "erin-password-123")
        self.acc.admin_set_access(erin, "alice@acme.example", False)
        with self.assertRaises(AccountError) as cm:
            self.acc.admin_set_access(erin, "erin@acme.example", False)
        self.assertEqual(cm.exception.status, 409)


class TwoFactorAttackTests(base.AccountsTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.sign_in()
        self.secret, _ = self.turn_on_2fa(self.user)
        self.user = self.sign_in_2fa()

    def sign_in_2fa(self):
        from invoice_detector import totp
        pending = self.acc.login("alice@acme.example", PW, "1.2.3.4")["mfa_token"]
        # A minute later, so the code isn't the one already used during set-up.
        with mock.patch("invoice_detector.totp.time.time", return_value=time.time() + 60):
            code = totp.code_at(self.secret, totp.current_step())
            token = self.acc.login_second_factor(pending, code, "1.2.3.4")["access_token"]
        return self.acc.authenticate(token)

    def test_codes_cannot_be_guessed_through_the_recovery_codes_page(self):
        """With a hijacked session, guess the 6-digit code here (it used to be unlimited)
        and walk away with recovery codes: a permanent way past two-factor."""
        for _ in range(5):
            with self.assertRaises(AccountError) as cm:
                self.acc.new_recovery_codes(self.user, "000000")
            self.assertEqual(cm.exception.status, 401)
        with self.assertRaises(AccountError) as cm:
            self.acc.new_recovery_codes(self.user, "000000")
        self.assertEqual(cm.exception.status, 429)

    def test_one_code_one_use_even_at_the_same_instant(self):
        """Two requests carrying the same code arrive together. Both used to read "not used
        yet" before either wrote "used". The claim is now a single conditional update."""
        step = 99_999_999
        self.assertTrue(self.app.claim_totp_step(self.a, "alice@acme.example", step))
        self.assertFalse(self.app.claim_totp_step(self.a, "alice@acme.example", step))
        self.assertFalse(self.app.claim_totp_step(self.a, "alice@acme.example", step - 1))
        before = self.app.user_security(self.a, "alice@acme.example")["recovery_codes"]
        self.assertTrue(self.app.swap_recovery_codes(self.a, "alice@acme.example", before, before[1:]))
        self.assertFalse(self.app.swap_recovery_codes(self.a, "alice@acme.example", before, before[1:]))
        self.assertEqual(len(self.app.user_security(self.a, "alice@acme.example")["recovery_codes"]), len(before) - 1)


class EnumerationAndSpoofingTests(base.AccountsTestCase):
    def test_forgot_password_takes_the_same_time_for_everyone(self):
        """Sending an email takes a moment. If the server waits for it, a slow answer means
        "this account exists". The email now goes out in the background."""
        class SlowMailer:
            configured = True
            sent = []

            def send(self, to, subject, body):
                time.sleep(0.6)
                self.sent.append(to)
        self.acc.mailer = SlowMailer()
        timings = {}
        for who in ("alice@acme.example", "nobody@acme.example"):
            start = time.perf_counter()
            self.assertEqual(self.acc.forgot_password(who, "1.2.3.4"), {"email_enabled": True})
            timings[who] = time.perf_counter() - start
        self.assertLess(timings["alice@acme.example"], 0.3)
        self.assertLess(abs(timings["alice@acme.example"] - timings["nobody@acme.example"]), 0.25)
        self.acc.wait_for_mail()
        self.assertEqual(SlowMailer.sent, ["alice@acme.example"])

    def test_obvious_passwords_are_refused_everywhere_a_password_is_set(self):
        user = self.sign_in()
        for weak in ("password1234", "Password123!!", "qwertyuiop12", "123456789012", "aaaaaaaaaaaa"):
            self.assertIsNotNone(password_problem(weak), weak)
            with self.assertRaises(AccountError) as cm:
                self.acc.change_password(user, PW, weak)
            self.assertEqual(cm.exception.status, 422, weak)
            with self.assertRaises(AccountError):
                self.acc.admin_set_password(user, "bob", weak)
        with self.assertRaises(AccountError):
            self.acc.admin_set_password(user, "bob", "bob123456789")   # the username with digits
        self.assertIsNone(password_problem("correct horse battery staple", "bob"))
        self.assertTrue(self.acc.login("alice@acme.example", PW, "1.2.3.4"))   # nothing was changed


class SingleSignOnIdentityTests(unittest.TestCase):
    """Which claim proves who you are? An email address the provider hasn't verified does not."""

    def setUp(self):
        from fake_idp import FakeIdP
        self.idp = FakeIdP()

    def client(self, name, **kw):
        return OIDCClient(Provider(name, name, self.idp.issuer, self.idp.client_id, "s", **kw),
                          self.idp.get_json, self.idp.post_form)

    def test_microsoft_email_claim_is_not_trusted_on_its_own(self):
        """A colleague sets the 'email' on their Microsoft account to the admin's address.
        Their sign-in name (set by the directory) still says who they really are."""
        from invoice_detector.oidc import providers_from_env
        provider = providers_from_env({"MICROSOFT_CLIENT_ID": "x", "MICROSOFT_TENANT_ID": "tenant-1"})["microsoft"]
        client = OIDCClient(provider, self.idp.get_json, self.idp.post_form)
        spoofed = {"email": "admin@acme.example", "preferred_username": "mallory@acme.example"}
        self.assertEqual(client.email_from(spoofed), "mallory@acme.example")
        with self.assertRaises(SSOError):
            client.email_from({"email": "admin@acme.example"})           # nothing trustworthy at all
        verified = {"email": "admin@acme.example", "xms_edov": True}
        self.assertEqual(client.email_from(verified), "admin@acme.example")

    def test_an_address_marked_unverified_is_refused(self):
        client = self.client("sso", email_claims=("email", "preferred_username"))
        with self.assertRaises(SSOError):
            client.email_from({"email": "admin@acme.example", "email_verified": False})
        self.assertEqual(client.email_from({"email": "a@acme.example", "email_verified": True}), "a@acme.example")


class DenialOfServiceTests(unittest.TestCase):
    def brute_force(self, invoices):
        names = sorted({i.vendor for i in invoices})
        norm = detection._normalize_name
        return {frozenset((a, b)) for i, a in enumerate(names) for b in names[i + 1:]
                if SequenceMatcher(None, norm(a), norm(b)).ratio() >= DEFAULT_CONFIG.name_similarity}

    def pairs(self, anomalies):
        out = set()
        for a in anomalies:
            minor, major = a.detail.removeprefix("vendor '").removesuffix("'").split("' looks like existing vendor '")
            out.add(frozenset((minor, major)))
        return out

    def test_look_alike_check_finds_exactly_what_comparing_everything_finds(self):
        import random
        import string
        rng = random.Random(4)
        names = [" ".join("".join(rng.choice(string.ascii_lowercase) for _ in range(rng.randint(4, 9))).title()
                          for _ in range(rng.randint(2, 3))) for _ in range(250)]
        for n in names[:80]:   # look-alikes: one letter changed, added or dropped
            i = rng.randrange(len(n))
            names.append(rng.choice([n[:i] + "x" + n[i + 1:], n[:i] + "x" + n[i:], n[:i] + n[i + 1:]]))
        invoices = [Invoice(f"I{i}", n, 100.0, "2026-01-05") for i, n in enumerate(names)]
        self.assertEqual(self.pairs(find_similar_vendor_names(invoices)), self.brute_force(invoices))

    def test_thousands_of_vendors_no_longer_freeze_the_server(self):
        """One upload with thousands of distinct vendor names used to keep the server busy for
        minutes (1,500 vendors: 21 s; 50,000: hours), inside a web request."""
        import random
        import string
        rng = random.Random(5)
        invoices = [Invoice(f"I{i}", "".join(rng.choice(string.ascii_lowercase) for _ in range(12)), 50.0, "2026-01-05")
                    for i in range(6000)]
        start = time.perf_counter()
        find_similar_vendor_names(invoices)
        self.assertLess(time.perf_counter() - start, 15)

    def test_names_crafted_to_be_slow_hit_a_ceiling(self):
        crafted = [Invoice(f"I{i}", f"aaaa{i:06d}zzz", 50.0, "2026-01-05") for i in range(3000)]
        with mock.patch.object(detection, "EXACT_NAME_COMPARE_LIMIT", 100), \
                mock.patch.object(detection, "NAME_COMPARE_BUDGET", 200_000):
            start = time.perf_counter()
            find_similar_vendor_names(crafted)
            self.assertLess(time.perf_counter() - start, 10)


class HostileSpreadsheetTests(unittest.TestCase):
    def bomb(self) -> bytes:
        """A real workbook whose worksheet carries a "billion laughs" XML bomb: a few hundred
        bytes of nested definitions that expand to gigabytes in a naive XML parser."""
        entities = "".join(f'<!ENTITY l{i} "{("&l" + str(i - 1) + ";") * 10}">' for i in range(1, 10))
        doctype = f'<?xml version="1.0"?><!DOCTYPE worksheet [<!ENTITY l0 "lol">{entities}]>'
        source = zipfile.ZipFile(io.BytesIO(xlsx_bytes([["Invoice", "Vendor", "Amount", "Date"],
                                                         ["INV-1", "Acme", 10, "2026-01-05"]])))
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for item in source.infolist():
                data = source.read(item.filename)
                if item.filename == "xl/worksheets/sheet1.xml":
                    self.assertIn(b"INV-1", data)
                    data = doctype.encode() + data.replace(b"INV-1", b"&l9;")
                z.writestr(item, data)
        return out.getvalue()

    def test_xml_bomb_is_refused_quickly(self):
        start = time.perf_counter()
        with self.assertRaises(FileProblem):
            read_table("invoices.xlsx", self.bomb())
        self.assertLess(time.perf_counter() - start, 5)

    def test_protection_is_a_declared_requirement(self):
        """openpyxl only refuses XML bombs when defusedxml is installed, so it must be a
        dependency of this project rather than something that happens to be present."""
        import pathlib
        project = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"
        self.assertIn('"defusedxml', project.read_text(encoding="utf-8"))
        from openpyxl.xml import DEFUSEDXML
        self.assertTrue(DEFUSEDXML)


if __name__ == "__main__":
    unittest.main()


class SeveralServersTests(base.AccountsTestCase):
    """Two copies of the app behind a load balancer, sharing one database."""

    def second_server(self):
        from invoice_detector.accounts import Accounts
        return Accounts(self.app, self.signer, self.acc.settings, self.acc.mailer)

    def test_wrong_passwords_are_counted_across_servers(self):
        """With a counter in each server's memory, an attacker gets five guesses per server."""
        servers = [self.acc, self.second_server(), self.second_server()]
        for i in range(5):
            with self.assertRaises(AccountError) as cm:
                servers[i % 3].login("bob", "wrong-password-000", "6.6.6.6")
            self.assertEqual(cm.exception.status, 401)
        for server in servers:   # locked everywhere, even with the right password
            with self.assertRaises(AccountError) as cm:
                server.login("bob", "bob-password-12345", "6.6.6.6")
            self.assertEqual(cm.exception.status, 429)

    def test_sign_out_on_one_server_ends_the_session_on_the_others(self):
        other = self.second_server()
        token = self.acc.login("alice@acme.example", PW, "1.2.3.4")["access_token"]
        self.assertEqual(other.authenticate(token)["sub"], "alice@acme.example")
        other.logout(other.authenticate(token))
        with self.assertRaises(AccountError):
            self.acc.authenticate(token)


class SharedLimiterTests(base.AccountsTestCase):
    def test_window_lockout_and_expiry(self):
        limiter = self.app.limiter("t", max_attempts=3, window=100, lockout=50)
        for t in (0, 10, 20):
            limiter.check("k", now=1000 + t)
            limiter.record_failure("k", now=1000 + t)
        with self.assertRaises(Exception) as cm:
            limiter.check("k", now=1021)
        self.assertEqual(type(cm.exception).__name__, "RateLimitError")
        limiter.check("k", now=1071)            # the lockout has ended
        limiter.check("other", now=1021)        # other keys are unaffected
        limiter.record_failure("k", now=5000)   # a stale window starts again from one
        limiter.check("k", now=5001)

    def test_simultaneous_failures_all_count(self):
        import threading
        limiter = self.app.limiter("t2", max_attempts=1000, window=900)
        threads = [threading.Thread(target=lambda: [limiter.record_failure("k") for _ in range(25)]) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with self.app._tx() as run:
            rows = run("SELECT key, failures FROM rate_limits")
        mine = [r for r in rows if r["failures"] == 200]
        self.assertEqual(len(mine), 1)
        self.assertNotIn("k", [r["key"] for r in rows])                 # keys are stored hashed
        self.assertTrue(all(len(r["key"]) == 64 for r in rows))
