import base64
import importlib.util
import unittest

from support import DbTestCase, make_settings

HAVE_API_DEPS = all(importlib.util.find_spec(m) for m in ("fastapi", "httpx"))
PW_A, PW_B = "alice-password-123", "bob-password-12345"


@unittest.skipUnless(HAVE_API_DEPS, "fastapi/httpx not installed")
class ApiTests(DbTestCase):
    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from invoice_detector.api import create_app

        self.client = TestClient(create_app(make_settings(), db=self.app))
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
        self.assertEqual(self.client.get("/me", headers=self.auth("carol", "carol-password-123")).json(),
                         {"username": "carol", "role": "accountant", "company": "Company A"})
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
        self.assertEqual(self.client.get("/settings", headers=admin).json(), {"approval_threshold": 500})
        self.client.post("/detect", headers=admin)
        rules = [a["rule"] for a in self.client.get("/anomalies", headers=admin).json()]
        self.assertEqual(rules, ["below_threshold"])
        bob = self.auth("bob", PW_B)
        self.assertEqual(self.client.get("/settings", headers=bob).json(), {"approval_threshold": 10000})

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


if __name__ == "__main__":
    unittest.main()
