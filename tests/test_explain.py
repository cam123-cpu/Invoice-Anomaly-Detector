import unittest

from invoice_detector.explain import build_prompt, explain

SAMPLE = [{"invoice_id": "INV-1", "rule": "duplicate", "severity": "high",
           "detail": "duplicate of INV-0"}]


class ExplainTests(unittest.TestCase):
    def test_no_key_uses_template(self):
        self.assertIn("INV-1", explain(SAMPLE, api_key=None))

    def test_empty(self):
        self.assertEqual(explain([], api_key="k"), "No anomalies to explain.")

    def test_uses_llm_response_and_sends_key_only_in_header(self):
        seen = {}

        def fake_post(url, headers, body):
            seen.update(url=url, headers=headers, body=body)
            return {"content": [{"type": "text", "text": "Looks like a double entry."}]}

        self.assertEqual(explain(SAMPLE, "sk-test", post=fake_post), "Looks like a double entry.")
        self.assertEqual(seen["headers"]["x-api-key"], "sk-test")
        self.assertNotIn("sk-test", str(seen["body"]))

    def test_llm_failure_falls_back(self):
        def broken(url, headers, body):
            raise OSError("network down")

        self.assertIn("INV-1", explain(SAMPLE, "sk-test", post=broken))

    def test_prompt_contains_no_bank_or_vendor_data(self):
        prompt = build_prompt(SAMPLE)
        self.assertNotIn("ACCT", prompt)
        self.assertIn("human review", prompt)


if __name__ == "__main__":
    unittest.main()
