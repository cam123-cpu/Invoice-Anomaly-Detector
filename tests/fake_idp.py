"""A fake OpenID Connect provider for tests: real RSA keys, real signed ID tokens."""
import base64
import json
import secrets
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class FakeIdP:
    def __init__(self, issuer: str = "https://idp.example", client_id: str = "client-123"):
        self.issuer, self.client_id = issuer, client_id
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "key-1"
        self.codes: dict[str, dict] = {}   # code -> claims to put in the ID token
        self.requests: list = []           # (url, form) of token requests
        self.jwks_fetches = 0

    # discovery + keys
    def get_json(self, url: str) -> dict:
        if url == self.issuer + "/.well-known/openid-configuration":
            return {"issuer": self.issuer, "authorization_endpoint": self.issuer + "/authorize",
                    "token_endpoint": self.issuer + "/token", "jwks_uri": self.issuer + "/jwks"}
        if url == self.issuer + "/jwks":
            self.jwks_fetches += 1
            nums = self.key.public_key().public_numbers()
            return {"keys": [{"kty": "RSA", "kid": self.kid, "alg": "RS256", "use": "sig",
                              "n": b64(nums.n.to_bytes((nums.n.bit_length() + 7) // 8, "big")),
                              "e": b64(nums.e.to_bytes(3, "big"))}]}
        raise AssertionError("unexpected URL " + url)

    def post_form(self, url: str, form: dict) -> dict:
        self.requests.append((url, form))
        claims = self.codes.pop(form.get("code"), None)
        if claims is None:
            return {"error": "invalid_grant"}
        return {"id_token": self.sign(claims), "token_type": "Bearer"}

    def sign(self, claims: dict, alg: str = "RS256", key=None, kid: str | None = None) -> str:
        header = b64(json.dumps({"alg": alg, "kid": kid or self.kid, "typ": "JWT"}).encode())
        body = b64(json.dumps(claims).encode())
        sig = (key or self.key).sign(f"{header}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{header}.{body}.{b64(sig)}"

    def claims(self, email: str, nonce: str, **extra) -> dict:
        now = int(time.time())
        return {"iss": self.issuer, "aud": self.client_id, "sub": secrets.token_hex(8), "email": email,
                "email_verified": True, "nonce": nonce, "iat": now, "exp": now + 600, **extra}

    def approve(self, email: str, nonce: str, **extra) -> str:
        """What happens when the user signs in at the provider: a one-time code."""
        code = secrets.token_urlsafe(16)
        self.codes[code] = self.claims(email, nonce, **extra)
        return code
