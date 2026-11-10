"""Single sign-on with OpenID Connect (Microsoft Entra ID, Google, Okta, Auth0, ...).

The flow ("authorization code with PKCE"):
1. The browser goes to /auth/<provider>/start. We make a random state, nonce and PKCE
   verifier, keep them in an encrypted, HttpOnly cookie, and redirect to the provider.
2. The user signs in there (the provider handles passwords and its own two-factor).
3. The provider redirects back to /auth/<provider>/callback?code=...&state=...
4. We check the state matches the cookie (stops forged logins), swap the code for an ID
   token at the provider's token endpoint (sending the PKCE verifier and client secret),
   and verify the ID token: RSA signature against the provider's published keys, issuer,
   audience, expiry and nonce.
5. The token's email must belong to an existing user; then we issue our own access token.
"""
import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

LEEWAY = 120          # seconds of clock difference tolerated
STATE_TTL = 600       # the sign-in must complete within 10 minutes
JWKS_TTL = 3600
HASHES = {"RS256": hashes.SHA256, "RS384": hashes.SHA384, "RS512": hashes.SHA512}


class SSOError(Exception):
    """Sign-in failed. The message is safe to show to the user."""


@dataclass(frozen=True)
class Provider:
    name: str                    # used in URLs: /auth/<name>/start
    label: str                   # shown on the button
    issuer: str                  # discovery: <issuer>/.well-known/openid-configuration
    client_id: str
    client_secret: str = field(repr=False)
    scopes: str = "openid email profile"
    email_claims: tuple = ("email",)
    require_verified_email: bool = False


def providers_from_env(env=os.environ) -> dict[str, Provider]:
    """Providers configured by environment variables. Each needs a client ID and secret
    from registering this app with the provider (see docs/DEPLOYMENT.md)."""
    found = {}
    if env.get("GOOGLE_CLIENT_ID"):
        found["google"] = Provider("google", "Google", "https://accounts.google.com",
                                   env["GOOGLE_CLIENT_ID"], env.get("GOOGLE_CLIENT_SECRET", ""),
                                   require_verified_email=True)
    if env.get("MICROSOFT_CLIENT_ID"):
        tenant = env.get("MICROSOFT_TENANT_ID", "")
        if not tenant or tenant in ("common", "organizations", "consumers"):
            raise RuntimeError("Set MICROSOFT_TENANT_ID to your directory (tenant) ID, so only "
                               "accounts from your organisation can sign in.")
        found["microsoft"] = Provider("microsoft", "Microsoft", f"https://login.microsoftonline.com/{tenant}/v2.0",
                                      env["MICROSOFT_CLIENT_ID"], env.get("MICROSOFT_CLIENT_SECRET", ""),
                                      email_claims=("email", "preferred_username"))
    if env.get("OIDC_ISSUER"):
        found["sso"] = Provider("sso", env.get("OIDC_LABEL", "single sign-on"), env["OIDC_ISSUER"].rstrip("/"),
                                env.get("OIDC_CLIENT_ID", ""), env.get("OIDC_CLIENT_SECRET", ""),
                                email_claims=("email", "preferred_username"),
                                require_verified_email=env.get("OIDC_REQUIRE_VERIFIED_EMAIL", "0") == "1")
    return found


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ---------------------------------------------------------------- HTTP (injectable for tests)
def _http_get_json(url: str) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}),
                                timeout=10) as resp:
        return json.load(resp)


def _http_post_form(url: str, form: dict) -> dict:
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            return json.load(exc)
        except Exception:
            raise SSOError("The sign-in provider rejected the request.") from None


# ---------------------------------------------------------------- the state cookie
class StateSealer:
    """Encrypts the in-progress sign-in (state, nonce, PKCE verifier) into a cookie value,
    so nothing needs storing on the server and any server instance can finish the flow."""

    def __init__(self, secret_key: str):
        self._aes = AESGCM(HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                                info=b"invoice-detector/sso-state").derive(secret_key.encode()))

    def seal(self, data: dict) -> str:
        nonce = secrets.token_bytes(12)
        return _b64url(nonce + self._aes.encrypt(nonce, json.dumps(data).encode(), b"sso-state"))

    def open(self, value: str, now: float | None = None) -> dict:
        try:
            raw = _unb64url(value)
            data = json.loads(self._aes.decrypt(raw[:12], raw[12:], b"sso-state"))
        except (InvalidTag, ValueError, TypeError):
            raise SSOError("The sign-in attempt could not be verified. Please try again.") from None
        if data.get("exp", 0) < (time.time() if now is None else now):
            raise SSOError("The sign-in took too long. Please try again.")
        return data


def new_attempt(provider: str) -> dict:
    verifier = secrets.token_urlsafe(48)
    return {"provider": provider, "state": secrets.token_urlsafe(24), "nonce": secrets.token_urlsafe(24),
            "verifier": verifier, "challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
            "exp": time.time() + STATE_TTL}


# ---------------------------------------------------------------- the client
class OIDCClient:
    def __init__(self, provider: Provider, get_json=_http_get_json, post_form=_http_post_form,
                 allow_http: bool = False):
        self.provider = provider
        self._get_json, self._post_form = get_json, post_form
        self._allow_http = allow_http
        self._discovery: dict | None = None
        self._jwks: dict | None = None
        self._jwks_at = 0.0

    def _check_url(self, url: str) -> str:
        if not (url.startswith("https://") or (self._allow_http and url.startswith("http://"))):
            raise SSOError("The sign-in provider must use HTTPS.")
        return url

    def discovery(self) -> dict:
        if self._discovery is None:
            url = self._check_url(self.provider.issuer + "/.well-known/openid-configuration")
            try:
                doc = self._get_json(url)
            except SSOError:
                raise
            except Exception:
                raise SSOError("The sign-in provider can't be reached right now.") from None
            for key in ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri"):
                if not isinstance(doc.get(key), str):
                    raise SSOError("The sign-in provider's configuration is incomplete.")
            self._discovery = doc
        return self._discovery

    def authorization_url(self, redirect_uri: str, attempt: dict) -> str:
        endpoint = self._check_url(self.discovery()["authorization_endpoint"])
        query = urllib.parse.urlencode({
            "response_type": "code", "client_id": self.provider.client_id, "redirect_uri": redirect_uri,
            "scope": self.provider.scopes, "state": attempt["state"], "nonce": attempt["nonce"],
            "code_challenge": attempt["challenge"], "code_challenge_method": "S256",
            "prompt": "select_account"})
        return f"{endpoint}{'&' if '?' in endpoint else '?'}{query}"

    def exchange_code(self, code: str, redirect_uri: str, verifier: str) -> str:
        endpoint = self._check_url(self.discovery()["token_endpoint"])
        try:
            reply = self._post_form(endpoint, {
                "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                "client_id": self.provider.client_id, "client_secret": self.provider.client_secret,
                "code_verifier": verifier})
        except SSOError:
            raise
        except Exception:
            raise SSOError("The sign-in provider can't be reached right now.") from None
        token = reply.get("id_token") if isinstance(reply, dict) else None
        if not isinstance(token, str):
            raise SSOError("The sign-in provider didn't return an identity.")
        return token

    def _key(self, kid: str | None, now: float):
        def lookup():
            for jwk in (self._jwks or {}).get("keys", []):
                if jwk.get("kty") == "RSA" and (kid is None or jwk.get("kid") == kid):
                    return jwk
            return None
        if self._jwks is None or now - self._jwks_at > JWKS_TTL or lookup() is None:
            self._jwks = self._get_json(self._check_url(self.discovery()["jwks_uri"]))
            self._jwks_at = now
        jwk = lookup()
        if jwk is None:
            raise SSOError("The identity couldn't be verified (unknown signing key).")
        numbers = rsa.RSAPublicNumbers(int.from_bytes(_unb64url(jwk["e"]), "big"),
                                       int.from_bytes(_unb64url(jwk["n"]), "big"))
        return numbers.public_key()

    def verify_id_token(self, token: str, nonce: str, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        try:
            head_b64, body_b64, sig_b64 = token.split(".")
            header = json.loads(_unb64url(head_b64))
            claims = json.loads(_unb64url(body_b64))
            signature = _unb64url(sig_b64)
        except (ValueError, TypeError):
            raise SSOError("The identity couldn't be verified.") from None
        alg = header.get("alg")
        if alg not in HASHES:  # refuses "none" and shared-secret algorithms
            raise SSOError("The identity couldn't be verified (unsupported signature).")
        try:
            self._key(header.get("kid"), now).verify(signature, f"{head_b64}.{body_b64}".encode(),
                                                     padding.PKCS1v15(), HASHES[alg]())
        except InvalidSignature:
            raise SSOError("The identity couldn't be verified (bad signature).") from None
        aud = claims.get("aud")
        audiences = aud if isinstance(aud, list) else [aud]
        problems = [
            claims.get("iss") != self.discovery()["issuer"],
            self.provider.client_id not in audiences,
            len(audiences) > 1 and claims.get("azp") != self.provider.client_id,
            not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < now - LEEWAY,
            isinstance(claims.get("iat"), (int, float)) and claims["iat"] > now + LEEWAY,
            not isinstance(claims.get("nonce"), str) or not secrets.compare_digest(claims["nonce"], nonce),
        ]
        if any(problems):
            raise SSOError("The identity couldn't be verified.")
        return claims

    def email_from(self, claims: dict) -> str:
        if self.provider.require_verified_email and claims.get("email_verified") is not True:
            raise SSOError("Your email address isn't verified with the sign-in provider.")
        for name in self.provider.email_claims:
            value = claims.get(name)
            if isinstance(value, str) and "@" in value:
                return value.strip().lower()
        raise SSOError("The sign-in provider didn't share an email address.")
