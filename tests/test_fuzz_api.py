"""Every route, hostile input: nothing may answer with a server error.

A 500 means input reached code that didn't expect it, which is where real bugs hide. Each
route is called (as an admin, and with no session) with malformed and oversized bodies,
odd path values and odd query strings. The answers may be refusals (4xx); they may not be
crashes, and they must stay JSON.
"""
import importlib.util
import itertools
import unittest

from support import DbTestCase, forget_cookies, make_settings

HAVE_API_DEPS = all(importlib.util.find_spec(m) for m in ("fastapi", "httpx"))

BODIES = [None, b"", b"{", b"[]", b'"x"', b"null", b"1e309", b'{"a": ' * 2000 + b"1" + b"}" * 2000,
          b'{"id": "\\u0000", "vendor": "\\ud800"}', b'{"invoice_ids": ["' + b"A" * 100_000 + b'"]}',
          b'{"verdict": "dismissed", "action": {"$ne": 1}}', b'{"filename": "../../etc/passwd", "content_base64": "%%%"}',
          b'{"items": [' + b"{}," * 500 + b"{}]}", b'{"username": [], "password": {}}']
PATH_VALUES = ["x", "..%2F..%2Fetc%2Fpasswd", "%00", "A" * 3000, "%E2%80%AE", "INV-1/../../users", "'; DROP TABLE invoices;--"]
QUERIES = ["", "?limit=-1", "?limit=99999999999999999999", "?sort=id;DROP", "?include_dismissed=maybe"]


@unittest.skipUnless(HAVE_API_DEPS, "fastapi/httpx not installed")
class NoServerErrorsTests(DbTestCase):
    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from invoice_detector.api import create_app

        self.api = create_app(make_settings(), db=self.app)
        self.client = forget_cookies(TestClient(self.api, raise_server_exceptions=False))
        self.app.create_user(self.a, "alice", "alice-password-123", "admin")
        self.app.add_invoice(self.a, "INV-1", "Acme", 100.0, "2026-01-05", "ACCT", "alice")
        token = self.client.post("/login", json={"username": "alice", "password": "alice-password-123"}).json()
        self.headers = {"Authorization": f"Bearer {token['access_token']}"}

    def routes(self):
        for route in self.api.routes:
            methods = getattr(route, "methods", None) or set()
            for method in sorted(methods - {"HEAD", "OPTIONS"}):
                yield method, route.path

    def fill(self, path, value):
        out = path
        while "{" in out:
            start, end = out.index("{"), out.index("}")
            out = out[:start] + value + out[end + 1:]
        return out

    def test_no_route_answers_500(self):
        crashes = []
        for (method, path), auth in itertools.product(list(self.routes()), (True, False)):
            values = PATH_VALUES if "{" in path else ["-"]
            for value, query in itertools.product(values, QUERIES):
                url = self.fill(path, value) + query
                bodies = BODIES if method in ("POST", "PUT", "PATCH", "DELETE") else [None]
                for body in bodies:
                    headers = {**(self.headers if auth else {}), "content-type": "application/json"}
                    r = self.client.request(method, url, content=body, headers=headers)
                    if r.status_code >= 500:
                        crashes.append(f"{r.status_code} {method} {url[:80]} body={str(body)[:40]}")
                    elif r.status_code >= 400 and "json" not in r.headers.get("content-type", ""):
                        crashes.append(f"non-JSON {r.status_code} {method} {url[:80]}")
        kinds = sorted({c.split(" body=")[0] + " " + c.split(" body=")[-1][:12] for c in crashes})
        self.assertEqual(kinds[:60], [], f"{len(crashes)} bad answers, {len(kinds)} kinds")


    def test_rejected_input_is_not_echoed(self):
        r = self.client.post("/invoices", json={"id": "OK-1", "vendor": "<script>steal()</script>", "amount": "lots",
                                                "date": "2026-01-01"}, headers=self.headers)
        self.assertEqual(r.status_code, 422)
        self.assertNotIn("steal()", r.text)
        self.assertNotIn("lots", r.text)
        self.assertEqual(r.json()["detail"][0]["loc"][-1], "amount")
        r = self.client.post("/login", content=b"1e309", headers={"content-type": "application/json"})
        self.assertEqual(r.status_code, 422)

if __name__ == "__main__":
    unittest.main()
