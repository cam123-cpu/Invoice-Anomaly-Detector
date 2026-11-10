import importlib.util
import unittest

HAVE_API_DEPS = all(importlib.util.find_spec(m) for m in ("fastapi", "httpx"))


@unittest.skipUnless(HAVE_API_DEPS, "fastapi/httpx not installed")
class ApiTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from invoice_detector.api import create_app
        from support import make_settings

        self.app = create_app(make_settings())
        self.client = TestClient(self.app)
        db = self.app.state.db
        db.create_user("admin1", "admin-password-123", "admin")
        db.create_user("audit1", "auditor-password-123", "auditor")

    def login(self, user, password):
        return self.client.post("/login", json={"username": user, "password": password})

    def auth(self, user, password):
        token = self.login(user, password).json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    def invoice(self, inv_id="INV-1", **kw):
        return {"id": inv_id, "vendor": "Acme", "amount": 100.0, "date": "2026-01-05",
                "bank_account": "ACCT1", **kw}

    def test_requires_login(self):
        self.assertEqual(self.client.get("/invoices").status_code, 401)
        self.assertEqual(self.client.get("/invoices", headers={"Authorization": "Bearer x"}).status_code, 401)

    def test_login_rate_limit(self):
        for _ in range(5):
            self.assertEqual(self.login("admin1", "wrong").status_code, 401)
        self.assertEqual(self.login("admin1", "admin-password-123").status_code, 429)

    def test_same_error_for_unknown_user_and_bad_password(self):
        a, b = self.login("nobody", "x"), self.login("admin1", "x")
        self.assertEqual((a.status_code, a.json()), (b.status_code, b.json()))

    def test_roles_enforced(self):
        admin, auditor = self.auth("admin1", "admin-password-123"), self.auth("audit1", "auditor-password-123")
        self.assertEqual(self.client.post("/invoices", json=self.invoice(), headers=admin).status_code, 201)
        self.assertEqual(self.client.post("/invoices", json=self.invoice("INV-2"), headers=auditor).status_code, 403)
        self.assertEqual(self.client.get("/invoices", headers=auditor).status_code, 200)
        self.assertEqual(self.client.get("/invoices/INV-1/bank", headers=auditor).status_code, 403)
        self.assertEqual(self.client.get("/invoices/INV-1/bank", headers=admin).json()["bank_account"], "ACCT1")

    def test_validation_and_duplicates(self):
        admin = self.auth("admin1", "admin-password-123")
        self.assertEqual(self.client.post("/invoices", json=self.invoice(amount=-1), headers=admin).status_code, 422)
        self.assertEqual(self.client.post("/invoices", json=self.invoice("bad id!"), headers=admin).status_code, 422)
        self.client.post("/invoices", json=self.invoice(), headers=admin)
        self.assertEqual(self.client.post("/invoices", json=self.invoice(), headers=admin).status_code, 409)
        self.assertEqual(self.client.get("/invoices?sort=amount;DROP", headers=admin).status_code, 422)

    def test_detect_flow(self):
        admin = self.auth("admin1", "admin-password-123")
        self.client.post("/invoices", json=self.invoice("INV-1"), headers=admin)
        self.client.post("/invoices", json=self.invoice("INV-2"), headers=admin)  # exact duplicate
        self.assertEqual(self.client.post("/detect", headers=admin).json()["invoices_checked"], 2)
        rules = {a["rule"] for a in self.client.get("/anomalies", headers=admin).json()}
        self.assertIn("duplicate", rules)
        self.assertIn("INV-2", self.client.post("/explain", headers=admin).json()["explanation"])
        queue = self.client.get("/review-queue", headers=admin).json()
        self.assertEqual(queue[0]["invoice_id"], "INV-2")
        self.assertIn("duplicate", queue[0]["rules"])
        self.assertEqual(self.client.get("/review-queue").status_code, 401)


if __name__ == "__main__":
    unittest.main()
