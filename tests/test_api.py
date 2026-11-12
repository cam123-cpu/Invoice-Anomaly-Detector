import base64
import importlib.util
import io
import unittest

from openpyxl import load_workbook

from support import DbTestCase, forget_cookies, make_settings

HAVE_API_DEPS = all(importlib.util.find_spec(m) for m in ("fastapi", "httpx"))
PW_A, PW_B = "alice-password-123", "bob-password-12345"


@unittest.skipUnless(HAVE_API_DEPS, "fastapi/httpx not installed")
class ApiTests(DbTestCase):
    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from invoice_detector.api import create_app

        self.client = forget_cookies(TestClient(create_app(make_settings(), db=self.app)))
        self.app.create_user(self.a, "alice", PW_A, "admin")
        self.app.create_user(self.a, "audit1", "auditor-password-123", "auditor")
        self.app.create_user(self.b, "bob", PW_B, "admin")

    def login(self, user, password):
        return self.client.post("/login", json={"username": user, "password": password})

    def auth(self, user, password):
        return {"Authorization": f"Bearer {self.login(user, password).json()['access_token']}"}

    def invoice(self, inv_id="INV-1", **kw):
        return {"id": inv_id, "vendor": "Acme", "amount": 100.0, "date": "2026-01-05",
                "bank_account": "ACCT1", **kw}

    def test_requires_login(self):
        for path in ("/invoices", "/review-queue", "/audit", "/users", "/metrics", "/settings", "/me"):
            self.assertEqual(self.client.get(path).status_code, 401, path)
        bad = {"Authorization": "Bearer x"}
        self.assertEqual(self.client.get("/invoices", headers=bad).status_code, 401)

    def test_login_rate_limit(self):
        for _ in range(5):
            self.assertEqual(self.login("alice", "wrong").status_code, 401)
        self.assertEqual(self.login("alice", PW_A).status_code, 429)

    def test_same_error_for_unknown_user_and_bad_password(self):
        a, b = self.login("nobody", "x"), self.login("alice", "x")
        self.assertEqual((a.status_code, a.json()), (b.status_code, b.json()))

    def test_roles_enforced(self):
        admin, auditor = self.auth("alice", PW_A), self.auth("audit1", "auditor-password-123")
        self.assertEqual(self.client.post("/invoices", json=self.invoice(), headers=admin).status_code, 201)
        denied = [("post", "/invoices", self.invoice("INV-2")), ("post", "/detect", None),
                  ("post", "/review/INV-1", {"verdict": "confirmed"}), ("get", "/users", None),
                  ("get", "/invoices/INV-1/bank", None), ("put", "/settings", {"approval_threshold": 5})]
        for method, path, body in denied:
            r = self.client.request(method, path, json=body, headers=auditor)
            self.assertEqual(r.status_code, 403, path)
        self.assertEqual(self.client.get("/invoices", headers=auditor).status_code, 200)
        self.assertEqual(self.client.get("/audit", headers=auditor).status_code, 200)
        bank = self.client.get("/invoices/INV-1/bank", headers=admin).json()
        self.assertEqual(bank["bank_account"], "ACCT1")

    def test_validation_and_duplicates(self):
        admin = self.auth("alice", PW_A)
        for bad in (self.invoice(amount=-1), self.invoice("bad id!"), self.invoice(date="05/01/2026")):
            self.assertEqual(self.client.post("/invoices", json=bad, headers=admin).status_code, 422)
        self.client.post("/invoices", json=self.invoice(), headers=admin)
        self.assertEqual(self.client.post("/invoices", json=self.invoice(), headers=admin).status_code, 409)
        self.assertEqual(self.client.get("/invoices?sort=amount;DROP", headers=admin).status_code, 422)
        weak = {"username": "newbie", "password": "short", "role": "auditor"}
        self.assertEqual(self.client.post("/users", json=weak, headers=admin).status_code, 422)

    def test_companies_are_isolated_through_the_api(self):
        alice, bob = self.auth("alice", PW_A), self.auth("bob", PW_B)
        self.assertEqual(self.client.get("/me", headers=alice).json()["company"], "Company A")
        # both companies can use the same invoice id
        self.assertEqual(self.client.post("/invoices", json=self.invoice(), headers=alice).status_code, 201)
        secret = self.invoice(vendor="Bob Secret Vendor", bank_account="BOB-ACCT")
        self.assertEqual(self.client.post("/invoices", json=secret, headers=bob).status_code, 201)
        self.client.post("/invoices", json=self.invoice("INV-B2", vendor="Bob Secret Vendor"), headers=bob)
        self.client.post("/detect", headers=bob)

        self.assertEqual([i["vendor"] for i in self.client.get("/invoices", headers=alice).json()], ["Acme"])
        self.assertEqual(self.client.get("/invoices/INV-1/bank", headers=alice).json()["bank_account"], "ACCT1")
        self.assertEqual(self.client.get("/invoices/INV-B2/bank", headers=alice).status_code, 404)
        self.assertEqual(self.client.get("/anomalies", headers=alice).json(), [])
        self.assertEqual(self.client.get("/review-queue", headers=alice).json(), [])
        review = self.client.post("/review/INV-B2", json={"verdict": "dismissed"}, headers=alice)
        self.assertEqual(review.status_code, 404)
        self.assertEqual([u["username"] for u in self.client.get("/users", headers=alice).json()],
                         ["alice", "audit1"])
        audit_text = str(self.client.get("/audit", headers=alice).json())
        self.assertNotIn("bob", audit_text)
        self.assertEqual(self.client.post("/detect", headers=alice).json()["invoices_checked"], 1)

    def test_admin_adds_users_to_own_company_only(self):
        alice = self.auth("alice", PW_A)
        new = {"username": "carol", "password": "carol-password-123", "role": "accountant"}
        self.assertEqual(self.client.post("/users", json=new, headers=alice).status_code, 201)
        self.assertEqual(self.client.post("/users", json=new, headers=alice).status_code, 409)
        me = self.client.get("/me", headers=self.auth("carol", "carol-password-123")).json()
        self.assertEqual((me["username"], me["role"], me["company"], me["two_factor_enabled"]),
                         ("carol", "accountant", "Company A", False))
        bob_users = [u["username"] for u in self.client.get("/users", headers=self.auth("bob", PW_B)).json()]
        self.assertEqual(bob_users, ["bob"])

    def test_detect_review_and_learning_flow(self):
        admin = self.auth("alice", PW_A)
        batch = [self.invoice("INV-1"), self.invoice("INV-2"),                  # exact duplicate
                 self.invoice("INV-3", vendor="Globex", amount=7000.0, date="2026-01-07")]  # round
        loaded = self.client.post("/invoices/bulk", json=batch + [self.invoice("INV-1")], headers=admin)
        self.assertEqual(loaded.json(), {"added": 3, "skipped": ["INV-1"]})

        result = self.client.post("/detect", headers=admin).json()
        self.assertEqual((result["invoices_checked"], result["invoices_flagged"]), (3, 2))
        queue = self.client.get("/review-queue", headers=admin).json()
        self.assertEqual([(q["invoice_id"], q["status"]) for q in queue], [("INV-2", "open"), ("INV-3", "open")])
        self.assertIn("duplicate", queue[0]["rules"])
        self.assertIn("INV-2", self.client.post("/explain", headers=admin).json()["explanation"])

        self.client.post("/review/INV-2", json={"verdict": "confirmed"}, headers=admin)
        self.client.post("/review/INV-3", json={"verdict": "dismissed"}, headers=admin)
        self.assertEqual(self.client.post("/review/INV-3", json={"verdict": "maybe"}, headers=admin).status_code, 422)
        self.assertEqual(self.client.post("/review/NOPE", json={"verdict": "confirmed"}, headers=admin).status_code, 404)

        queue = self.client.get("/review-queue", headers=admin).json()
        self.assertEqual([(q["invoice_id"], q["status"]) for q in queue], [("INV-2", "confirmed")])
        everything = self.client.get("/review-queue?include_dismissed=true", headers=admin).json()
        self.assertEqual(len(everything), 2)

        metrics = self.client.get("/metrics", headers=admin).json()
        self.assertEqual((metrics["reviewed"], metrics["confirmed"], metrics["precision"]), (2, 1, 0.5))
        round_rule = metrics["rules"]["round_amount"]
        self.assertEqual((round_rule["confirmed"], round_rule["dismissed"]), (0, 1))
        self.assertLess(round_rule["weight"], round_rule["base_weight"])

        actions = [e["action"] for e in self.client.get("/audit", headers=admin).json()]
        for expected in ("login", "invoices_imported", "detection_run", "invoice_reviewed"):
            self.assertIn(expected, actions)

    def test_company_threshold_changes_detection(self):
        admin = self.auth("alice", PW_A)
        self.client.post("/invoices", json=self.invoice(amount=480.0), headers=admin)
        self.client.post("/detect", headers=admin)
        self.assertEqual(self.client.get("/anomalies", headers=admin).json(), [])
        put = self.client.put("/settings", json={"approval_threshold": 500}, headers=admin)
        self.assertEqual(put.status_code, 200)
        self.assertEqual(self.client.get("/settings", headers=admin).json(), {"approval_threshold": 500, "require_2fa": False,
                                                                           "ai_extraction": False, "ai_available": False})
        self.client.post("/detect", headers=admin)
        rules = [a["rule"] for a in self.client.get("/anomalies", headers=admin).json()]
        self.assertEqual(rules, ["below_threshold"])
        bob = self.auth("bob", PW_B)
        self.assertEqual(self.client.get("/settings", headers=bob).json(), {"approval_threshold": 10000, "require_2fa": False,
                                                                         "ai_extraction": False, "ai_available": False})

    def upload(self, headers, text, filename="export.csv", **extra):
        body = {"filename": filename, "content_base64": base64.b64encode(text.encode()).decode(), **extra}
        return self.client.post("/import/preview" if "mapping" not in extra else "/import", json=body, headers=headers)

    def test_file_import_flow(self):
        admin = self.auth("alice", PW_A)
        csv_text = ("Invoice #,Supplier,Invoice Date,Total\n"
                    "Q1/001,Northwind,03/02/2026,\"$1,250.00\"\n"
                    "Q1/002,Northwind,03/02/2026,1250\n"
                    "Q1/003,Contoso,not a date,10\n")
        preview = self.upload(admin, csv_text).json()
        self.assertEqual(preview["mapping"]["vendor"], "Supplier")
        self.assertTrue(preview["date_order_ambiguous"])
        mapping = preview["mapping"]
        result = self.upload(admin, csv_text, mapping=mapping, date_order="DMY").json()
        self.assertEqual((result["added"], result["error_count"]), (2, 1))
        self.assertEqual(result["errors"][0]["row"], 4)
        self.assertEqual(result["detection"]["flagged_from_file"], 1)  # the duplicate
        again = self.upload(admin, csv_text, mapping=mapping, date_order="DMY").json()
        self.assertEqual((again["added"], again["already_stored"], again["detection"]), (0, 2, None))
        stored = {i["id"]: i["invoice_date"] for i in self.client.get("/invoices", headers=admin).json()}
        self.assertEqual(stored, {"Q1/001": "2026-02-03", "Q1/002": "2026-02-03"})
        # ids with slashes work in the per-invoice URLs too
        self.assertEqual(self.client.post("/review/Q1%2F002", json={"verdict": "confirmed"}, headers=admin).status_code, 200)
        self.assertEqual(self.client.get("/invoices/Q1%2F001/bank", headers=admin).json()["bank_account"], None)

        summary = self.client.get("/summary", headers=admin).json()
        self.assertEqual((summary["invoices"], summary["confirmed"], summary["company"]), (2, 1, "Company A"))
        self.assertIsNotNone(summary["last_import"])
        queue = self.client.get("/review-queue", headers=admin).json()
        self.assertEqual((queue[0]["vendor"], queue[0]["amount"], queue[0]["date"]), ("Northwind", 1250.0, "2026-02-03"))

        bad_map = dict(mapping, date=None)
        self.assertIn("Invoice date", self.upload(admin, csv_text, mapping=bad_map).json()["detail"])
        self.assertEqual(self.upload(admin, "x", filename="notes.pdf").status_code, 422)
        broken = {"filename": "a.csv", "content_base64": "not base64!!"}
        self.assertEqual(self.client.post("/import/preview", json=broken, headers=admin).status_code, 422)
        auditor = self.auth("audit1", "auditor-password-123")
        self.assertEqual(self.upload(auditor, csv_text).status_code, 403)
        bob = self.auth("bob", PW_B)
        self.assertEqual(self.client.get("/summary", headers=bob).json()["invoices"], 0)

    def test_web_app_is_served_with_a_strict_policy(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn('src="/app/app.js"', page.text)
        csp = page.headers["content-security-policy"]
        for directive in ("default-src 'self'", "script-src 'self'", "frame-ancestors 'none'"):
            self.assertIn(directive, csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertEqual(page.headers["x-content-type-options"], "nosniff")
        js = self.client.get("/app/app.js")
        self.assertEqual((js.status_code, js.headers["content-type"].split(";")[0]), (200, "text/javascript"))
        self.assertNotIn("innerHTML", js.text)  # all server text is inserted as plain text
        self.assertEqual(self.client.get("/app/app.css").status_code, 200)
        for bad in ("/app/index.html", "/app/../db.py", "/app/secret"):
            self.assertEqual(self.client.get(bad).status_code, 404, bad)
        self.assertEqual(self.client.get("/health").headers["x-frame-options"], "DENY")

    def test_excel_round_trip(self):
        admin, bob = self.auth("alice", PW_A), self.auth("bob", PW_B)
        csv_text = ("Invoice,Vendor,Date,Amount\n"
                    "E1,=1+2 Corp,2026-01-05,500\n"
                    "E2,=1+2 Corp,2026-01-05,500\n"
                    "E3,Globex,2026-01-07,12.5\n"
                    "E4,Globex,someday,1\n")
        body = {"filename": 'ap "march"\r\nX-Evil: 1.csv',
                "content_base64": base64.b64encode(csv_text.encode()).decode(),
                "mapping": {"id": "Invoice", "vendor": "Vendor", "date": "Date", "amount": "Amount"},
                "date_order": "MDY"}
        self.assertEqual(self.client.post("/import", json=body, headers=admin).json()["added"], 3)

        r = self.client.post("/export/annotated", json=body, headers=admin)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("application/vnd.openxmlformats"))
        disposition = r.headers["content-disposition"]
        self.assertNotIn("\n", disposition)
        self.assertNotIn("X-Evil:", r.headers)
        self.assertIn('filename="ap _march___X-Evil_ 1 - checked.xlsx"', disposition)
        wb = load_workbook(io.BytesIO(r.content))
        ws = wb["Invoices"]
        self.assertEqual([ws.cell(1, c).value for c in range(5, 11)],
                         ["Risk", "Review status", "Payment decision", "Flags", "Why flagged", "What to do"])
        statuses = [ws.cell(row, 6).value for row in range(2, 6)]
        self.assertEqual(statuses[2:], ["No flags", "Not imported"])
        self.assertEqual(ws.cell(3, 7).value, "Review first")
        self.assertIn("Duplicate", ws.cell(3, 8).value)
        self.assertEqual((ws.cell(2, 2).data_type, ws.cell(2, 2).value), ("s", "'=1+2 Corp"))

        report = self.client.get("/export/review.xlsx", headers=admin)
        rq = load_workbook(io.BytesIO(report.content))["Review queue"]
        self.assertEqual(rq.cell(2, 2).value, "E2")
        self.assertEqual(rq.cell(2, 3).data_type, "s")
        self.assertIn('filename="review-queue-company-a.xlsx"', report.headers["content-disposition"])
        actions = [e["action"] for e in self.client.get("/audit", headers=admin).json()]
        self.assertIn("workbook_exported", actions)
        self.assertIn("report_exported", actions)

        # another company gets nothing from the same file or the report
        theirs = load_workbook(io.BytesIO(self.client.post("/export/annotated", json=body, headers=bob).content))["Invoices"]
        self.assertEqual([theirs.cell(row, 5).value for row in range(2, 6)], [None] * 4)
        self.assertEqual([theirs.cell(row, 6).value for row in range(2, 5)], [None] * 3)
        bob_report = load_workbook(io.BytesIO(self.client.get("/export/review.xlsx", headers=bob).content))
        self.assertEqual(bob_report["Review queue"].max_row, 1)

        self.assertEqual(self.client.post("/export/annotated", json=body).status_code, 401)
        self.assertEqual(self.client.get("/export/review.xlsx").status_code, 401)

    def test_sample_files(self):
        listing = self.client.get("/samples").json()  # synthetic data, no sign-in needed
        self.assertEqual(len(listing), 4)
        for item in listing:
            r = self.client.get("/samples/" + item["name"])
            self.assertEqual(r.status_code, 200)
            self.assertTrue(load_workbook(io.BytesIO(r.content)).sheetnames)
        self.assertEqual(self.client.get("/samples/../api.py").status_code, 404)
        self.assertEqual(self.client.get("/samples/nope.xlsx").status_code, 404)

    def test_sheet_choice(self):
        admin = self.auth("alice", PW_A)
        from invoice_detector.samples import build
        body = {"filename": "ap-register-2026.xlsx", "content_base64": base64.b64encode(build("ap-register-2026.xlsx")).decode()}
        first = self.client.post("/import/preview", json=body, headers=admin).json()
        self.assertEqual((first["sheet"], len(first["sheets"])), ("Q1 2026", 4))
        second = self.client.post("/import/preview", json={**body, "sheet": "Q3 2026"}, headers=admin).json()
        self.assertEqual(second["sheet"], "Q3 2026")
        missing = self.client.post("/import/preview", json={**body, "sheet": "Q9"}, headers=admin)
        self.assertEqual(missing.status_code, 422)

    def test_confirming_sets_the_payment_decision(self):
        admin, bob = self.auth("alice", PW_A), self.auth("bob", PW_B)
        batch = [self.invoice("D1", amount=500.0), self.invoice("D2", amount=500.0),   # duplicate
                 self.invoice("R1", vendor="Globex", amount=7000.0, date="2026-01-07",   # round amount
                              bank_account="ACCT2"),
                 self.invoice("OK", vendor="Initech", amount=12.34, date="2026-01-08",  # nothing wrong
                              bank_account="ACCT3")]
        self.client.post("/invoices/bulk", json=batch, headers=admin)
        self.client.post("/detect", headers=admin)
        queue = {q["invoice_id"]: q for q in self.client.get("/review-queue", headers=admin).json()}
        self.assertEqual((queue["D2"]["decision"], queue["D2"]["suggested_action"]), ("review", "do_not_pay"))
        self.assertTrue(queue["D2"]["fixes"][0].startswith("Don't pay"))

        r = self.client.post("/review/D2", json={"verdict": "confirmed"}, headers=admin).json()
        self.assertEqual((r["status"], r["action"]), ("confirmed", "do_not_pay"))
        r = self.client.post("/review/R1", json={"verdict": "confirmed", "action": "hold",
                                                "note": "asked for breakdown"}, headers=admin).json()
        self.assertEqual(r["action"], "hold")
        bad = self.client.post("/review/R1", json={"verdict": "confirmed", "action": "shred"}, headers=admin)
        self.assertEqual(bad.status_code, 422)
        long_note = self.client.post("/review/R1", json={"verdict": "confirmed", "note": "x" * 501}, headers=admin)
        self.assertEqual(long_note.status_code, 422)

        queue = {q["invoice_id"]: q for q in self.client.get("/review-queue", headers=admin).json()}
        self.assertEqual((queue["R1"]["decision"], queue["R1"]["note"]), ("hold", "asked for breakdown"))
        summary = self.client.get("/summary", headers=admin).json()
        self.assertEqual(summary["decisions"], {"pay": 2, "review": 0, "hold": 1, "do_not_pay": 1})
        self.assertEqual(summary["amounts"], {"pay": 512.34, "review": 0.0, "hold": 7000.0, "do_not_pay": 500.0})

        r = self.client.get("/export/payment-file.xlsx", headers=admin)
        self.assertIn('filename="payment-file-company-a.xlsx"', r.headers["content-disposition"])
        wb = load_workbook(io.BytesIO(r.content))
        self.assertEqual([wb["Do not pay"].cell(2, 1).value, wb["On hold"].cell(2, 9).value],
                         ["D2", "asked for breakdown"])
        self.assertEqual(sorted(wb["Ready to pay"].cell(row, 1).value for row in (2, 3)), ["D1", "OK"])
        theirs = load_workbook(io.BytesIO(self.client.get("/export/payment-file.xlsx", headers=bob).content))
        self.assertEqual(theirs["Summary"].cell(9, 2).value, 0)  # another company: no invoices at all
        self.assertEqual(self.client.get("/export/payment-file.xlsx").status_code, 401)

        auditor = self.auth("audit1", "auditor-password-123")
        self.assertEqual(self.client.post("/review/D2", json={"verdict": "dismissed"}, headers=auditor).status_code, 403)
        self.assertEqual(self.client.get("/export/payment-file.xlsx", headers=auditor).status_code, 200)
        actions = [e["detail"] for e in self.client.get("/audit", headers=admin).json() if e["action"] == "invoice_reviewed"]
        self.assertIn("D2: confirmed, do_not_pay", actions)




@unittest.skipUnless(HAVE_API_DEPS, "fastapi/httpx not installed")
class SignInApiTests(DbTestCase):
    """The HTTP wiring of sign-in; the rules themselves are tested in test_accounts.py."""

    def setUp(self):
        super().setUp()
        import tempfile
        from dataclasses import replace

        from fake_idp import FakeIdP
        from fastapi.testclient import TestClient
        from invoice_detector.accounts import Accounts
        from invoice_detector.api import create_app
        from invoice_detector.mailer import FolderMailer
        from invoice_detector.oidc import OIDCClient, Provider
        from invoice_detector.security import TokenSigner

        self.mail_dir = tempfile.mkdtemp()
        self.idp = FakeIdP()
        self.settings = replace(make_settings(), public_url="https://app.example", mail_dir=self.mail_dir)
        accounts = Accounts(self.app, TokenSigner(self.settings.secret_key), self.settings, FolderMailer(self.mail_dir),
                            {"sso": OIDCClient(Provider("sso", "Okta", self.idp.issuer, self.idp.client_id, "s"),
                                               self.idp.get_json, self.idp.post_form)})
        self.make_app = lambda settings=self.settings: create_app(settings, db=self.app, accounts=accounts)
        self.client = forget_cookies(TestClient(self.make_app(), base_url="https://testserver"))
        self.TestClient = TestClient
        self.app.create_user(self.a, "alice@acme.example", PW_A, "admin")
        self.app.create_user(self.a, "dana@acme.example", "dana-password-1234", "accountant")

    def token(self, user="alice@acme.example", password=PW_A):
        return self.client.post("/login", json={"username": user, "password": password}).json()["access_token"]

    def h(self, token):
        return {"Authorization": f"Bearer {token}"}

    def code(self, secret, ahead=0):
        from invoice_detector import totp
        return totp.code_at(secret.replace(" ", ""), totp.current_step() + ahead)

    def enable(self, token):
        setup = self.client.post("/me/2fa/setup", headers=self.h(token)).json()
        done = self.client.post("/me/2fa/enable", json={"code": self.code(setup["secret"])}, headers=self.h(token)).json()
        return setup["secret"], done

    def test_two_factor(self):
        token = self.token()
        self.assertFalse(self.client.get("/me", headers=self.h(token)).json()["two_factor_enabled"])
        setup = self.client.post("/me/2fa/setup", headers=self.h(token)).json()
        self.assertTrue(setup["qr"].startswith("data:image/svg+xml;base64,"))
        bad = self.client.post("/me/2fa/enable", json={"code": "000000"}, headers=self.h(token))
        self.assertEqual(bad.status_code, 400)
        done = self.client.post("/me/2fa/enable", json={"code": self.code(setup["secret"])}, headers=self.h(token)).json()
        self.assertEqual(len(done["recovery_codes"]), 10)
        first = self.client.post("/login", json={"username": "alice@acme.example", "password": PW_A}).json()
        self.assertEqual(set(first), {"mfa_required", "mfa_token"})
        wrong = self.client.post("/login/2fa", json={"mfa_token": first["mfa_token"], "code": "000000"})
        self.assertEqual(wrong.status_code, 401)
        ok = self.client.post("/login/2fa", json={"mfa_token": first["mfa_token"], "code": self.code(setup["secret"], 1)})
        me = self.client.get("/me", headers=self.h(ok.json()["access_token"])).json()
        self.assertEqual((me["two_factor_enabled"], me["signed_in_with_two_factor"]), (True, True))
        self.assertEqual(self.client.get("/me/2fa", headers=self.h(ok.json()["access_token"])).json()["recovery_codes_left"], 10)

    def test_company_requires_two_factor(self):
        admin = self.token()
        refused = self.client.put("/settings", json={"require_2fa": True}, headers=self.h(admin))
        self.assertEqual(refused.status_code, 409)  # turn it on for yourself first
        _, done = self.enable(admin)
        admin = done["access_token"]
        self.assertTrue(self.client.put("/settings", json={"require_2fa": True}, headers=self.h(admin)).json()["require_2fa"])
        dana = self.token("dana@acme.example", "dana-password-1234")
        blocked = self.client.get("/invoices", headers=self.h(dana))
        self.assertEqual((blocked.status_code, blocked.headers.get("x-requires-2fa")), (403, "1"))
        self.assertTrue(self.client.get("/me", headers=self.h(dana)).json()["two_factor_required"])
        _, dana_done = self.enable(dana)
        self.assertEqual(self.client.get("/invoices", headers=self.h(dana_done["access_token"])).status_code, 200)
        users = {u["username"]: u for u in self.client.get("/users", headers=self.h(admin)).json()}
        self.assertTrue(users["dana@acme.example"]["two_factor"])
        self.assertEqual(self.client.post("/users/dana@acme.example/2fa/reset", headers=self.h(admin)).status_code, 200)
        self.assertEqual(self.client.post("/users/nobody@x.example/2fa/reset", headers=self.h(admin)).status_code, 404)

    def test_passwords(self):
        import os
        from urllib.parse import unquote
        old = self.token("dana@acme.example", "dana-password-1234")
        short = self.client.post("/me/password", json={"current_password": "dana-password-1234", "new_password": "short"},
                                 headers=self.h(old))
        self.assertEqual(short.status_code, 422)
        new = self.client.post("/me/password", json={"current_password": "dana-password-1234",
                                                     "new_password": "dana-new-password-1"}, headers=self.h(old)).json()
        self.assertEqual(self.client.get("/me", headers=self.h(old)).status_code, 401)  # other sessions end
        self.assertEqual(self.client.get("/me", headers=self.h(new["access_token"])).status_code, 200)

        self.assertEqual(self.client.post("/password/forgot", json={"username": "dana@acme.example"}).json(),
                         {"email_enabled": True})
        self.assertEqual(self.client.post("/password/forgot", json={"username": "ghost@acme.example"}).json(),
                         {"email_enabled": True})
        mail = [open(os.path.join(self.mail_dir, f), encoding="utf-8").read() for f in os.listdir(self.mail_dir)]
        self.assertEqual(len(mail), 1)
        link = next(line for line in mail[0].splitlines() if "#/reset?token=" in line)
        self.assertTrue(link.startswith("https://app.example/#/reset?token="))
        reset = self.client.post("/password/reset", json={"token": unquote(link.split("token=")[1]),
                                                          "new_password": "dana-third-password"})
        self.assertEqual(reset.status_code, 200)
        self.assertTrue(self.token("dana@acme.example", "dana-third-password"))

        admin = self.token()
        self.client.post("/users/dana@acme.example/password", json={"new_password": "temporary-pass-123"}, headers=self.h(admin))
        self.assertTrue(self.token("dana@acme.example", "temporary-pass-123"))
        sso_user = {"username": "Eve@Acme.example", "role": "auditor"}
        made = self.client.post("/users", json=sso_user, headers=self.h(admin)).json()
        self.assertEqual((made["username"], made["sso_only"]), ("eve@acme.example", True))
        self.assertEqual(self.client.post("/users", json=sso_user, headers=self.h(admin)).status_code, 409)

    def test_single_sign_on(self):
        from urllib.parse import parse_qs, urlparse
        self.assertEqual(self.client.get("/auth/providers").json(), [{"name": "sso", "label": "Okta"}])
        browser = self.TestClient(self.make_app(), base_url="https://testserver")  # keeps cookies
        start = browser.get("/auth/sso/start", follow_redirects=False)
        self.assertEqual(start.status_code, 302)
        cookie = start.headers["set-cookie"].lower()
        for attribute in ("httponly", "samesite=lax", "path=/auth/", "secure"):
            self.assertIn(attribute, cookie)
        q = parse_qs(urlparse(start.headers["location"]).query)
        code = self.idp.approve("alice@acme.example", q["nonce"][0])
        back = browser.get(f"/auth/sso/callback?code={code}&state={q['state'][0]}", follow_redirects=False)
        self.assertEqual((back.status_code, back.headers["location"]), (302, "/#/dashboard"))  # no token in the address
        self.assertIn("__host-iad_session=", back.headers["set-cookie"].lower())
        me = browser.get("/me").json()  # signed in by the session cookie alone
        self.assertEqual((me["username"], me["signed_in_with_two_factor"]), ("alice@acme.example", True))
        replay = browser.get(f"/auth/sso/callback?code={code}&state={q['state'][0]}", follow_redirects=False)
        where = replay.headers["location"]   # cookie cleared, code used: back to sign-in with a signed message
        self.assertTrue(where.startswith("/#/login?notice="))
        message = browser.get("/auth/notice", params={"token": where.split("notice=")[1]}).json()["message"]
        self.assertIn("could not be verified", message)
        self.assertEqual(self.client.get("/auth/google/start", follow_redirects=False).status_code, 404)

    def test_https_enforcement(self):
        from dataclasses import replace
        secure = self.TestClient(self.make_app(replace(self.settings, force_https=True)), base_url="http://testserver")
        r = secure.get("/me", follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (308, "https://testserver/me"))
        self.assertEqual(secure.get("/health").status_code, 200)  # load-balancer checks stay on http
        tls = self.TestClient(self.make_app(replace(self.settings, force_https=True)), base_url="https://testserver")
        self.assertIn("max-age=31536000", tls.get("/health").headers["strict-transport-security"])
        self.assertNotIn("strict-transport-security", self.client.get("/health").headers)
        self.assertEqual(tls.get("/health/ready").json(), {"status": "ready"})

    def test_browser_session_cookie(self):
        browser = self.TestClient(self.make_app(), base_url="https://testserver")
        signed = browser.post("/login", json={"username": "alice@acme.example", "password": PW_A},
                              headers={"X-Requested-With": "fetch"})
        cookie = signed.headers["set-cookie"].lower()
        for attribute in ("__host-iad_session=", "httponly", "secure", "samesite=strict", "path=/"):
            self.assertIn(attribute, cookie)
        self.assertEqual(browser.get("/me").json()["username"], "alice@acme.example")
        # Changes signed only by the cookie need the header another site can't add...
        change = "/me/2fa/setup"  # any change will do; this one is harmless
        self.assertEqual(browser.post(change).status_code, 403)
        fetch = {"X-Requested-With": "fetch"}
        self.assertEqual(browser.post(change, headers=fetch).status_code, 200)
        # ...and must not come from another site.
        evil = {"X-Requested-With": "fetch", "Origin": "https://evil.example"}
        self.assertEqual(browser.post(change, headers=evil).status_code, 403)
        self.assertEqual(browser.post(change, headers={**fetch, "Origin": "https://testserver"}).status_code, 200)
        # Programs using a bearer token aren't affected.
        self.assertEqual(self.client.post(change, headers=self.h(self.token())).status_code, 200)
        out = browser.post("/logout", headers=fetch)
        self.assertIn("max-age=0", out.headers["set-cookie"].lower())
        self.assertEqual(browser.get("/me").status_code, 401)

    def test_sign_out_ends_the_session_on_the_server(self):
        """A copy of the token must stop working when its owner signs out."""
        token = self.token()
        self.assertEqual(self.client.get("/me", headers=self.h(token)).status_code, 200)
        self.assertEqual(self.client.post("/logout", headers=self.h(token)).json(), {"signed_out": True})
        self.assertEqual(self.client.get("/me", headers=self.h(token)).status_code, 401)
        self.assertEqual(self.client.post("/logout").status_code, 200)   # signing out twice is harmless

    def test_the_web_app_is_never_handed_a_token(self):
        browser = self.TestClient(self.make_app(), base_url="https://testserver")
        signed = browser.post("/login", json={"username": "alice@acme.example", "password": PW_A},
                              headers={"X-Requested-With": "fetch"})
        self.assertEqual(signed.json(), {"signed_in": True})
        self.assertIn("httponly", signed.headers["set-cookie"].lower())
        program = self.client.post("/login", json={"username": "alice@acme.example", "password": PW_A})
        self.assertIn("access_token", program.json())

    def test_removing_a_persons_access(self):
        admin = self.h(self.token())
        made = self.client.post("/users", json={"username": "leaver@acme.example", "role": "accountant",
                                                "password": "leaver-password-1"}, headers=admin)
        self.assertEqual(made.status_code, 201)
        leaver = self.h(self.client.post("/login", json={"username": "leaver@acme.example",
                                                         "password": "leaver-password-1"}).json()["access_token"])
        self.assertEqual(self.client.post("/users/leaver@acme.example/access", json={"enabled": False},
                                          headers=leaver).status_code, 403)          # not an admin
        off = self.client.post("/users/leaver@acme.example/access", json={"enabled": False}, headers=admin)
        self.assertEqual(off.json(), {"username": "leaver@acme.example", "disabled": True})
        self.assertEqual(self.client.get("/me", headers=leaver).status_code, 401)   # signed out at once
        again = self.client.post("/login", json={"username": "leaver@acme.example", "password": "leaver-password-1"})
        self.assertEqual(again.status_code, 401)
        listed = {u["username"]: u["disabled"] for u in self.client.get("/users", headers=admin).json()}
        self.assertTrue(listed["leaver@acme.example"])
        own = self.client.post("/users/alice@acme.example/access", json={"enabled": False}, headers=admin)
        self.assertEqual(own.status_code, 409)

    def test_sign_in_page_only_shows_messages_the_server_signed(self):
        for forged in ("Your account is locked. Call 555-0100.", "a.b", ""):
            self.assertIn(self.client.get("/auth/notice", params={"token": forged}).status_code, (400, 422))

    def test_obvious_passwords_are_refused(self):
        admin = self.h(self.token())
        for weak in ("password1234", "qwertyuiop12", "weakling1234"):
            r = self.client.post("/users", json={"username": "weakling@acme.example", "role": "auditor",
                                                 "password": weak}, headers=admin)
            self.assertEqual(r.status_code, 422, weak)

    def test_unknown_fields_are_refused(self):
        token = self.token()
        tampered = {"username": "zed@acme.example", "role": "auditor", "password": "zed-password-1234",
                    "org_id": "someone-else"}
        r = self.client.post("/users", json=tampered, headers=self.h(token))
        self.assertEqual(r.status_code, 422)
        self.assertIn("org_id", r.text)
        login = self.client.post("/login", json={"username": "alice@acme.example", "password": PW_A, "role": "admin"})
        self.assertEqual(login.status_code, 422)

    def test_security_headers_and_caching(self):
        api = self.client.get("/me", headers=self.h(self.token()))
        page = self.client.get("/")
        for r in (api, page):
            self.assertEqual(r.headers["x-content-type-options"], "nosniff")
            self.assertEqual(r.headers["x-frame-options"], "DENY")
            self.assertEqual(r.headers["cross-origin-opener-policy"], "same-origin")
            self.assertIn("camera=()", r.headers["permissions-policy"])
        self.assertEqual(api.headers["content-security-policy"], "default-src 'none'; frame-ancestors 'none'")
        self.assertEqual(api.headers["cache-control"], "no-store")
        self.assertIn("script-src 'self'", page.headers["content-security-policy"])

    def test_request_size_limits(self):
        from invoice_detector.api import MAX_BODY
        big = self.client.post("/login", content=b"{" + b" " * (MAX_BODY + 1) + b"}",
                               headers={"Content-Type": "application/json"})
        self.assertEqual(big.status_code, 413)

        def chunks():
            yield b'{"username": "a", '
            yield b'"password": "b"}'
        chunked = self.client.post("/login", content=chunks(), headers={"Content-Type": "application/json"})
        self.assertEqual(chunked.status_code, 411)

    def test_api_docs_can_be_turned_off(self):
        from dataclasses import replace
        self.assertEqual(self.client.get("/openapi.json").status_code, 200)
        closed = self.TestClient(self.make_app(replace(self.settings, api_docs=False)))
        self.assertEqual(closed.get("/docs").status_code, 404)
        self.assertEqual(closed.get("/openapi.json").status_code, 404)

    def test_every_route_needs_sign_in_except_the_public_ones(self):
        """A new route that forgets its sign-in check fails this test."""
        public = {"/", "/app", "/app/{name}", "/samples", "/samples/{name}", "/samples/documents",
                  "/samples/documents/{name}", "/health", "/health/ready",
                  "/login", "/login/2fa", "/logout", "/auth/providers", "/auth/notice", "/auth/{name}/start",
                  "/auth/{name}/callback", "/password/forgot", "/password/reset",
                  "/docs", "/docs/oauth2-redirect", "/openapi.json"}
        checked = 0
        for route in self.make_app().routes:
            path, methods = getattr(route, "path", None), getattr(route, "methods", None) or set()
            if path in public or not methods:
                continue
            url = path
            for param in ("{username}", "{invoice_id:path}", "{inv_id:path}", "{name}"):
                url = url.replace(param, "x")
            for method in methods - {"HEAD"}:
                r = self.client.request(method, url, json={})
                self.assertEqual(r.status_code, 401, f"{method} {path}")
                checked += 1
        self.assertGreater(checked, 25)



@unittest.skipUnless(HAVE_API_DEPS, "fastapi/httpx not installed")
class DocumentApiTests(DbTestCase):
    """Invoice documents end to end over HTTP, with files read in an isolated process."""

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from invoice_detector.api import create_app
        from invoice_detector.doc_samples import sample_docs

        self.client = forget_cookies(TestClient(create_app(make_settings(), db=self.app)))
        self.app.create_user(self.a, "alice", PW_A, "admin")
        self.app.create_user(self.a, "audit1", "auditor-password-123", "auditor")
        self.h = {"Authorization": "Bearer " + self.client.post(
            "/login", json={"username": "alice", "password": PW_A}).json()["access_token"]}
        self.samples = {name: data for name, data, _ in sample_docs()}

    def extract(self, name, data=None, headers=None):
        body = {"filename": name, "content_base64": base64.b64encode(data or self.samples[name]).decode()}
        return self.client.post("/documents/extract", json=body, headers=headers or self.h)

    def test_read_check_and_import(self):
        drafts = [self.extract(n).json() for n in ("umbrella-ltd-3307.xlsx", "stark-freight-8811.pdf")]
        self.assertEqual([d["status"] for d in drafts], ["ready", "ready"])
        self.assertTrue(any("add up" in w for w in drafts[0]["warnings"]))
        items = [{"draft": d["draft"], "file": d["file"], **d["fields"]} for d in drafts]
        result = self.client.post("/documents/import", json={"items": items}, headers=self.h).json()
        self.assertEqual(result["added"], 2)
        self.assertEqual(result["detection"]["invoices_checked"], 2)
        queue = self.client.get("/review-queue", headers=self.h).json()
        self.assertIn("totals_mismatch", next(q for q in queue if q["invoice_id"] == "3307")["rules"])
        self.assertEqual(self.client.get("/documents/stats", headers=self.h).json()["documents"], 2)
        self.assertEqual(self.extract("umbrella-ltd-3307.xlsx").json()["status"], "already_imported")

    def test_a_credit_note_and_the_invoice_it_cancels(self):
        drafts = [self.extract(n).json() for n in ("globex-corp-7730.pdf", "globex-corp-credit-note-118.pdf")]
        self.assertEqual([d["kind"] for d in drafts], ["invoice", "credit"])
        self.assertGreater(drafts[1]["fields"]["amount"], 0)          # read as a positive amount
        items = [{"draft": d["draft"], "file": d["file"], "kind": d["kind"], **d["fields"]} for d in drafts]
        result = self.client.post("/documents/import", json={"items": items}, headers=self.h).json()
        self.assertEqual((result["added"], result["fields_corrected"]), (2, 0))
        queue = {q["invoice_id"]: q for q in self.client.get("/review-queue", headers=self.h).json()}
        bill, credit = drafts[0]["fields"]["id"], drafts[1]["fields"]["id"]
        self.assertEqual((queue[bill]["rules"], queue[bill]["kind"]), (["credited_invoice"], "invoice"))
        self.assertEqual((queue[credit]["rules"], queue[credit]["kind"]), (["matched_credit"], "credit"))
        self.assertEqual((queue[bill]["decision"], queue[bill]["suggested_action"]), ("review", "do_not_pay"))
        self.assertLess(queue[credit]["amount"], 0)                   # shown signed
        summary = self.client.get("/summary", headers=self.h).json()
        amount = drafts[1]["fields"]["amount"]
        self.assertEqual(summary["credit_notes"], {"count": 1, "amount": amount, "to_apply": 0.0})
        self.assertEqual(round(sum(summary["amounts"].values()), 2), 0.0)   # the pair nets to nothing
        listed = {i["id"]: i for i in self.client.get("/invoices", headers=self.h).json()}
        self.assertEqual((listed[credit]["kind"], listed[credit]["amount"]), ("credit", -amount))

    def test_credit_notes_through_the_api_and_a_list(self):
        body = {"id": "CN-1", "vendor": "Acme", "amount": 25.0, "date": "2026-01-09", "kind": "credit"}
        self.assertIn(self.client.post("/invoices", json=body, headers=self.h).status_code, (200, 201))
        self.assertEqual(self.client.post("/invoices", json=dict(body, id="CN-2", kind="refund"),
                                          headers=self.h).status_code, 422)
        csv_text = "Invoice No,Vendor,Date,Amount\nB-1,Acme,2026-01-05,100.00\nCN-9,Acme,2026-01-12,-40.00\n"
        payload = {"filename": "a.csv", "content_base64": base64.b64encode(csv_text.encode()).decode(),
                   "mapping": {"id": "Invoice No", "vendor": "Vendor", "date": "Date", "amount": "Amount"},
                   "date_order": "MDY", "run_detection": True}
        result = self.client.post("/import", json=payload, headers=self.h).json()
        self.assertEqual((result["added"], result["credit_notes"]), (2, 1))
        summary = self.client.get("/summary", headers=self.h).json()
        self.assertEqual(summary["credit_notes"]["count"], 2)
        self.assertEqual(summary["amounts"]["pay"] + summary["amounts"]["review"] + summary["amounts"]["hold"]
                         + summary["amounts"]["do_not_pay"], 35.0)   # 100 - 40 - 25

    def test_unreadable_and_forbidden(self):
        self.assertEqual(self.extract("x.pdf", b"%PDF-1.4 broken").json()["status"], "unreadable")
        auditor = {"Authorization": "Bearer " + self.client.post(
            "/login", json={"username": "audit1", "password": "auditor-password-123"}).json()["access_token"]}
        self.assertEqual(self.extract("stark-freight-8811.pdf", headers=auditor).status_code, 403)
        bad = self.client.post("/documents/extract", json={"filename": "x.pdf", "content_base64": "%%%"},
                               headers=self.h)
        self.assertEqual(bad.status_code, 422)

    def test_an_invoice_form_uploaded_as_a_list_is_recognised(self):
        from doc_templates import TEMPLATES
        data, _ = TEMPLATES["Simple business invoice"]()
        body = {"filename": "hlp.xlsx", "content_base64": base64.b64encode(data).decode()}
        self.assertTrue(self.client.post("/import/preview", json=body, headers=self.h).json()["looks_like_document"])

    def test_ai_reading_needs_a_server_key(self):
        refused = self.client.put("/settings", json={"ai_extraction": True}, headers=self.h)
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(self.client.get("/settings", headers=self.h).json()["ai_available"], False)

    def test_sample_documents_download(self):
        import zipfile
        r = self.client.get("/samples/invoice-documents.zip")
        self.assertEqual(r.headers["content-type"], "application/zip")
        self.assertEqual(len(zipfile.ZipFile(io.BytesIO(r.content)).namelist()), 6)
        listed = self.client.get("/samples/documents").json()
        self.assertEqual(len(listed), 6)
        self.assertTrue(all(d["description"] for d in listed))
        one = self.client.get("/samples/documents/" + listed[1]["name"])
        self.assertTrue(one.content.startswith(b"%PDF"))
        self.assertEqual(self.client.get("/samples/documents/../../etc/passwd").status_code, 404)

if __name__ == "__main__":
    unittest.main()
