import importlib.util
import random
import string
import unittest
from urllib.parse import parse_qs, urlparse

from invoice_detector import totp
from invoice_detector.qr import matrix, svg_data_uri

RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # base32 of "12345678901234567890"


class TotpTests(unittest.TestCase):
    def test_rfc6238_vectors(self):
        # RFC 6238 appendix B (SHA1), last six digits of the 8-digit values
        for t, expected in ((59, "287082"), (1111111109, "081804"), (1111111111, "050471"),
                            (1234567890, "005924"), (2000000000, "279037"), (20000000000, "353130")):
            self.assertEqual(totp.code_at(RFC_SECRET, t // 30), expected, t)

    def test_verify_window_and_replay_protection(self):
        now = 1_000_000_000
        step = totp.current_step(now)
        code = totp.code_at(RFC_SECRET, step)
        self.assertEqual(totp.verify(RFC_SECRET, code, now=now), step)
        self.assertEqual(totp.verify(RFC_SECRET, f"{code[:3]} {code[3:]}", now=now), step)  # spaces OK
        self.assertEqual(totp.verify(RFC_SECRET, totp.code_at(RFC_SECRET, step - 1), now=now), step - 1)
        self.assertIsNone(totp.verify(RFC_SECRET, totp.code_at(RFC_SECRET, step - 2), now=now))
        self.assertIsNone(totp.verify(RFC_SECRET, code, last_step=step, now=now))  # already used
        self.assertIsNone(totp.verify(RFC_SECRET, "12345", now=now))
        self.assertIsNone(totp.verify(RFC_SECRET, "abcdef", now=now))

    def test_new_secret_and_uri(self):
        secret = totp.new_secret()
        self.assertEqual(len(secret), 32)
        self.assertNotEqual(secret, totp.new_secret())
        uri = urlparse(totp.provisioning_uri(secret, "alice@acme.example"))
        self.assertEqual((uri.scheme, uri.netloc), ("otpauth", "totp"))
        self.assertEqual(uri.path, "/Invoice%20Review%3Aalice%40acme.example")
        query = parse_qs(uri.query)
        self.assertEqual((query["secret"][0], query["digits"][0], query["period"][0]), (secret, "6", "30"))

    def test_recovery_codes_are_single_use(self):
        pepper = b"p" * 32
        codes = totp.new_recovery_codes()
        self.assertEqual(len(set(codes)), 10)
        self.assertRegex(codes[0], r"^[a-z2-9]{5}-[a-z2-9]{5}-[a-z2-9]{5}$")
        hashes = [totp.hash_recovery_code(c, pepper) for c in codes]
        self.assertNotIn(codes[0], "".join(hashes))
        remaining = totp.use_recovery_code(codes[3].upper().replace("-", " "), hashes, pepper)
        self.assertEqual(len(remaining), 9)
        self.assertIsNone(totp.use_recovery_code(codes[3], remaining, pepper))
        self.assertIsNone(totp.use_recovery_code("not-a-real-code", hashes, pepper))
        self.assertIsNone(totp.use_recovery_code(codes[3], hashes, b"other pepper" * 3))


class QrTests(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(len(matrix("hi")), 21)          # version 1
        self.assertEqual(len(matrix("x" * 200)), 57)     # version 10
        with self.assertRaises(ValueError):
            matrix("x" * 300)
        self.assertTrue(svg_data_uri("hi").startswith("data:image/svg+xml;base64,"))

    @unittest.skipUnless(importlib.util.find_spec("cv2"), "needs OpenCV to decode")
    def test_codes_scan(self):
        import cv2
        import numpy as np

        def render(text, scale=8, border=4):
            grid = matrix(text)
            img = np.full(((len(grid) + 2 * border) * scale,) * 2, 255, np.uint8)
            for y, row in enumerate(grid):
                for x, dark in enumerate(row):
                    if dark:
                        img[(y + border) * scale:(y + border + 1) * scale,
                            (x + border) * scale:(x + border + 1) * scale] = 0
            return img

        rng = random.Random(1)
        texts = [totp.provisioning_uri(totp.new_secret(), "alice@acme.example")]
        texts += ["".join(rng.choice(string.ascii_letters + ":/?=&") for _ in range(n))
                  for n in (1, 14, 26, 42, 62, 84, 106, 122, 152, 180, 213)]
        # OpenCV's default detector occasionally misses a valid code, so a code passes if
        # either of OpenCV's two independent decoders reads it at one of three sizes.
        plain = cv2.QRCodeDetector()
        aruco = cv2.QRCodeDetectorAruco() if hasattr(cv2, "QRCodeDetectorAruco") else None

        def reads(text):
            for scale in (8, 5, 11):
                img = render(text, scale)
                if plain.detectAndDecode(img)[0] == text:
                    return True
                if aruco is not None:
                    ok, values, *_ = aruco.detectAndDecodeMulti(img)
                    if ok and text in values:
                        return True
            return False

        for text in texts:
            self.assertTrue(reads(text), f"QR code for {len(text)} characters did not scan")


if __name__ == "__main__":
    unittest.main()
