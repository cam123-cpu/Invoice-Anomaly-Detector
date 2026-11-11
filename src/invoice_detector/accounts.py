"""Everything about signing in, in one place, independent of the web framework.

The API routes in api.py are thin wrappers around this class, so these rules are tested
directly (tests/test_accounts.py) and can't drift between endpoints.

Sign-in methods
- Password, then (if the user turned it on) a code from an authenticator app or a
  one-time recovery code.
- Single sign-on with Microsoft, Google or any OpenID Connect provider. The provider
  handles the password and its own two-factor; we check its signed answer.

Sessions
- Every access token carries the company, whether a second factor was used ("mfa"), and a
  fingerprint of the password. Each request re-checks the user against the database, so
  changing a password or deleting a user ends their sessions at once.
- A company can require two-factor: until a user sets it up, they can only reach the
  account pages needed to do so.
"""
import hashlib
import hmac
import threading

from . import totp
from .db import SSO_ONLY, Database
from .mailer import Mailer
from .oidc import OIDCClient, SSOError, StateSealer, new_attempt
from .qr import svg_data_uri
from .security import (DUMMY_HASH, AuthError, RateLimiter, RateLimitError, TokenSigner,
                       password_problem, session_fingerprint, verify_password)

MFA_PENDING_TTL = 300     # 5 minutes to type the authenticator code
RESET_TTL = 1800          # reset links work for 30 minutes
ISSUER = "Invoice Review"


class AccountError(Exception):
    """A sign-in problem with an HTTP status and a message that is safe to show."""

    def __init__(self, status: int, message: str, requires_2fa: bool = False):
        super().__init__(message)
        self.status, self.message, self.requires_2fa = status, message, requires_2fa


class Accounts:
    def __init__(self, db: Database, signer: TokenSigner, settings, mailer: Mailer | None = None,
                 sso_clients: dict[str, OIDCClient] | None = None):
        self.db, self.signer, self.settings = db, signer, settings
        self.mailer = mailer or Mailer()
        self.sso = sso_clients if sso_clients is not None else {
            name: OIDCClient(p, allow_http=settings.oidc_allow_http) for name, p in settings.sso_providers.items()}
        self.sealer = StateSealer(settings.secret_key)
        self._mail_threads: list[threading.Thread] = []
        self.login_limiter = RateLimiter()                 # 5 wrong passwords per address + username
        self.address_limiter = RateLimiter(max_attempts=30)  # 30 per address, any usernames (bots)
        self.code_limiter = RateLimiter()
        self.reset_limiter = RateLimiter()                 # reset emails per address
        self.reset_account_limiter = RateLimiter(max_attempts=3, window=3600, lockout=3600)  # per account
        self._pepper = hmac.new(settings.secret_key.encode(), b"recovery-codes", hashlib.sha256).digest()

    # ------------------------------------------------------------ helpers
    def _token(self, user: dict, org: str, mfa: bool) -> str:
        return self.signer.make(user["username"], user["role"], org, mfa=mfa,
                                pwf=session_fingerprint(user))

    def _secret(self, org: str, username: str, stored: str) -> str:
        return self.db.vault(org).decrypt(stored, f"users.totp:{org}:{username}")

    @staticmethod
    def _check_password(password: str, username: str) -> None:
        problem = password_problem(password, username)
        if problem:
            raise AccountError(422, problem)

    def _check_rate(self, limiter: RateLimiter, key: str) -> None:
        try:
            limiter.check(key)
        except RateLimitError:
            raise AccountError(429, "Too many attempts. Try again in 15 minutes.") from None

    def _check_code(self, org: str, sec: dict, code: str, allow_recovery: bool = True) -> str | None:
        """'app' or 'recovery' if the code is right (and records its use), else None."""
        if sec["totp_enabled"] and sec["totp_secret"]:
            step = totp.verify(self._secret(org, sec["username"], sec["totp_secret"]), code, sec["totp_last_step"])
            # claim_totp_step is one conditional UPDATE: of two requests carrying the same code
            # at the same moment, exactly one wins.
            if step is not None and self.db.claim_totp_step(org, sec["username"], step):
                return "app"
        if allow_recovery and sec["recovery_codes"]:
            remaining = totp.use_recovery_code(code, sec["recovery_codes"], self._pepper)
            if remaining is not None and self.db.swap_recovery_codes(org, sec["username"], sec["recovery_codes"],
                                                                     remaining):
                return "recovery"
        return None

    # ------------------------------------------------------------ password sign-in
    def login(self, username: str, password: str, ip: str) -> dict:
        key = f"{ip}:{username.lower()}"
        self._check_rate(self.address_limiter, ip)
        self._check_rate(self.login_limiter, key)
        user = self.db.auth_lookup(username)
        usable = user is not None and user["password_hash"] != SSO_ONLY and not user["disabled"]
        ok = verify_password(password, user["password_hash"] if usable else DUMMY_HASH)
        if not (usable and ok):
            self.login_limiter.record_failure(key)
            self.address_limiter.record_failure(ip)
            if user:
                self.db.audit(user["org_id"], user["username"], "login_failed", f"from {ip}")
            raise AccountError(401, "Wrong username or password.")
        self.login_limiter.record_success(key)
        if user["totp_enabled"]:
            pending = self.signer.issue({"sub": user["username"], "org": user["org_id"],
                                         "pwf": session_fingerprint(user)},
                                        "mfa", MFA_PENDING_TTL)
            return {"mfa_required": True, "mfa_token": pending}
        self.db.audit(user["org_id"], user["username"], "login", f"password, from {ip}")
        return {"access_token": self._token(user, user["org_id"], mfa=False), "token_type": "bearer"}

    def login_second_factor(self, mfa_token: str, code: str, ip: str) -> dict:
        try:
            pending = self.signer.verify(mfa_token, "mfa")
        except AuthError:
            raise AccountError(401, "Your sign-in timed out. Please start again.") from None
        org, username = pending["org"], pending["sub"]
        key = f"2fa:{username.lower()}"
        self._check_rate(self.code_limiter, key)
        sec = self.db.user_security(org, username)
        if (sec is None or sec["disabled"] or not sec["totp_enabled"]
                or session_fingerprint(sec) != pending.get("pwf")):
            raise AccountError(401, "Your sign-in timed out. Please start again.")
        method = self._check_code(org, sec, code)
        if method is None:
            self.code_limiter.record_failure(key)
            self.db.audit(org, username, "login_failed", f"wrong two-factor code, from {ip}")
            raise AccountError(401, "That code isn't right. Codes change every 30 seconds.")
        self.code_limiter.record_success(key)
        left = len(self.db.user_security(org, username)["recovery_codes"])
        detail = "authenticator app" if method == "app" else f"recovery code ({left} left)"
        self.db.audit(org, username, "login", f"password + {detail}, from {ip}")
        return {"access_token": self._token(sec, org, mfa=True), "token_type": "bearer",
                "recovery_codes_left": left}

    # ------------------------------------------------------------ every request
    def authenticate(self, token: str, enforce_2fa: bool = True) -> dict:
        """The signed-in user for an access token, re-checked against the database."""
        try:
            data = self.signer.read(token)
        except AuthError:
            raise AccountError(401, "Not signed in, or the sign-in expired.") from None
        user = self.db.auth_lookup(data["sub"])
        if (user is None or user["disabled"] or user["org_id"] != data["org"] or user["username"] != data["sub"]
                or session_fingerprint(user) != data["pwf"]):
            raise AccountError(401, "Your session has ended. Please sign in again.")
        org = self.db.get_org(data["org"])
        current = {"sub": user["username"], "role": user["role"], "org": data["org"], "mfa": data["mfa"],
                   "two_factor": user["totp_enabled"], "require_2fa": org["require_2fa"],
                   "sso_only": user["password_hash"] == SSO_ONLY}
        if enforce_2fa and org["require_2fa"] and not data["mfa"]:
            raise AccountError(403, "Your company requires two-factor authentication. Set it up to continue.",
                               requires_2fa=True)
        return current

    # ------------------------------------------------------------ two-factor management
    def two_factor_status(self, user: dict) -> dict:
        sec = self.db.user_security(user["org"], user["sub"])
        return {"enabled": sec["totp_enabled"], "recovery_codes_left": len(sec["recovery_codes"]),
                "required_by_company": user["require_2fa"]}

    def setup_2fa(self, user: dict) -> dict:
        sec = self.db.user_security(user["org"], user["sub"])
        if sec["totp_enabled"]:
            raise AccountError(409, "Two-factor authentication is already on.")
        secret = totp.new_secret()
        enc = self.db.vault(user["org"]).encrypt(secret, f"users.totp:{user['org']}:{user['sub']}")
        self.db.update_user_security(user["org"], user["sub"], totp_secret=enc, totp_enabled=False,
                                     totp_last_step=None)
        uri = totp.provisioning_uri(secret, user["sub"], ISSUER)
        return {"secret": " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)), "uri": uri,
                "qr": svg_data_uri(uri)}

    def enable_2fa(self, user: dict, code: str) -> dict:
        org = user["org"]
        sec = self.db.user_security(org, user["sub"])
        if sec["totp_enabled"]:
            raise AccountError(409, "Two-factor authentication is already on.")
        if not sec["totp_secret"]:
            raise AccountError(400, "Start the set-up first.")
        step = totp.verify(self._secret(org, user["sub"], sec["totp_secret"]), code)
        if step is None:
            raise AccountError(400, "That code isn't right. Check the time on your phone is set automatically.")
        codes = totp.new_recovery_codes()
        self.db.update_user_security(org, user["sub"], totp_enabled=True, totp_last_step=step,
                                     recovery_codes=[totp.hash_recovery_code(c, self._pepper) for c in codes])
        self.db.audit(org, user["sub"], "two_factor_enabled", "")
        sec = self.db.user_security(org, user["sub"])
        return {"recovery_codes": codes, "access_token": self._token(sec, org, mfa=True)}

    def _confirm_identity(self, user: dict, password: str | None, code: str | None) -> dict:
        sec = self.db.user_security(user["org"], user["sub"])
        key = f"confirm:{user['sub'].lower()}"
        self._check_rate(self.code_limiter, key)
        if sec["password_hash"] != SSO_ONLY and not verify_password(password or "", sec["password_hash"]):
            self.code_limiter.record_failure(key)
            raise AccountError(401, "Your password isn't right.")
        if self._check_code(user["org"], sec, code or "") is None:
            self.code_limiter.record_failure(key)
            raise AccountError(401, "That code isn't right.")
        self.code_limiter.record_success(key)
        return sec

    def disable_2fa(self, user: dict, password: str | None, code: str) -> dict:
        if user["require_2fa"]:
            raise AccountError(409, "Your company requires two-factor authentication, so it can't be turned off.")
        sec = self._confirm_identity(user, password, code)
        self.db.update_user_security(user["org"], user["sub"], totp_secret=None, totp_enabled=False,
                                     totp_last_step=None, recovery_codes=None)
        self.db.audit(user["org"], user["sub"], "two_factor_disabled", "by the user")
        return {"access_token": self._token(sec, user["org"], mfa=False)}

    def new_recovery_codes(self, user: dict, code: str) -> dict:
        sec = self.db.user_security(user["org"], user["sub"])
        key = f"confirm:{user['sub'].lower()}"   # same budget as other "prove it's you" checks
        self._check_rate(self.code_limiter, key)
        if not sec["totp_enabled"] or self._check_code(user["org"], sec, code, allow_recovery=False) is None:
            self.code_limiter.record_failure(key)
            raise AccountError(401, "That code isn't right.")
        self.code_limiter.record_success(key)
        codes = totp.new_recovery_codes()
        self.db.update_user_security(user["org"], user["sub"],
                                     recovery_codes=[totp.hash_recovery_code(c, self._pepper) for c in codes])
        self.db.audit(user["org"], user["sub"], "recovery_codes_replaced", "")
        return {"recovery_codes": codes}

    # ------------------------------------------------------------ passwords
    def change_password(self, user: dict, current: str, new: str) -> dict:
        sec = self.db.user_security(user["org"], user["sub"])
        if sec["password_hash"] == SSO_ONLY:
            raise AccountError(400, "You sign in with single sign-on, so there's no password here to change.")
        key = f"password:{user['sub'].lower()}"
        self._check_rate(self.code_limiter, key)
        if not verify_password(current, sec["password_hash"]):
            self.code_limiter.record_failure(key)
            raise AccountError(401, "Your current password isn't right.")
        self.code_limiter.record_success(key)
        self._check_password(new, user["sub"])
        self.db.set_password(user["sub"], new, user["org"])
        self.db.audit(user["org"], user["sub"], "password_changed", "by the user; other sessions ended")
        return {"access_token": self._token(self.db.user_security(user["org"], user["sub"]), user["org"],
                                            mfa=user["mfa"])}

    def forgot_password(self, username: str, ip: str) -> dict:
        """Email a reset link. The answer is the same whether or not the account exists."""
        if not self.mailer.configured:
            return {"email_enabled": False}
        self._check_rate(self.reset_limiter, ip)
        self.reset_limiter.record_failure(ip)  # count every request from this address
        user = self.db.auth_lookup(username.strip())
        account = f"reset:{(user or {}).get('username', '')}"
        try:
            self.reset_account_limiter.check(account)
        except RateLimitError:
            return {"email_enabled": True}  # same answer; just no more emails to this person
        if user and "@" in user["username"] and user["password_hash"] != SSO_ONLY and not user["disabled"]:
            self.reset_account_limiter.record_failure(account)  # at most 3 emails an hour each
            token = self.signer.issue({"sub": user["username"], "org": user["org_id"],
                                       "pwf": session_fingerprint(user)}, "reset", RESET_TTL)
            link = f"{self.settings.public_url}/#/reset?token={token}"
            self.db.audit(user["org_id"], user["username"], "password_reset_requested", f"from {ip}")
            # Sent on a background thread: talking to a mail server takes a noticeable moment,
            # and that delay would reveal which usernames have accounts.
            self._send_later(user["username"], "Reset your Invoice Review password",
                             f"Someone asked to reset the password for {user['username']}.\n\n"
                             f"To choose a new password, open this link within 30 minutes:\n{link}\n\n"
                             "If you didn't ask for this, ignore this email; your password stays the same.\n")
        return {"email_enabled": True}

    def _send_later(self, to: str, subject: str, body: str) -> None:
        def send():
            try:
                self.mailer.send(to, subject, body)
            except Exception:   # a mail failure must never surface to the requester
                pass
        thread = threading.Thread(target=send, daemon=True)
        self._mail_threads.append(thread)
        thread.start()

    def wait_for_mail(self, timeout: float = 10) -> None:
        """Block until queued emails are sent (used by tests and at shutdown)."""
        while self._mail_threads:
            self._mail_threads.pop().join(timeout)

    def reset_password(self, token: str, new: str) -> dict:
        invalid = AccountError(400, "This reset link is invalid, has expired or was already used. Ask for a new one.")
        try:
            data = self.signer.verify(token, "reset")
        except AuthError:
            raise invalid from None
        user = self.db.auth_lookup(data["sub"])
        if (user is None or user["disabled"] or user["org_id"] != data["org"]
                or session_fingerprint(user) != data["pwf"]):
            raise invalid
        self._check_password(new, user["username"])
        self.db.set_password(user["username"], new, user["org_id"])
        self.db.audit(user["org_id"], user["username"], "password_reset", "with an emailed link; sessions ended")
        return {"ok": True}

    # ------------------------------------------------------------ admin actions
    def admin_set_password(self, admin: dict, username: str, new: str) -> None:
        self._check_password(new, username)
        if not self.db.set_password(username, new, admin["org"]):
            raise AccountError(404, "No such person in your company.")
        self.db.audit(admin["org"], admin["sub"], "password_reset", f"for {username}, by an admin")

    def admin_reset_2fa(self, admin: dict, username: str) -> None:
        if self.db.user_security(admin["org"], username) is None:
            raise AccountError(404, "No such person in your company.")
        self.db.update_user_security(admin["org"], username, totp_secret=None, totp_enabled=False,
                                     totp_last_step=None, recovery_codes=None)
        self.db.audit(admin["org"], admin["sub"], "two_factor_disabled", f"for {username}, by an admin")

    # ------------------------------------------------------------ single sign-on
    def sso_providers(self) -> list[dict]:
        return [{"name": c.provider.name, "label": c.provider.label} for c in self.sso.values()]

    def _redirect_uri(self, name: str) -> str:
        return f"{self.settings.public_url}/auth/{name}/callback"

    def sso_start(self, name: str) -> tuple[str, str]:
        """(URL to send the browser to, value for the encrypted state cookie)."""
        client = self.sso.get(name)
        if client is None:
            raise AccountError(404, "That sign-in option isn't set up.")
        attempt = new_attempt(name)
        try:
            url = client.authorization_url(self._redirect_uri(name), attempt)
        except SSOError as exc:
            raise AccountError(502, str(exc)) from None
        return url, self.sealer.seal(attempt)

    def sso_callback(self, name: str, query: dict, cookie: str | None, ip: str) -> tuple[str, str | None]:
        """(Where to send the browser next, new access token or None). The API puts the token
        in the session cookie, so it never appears in an address."""
        try:
            client = self.sso.get(name)
            if client is None:
                raise SSOError("That sign-in option isn't set up.")
            if query.get("error"):
                raise SSOError("Sign-in was cancelled or refused by the provider.")
            if not cookie:
                raise SSOError("The sign-in attempt could not be verified. Please try again.")
            attempt = self.sealer.open(cookie)
            if attempt.get("provider") != name or not hmac.compare_digest(str(query.get("state", "")), attempt["state"]):
                raise SSOError("The sign-in attempt could not be verified. Please try again.")
            id_token = client.exchange_code(str(query.get("code", "")), self._redirect_uri(name), attempt["verifier"])
            email = client.email_from(client.verify_id_token(id_token, attempt["nonce"]))
            user = self.db.auth_lookup(email)
            if user is None or user["disabled"]:
                raise SSOError(f"There's no account for {email}. Ask your company's admin to add you.")
            self.db.audit(user["org_id"], user["username"], "login", f"single sign-on ({client.provider.label}), from {ip}")
            return "/#/dashboard", self._token(user, user["org_id"], mfa=True)
        except SSOError as exc:
            # The message travels as a signed, short-lived token. The sign-in page shows only
            # messages this server signed, so nobody can craft a link that makes the real
            # site display their own text.
            return "/#/login?notice=" + self.signer.issue({"m": str(exc)}, "notice", 120), None

    def notice(self, token: str) -> str:
        try:
            return str(self.signer.verify(token, "notice")["m"])
        except (AuthError, KeyError):
            raise AccountError(400, "That message has expired.") from None

    # ------------------------------------------------------------ ending access
    def logout(self, user: dict) -> None:
        """Sign out everywhere: the token that was just used, and any other, stops working."""
        self.db.end_sessions(user["org"], user["sub"])
        self.db.audit(user["org"], user["sub"], "logout", "all sessions ended")

    def admin_set_access(self, admin: dict, username: str, enabled: bool) -> None:
        """Remove (or give back) a person's access. Their record and history stay."""
        target = self.db.user_security(admin["org"], username)
        if target is None:
            raise AccountError(404, "No such person in your company.")
        if not enabled:
            if username == admin["sub"]:
                raise AccountError(409, "You can't remove your own access. Ask another admin.")
            if target["role"] == "admin" and not target["disabled"] and self.db.active_admins(admin["org"]) <= 1:
                raise AccountError(409, "This is the only admin. Make someone else an admin first.")
        self.db.set_user_disabled(admin["org"], username, not enabled)
        self.db.audit(admin["org"], admin["sub"], "access_restored" if enabled else "access_removed", f"for {username}")
