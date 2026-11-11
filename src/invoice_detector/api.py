"""FastAPI service for many companies: login (rate limited), role-based access,
detection, reviewer feedback and an audit trail. Every request is scoped to the
company inside the caller's signed token.

Run:  uvicorn invoice_detector.api:create_app --factory
"""
import base64
import binascii
import re
from dataclasses import replace
from urllib.parse import quote
from datetime import date as Date
from importlib.resources import files
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from .config import Settings
from .db import Conflict, Database
from .detection import (DEFAULT_CONFIG, RULE_WEIGHT, Anomaly, detect, learned_weights,
                        review_queue)
from .decisions import fix_steps, payment_decision, suggested_action
from .excel_out import XLSX, annotate, payment_file, review_report
from .explain import explain
from .importer import FIELDS, ID_PATTERN, FileProblem, convert, preview, read_table
from .samples import SAMPLES, build as build_sample
from .doc_samples import sample_docs, sample_zip
from .intake import DocumentIntake, IntakeError
from .accounts import AccountError, Accounts
from .mailer import from_settings as mailer_from_settings
from .security import TokenSigner

MAX_BULK = 1000
MAX_BODY = 16 * 1024 * 1024   # an upload is at most 10 MB, about 13.4 MB once base64-encoded
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
SESSION_TTL = 3600
WEB = files("invoice_detector") / "web"
WEB_FILES = {"index.html": "text/html", "app.js": "text/javascript", "app.css": "text/css"}
# The web app loads nothing from other sites and never runs inline script, so a strict
# Content-Security-Policy blocks injected script even if some text slipped through.
APP_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
           "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
           "form-action 'self'; frame-ancestors 'none'")
# API answers are data, never pages: nothing in them may load or run anything.
API_CSP = "default-src 'none'; frame-ancestors 'none'"
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


class StrictModel(BaseModel):
    """Request bodies. Unknown fields are refused rather than silently ignored, so a client
    can't slip in extra ones (say "role" or "org_id") hoping some code path reads them."""
    model_config = ConfigDict(extra="forbid")


class BodyLimit:
    """Refuse oversized request bodies before anything reads them. Browsers always send
    Content-Length for the bodies this app uses; a body without one is refused (411)."""

    def __init__(self, app, max_bytes: int = MAX_BODY):
        self.app, self.max = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
            headers = dict(scope["headers"])
            length = headers.get(b"content-length")
            problem = None
            if length is None and headers.get(b"transfer-encoding"):
                problem = (411, "Send a Content-Length header.")
            elif length is not None and (not length.isdigit() or int(length) > self.max):
                problem = (413, f"The request is larger than {self.max // (1024 * 1024)} MB.")
            if problem:
                body = ('{"detail": "%s"}' % problem[1]).encode()
                await send({"type": "http.response.start", "status": problem[0],
                            "headers": [(b"content-type", b"application/json"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


class LoginIn(StrictModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class InvoiceIn(StrictModel):
    id: str = Field(pattern=ID_PATTERN.pattern)
    vendor: str = Field(min_length=1, max_length=120)
    amount: float = Field(gt=0, lt=1e12)
    date: Date
    bank_account: str | None = Field(default=None, max_length=64)


class UserIn(StrictModel):
    username: str = Field(pattern=r"^[A-Za-z0-9_.@+-]{3,64}$")
    # Leave out for someone who will only use single sign-on (Microsoft, Google, ...).
    password: str | None = Field(default=None, min_length=12, max_length=256)
    role: Literal["admin", "accountant", "auditor"]


class SecondFactorIn(StrictModel):
    mfa_token: str = Field(min_length=1, max_length=2000)
    code: str = Field(min_length=6, max_length=40)


class CodeIn(StrictModel):
    code: str = Field(min_length=6, max_length=40)


class DisableTwoFactorIn(StrictModel):
    password: str | None = Field(default=None, max_length=256)
    code: str = Field(min_length=6, max_length=40)


class ChangePasswordIn(StrictModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class ForgotIn(StrictModel):
    username: str = Field(min_length=1, max_length=64)


class ResetIn(StrictModel):
    token: str = Field(min_length=1, max_length=2000)
    new_password: str = Field(min_length=12, max_length=256)


class NewPasswordIn(StrictModel):
    new_password: str = Field(min_length=12, max_length=256)


class ReviewIn(StrictModel):
    verdict: Literal["confirmed", "dismissed"]
    # What happens to the payment. Leave out to use the suggestion (e.g. "do_not_pay" for a duplicate).
    action: Literal["pay", "hold", "do_not_pay"] | None = None
    note: str | None = Field(default=None, max_length=500)


class FileIn(StrictModel):
    filename: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(min_length=1, max_length=14_000_000)
    sheet: str | None = Field(default=None, max_length=100)


class MappedFileIn(FileIn):
    mapping: dict[str, str | None]
    date_order: Literal["MDY", "DMY"] = "MDY"


class ImportIn(MappedFileIn):
    run_detection: bool = True


def attachment(data: bytes, filename: str, media_type: str) -> Response:
    """A file download. The plain filename is restricted to safe characters; the
    original name goes in the RFC 5987 field for browsers that support it."""
    plain = re.sub(r"[^A-Za-z0-9 ._-]", "_", filename)[:150] or "download.xlsx"
    disposition = f"attachment; filename=\"{plain}\"; filename*=UTF-8''{quote(filename[:150])}"
    return Response(data, media_type=media_type, headers={"Content-Disposition": disposition,
                                                          "Cache-Control": "no-store"})


class SettingsIn(StrictModel):
    approval_threshold: float | None = Field(default=None, gt=0, lt=1e12)
    require_2fa: bool | None = None
    ai_extraction: bool | None = None


class DocumentIn(StrictModel):
    filename: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(min_length=1, max_length=14_000_000)


class CheckedDocument(StrictModel):
    """One draft after a person checked it. The fields are what they confirmed."""
    draft: str = Field(min_length=1, max_length=8000)
    file: str | None = Field(default=None, max_length=255)
    id: str | None = Field(default=None, max_length=64)
    vendor: str | None = Field(default=None, max_length=200)
    date: str | None = Field(default=None, max_length=10)
    amount: float | None = Field(default=None, ge=0, lt=1e12)
    bank_account: str | None = Field(default=None, max_length=64)


class DocumentsImportIn(StrictModel):
    items: list[CheckedDocument] = Field(min_length=1, max_length=200)
    run_detection: bool = True


def create_app(settings: Settings | None = None, db: Database | None = None,
               accounts: Accounts | None = None, intake: DocumentIntake | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db = db or Database.from_settings(settings)
    db.check_keys()  # refuse to start with encryption keys that don't match the data
    signer = TokenSigner(settings.secret_key)
    accounts = accounts or Accounts(db, signer, settings, mailer_from_settings(settings))
    intake = intake or DocumentIntake(db, signer, settings, isolate=settings.isolate_documents)
    bearer = HTTPBearer(auto_error=False)
    https = settings.public_url.startswith("https://")
    sso_cookie = "iad_sso"
    # The browser's session lives in an HttpOnly cookie, out of reach of page scripts. The
    # __Host- prefix makes browsers insist it is Secure, for this exact host, for every path.
    session_cookie = "__Host-iad_session" if https else "iad_session"

    docs = settings.api_docs
    app = FastAPI(title="Invoice Anomaly Detector", version="0.9.0",
                  docs_url="/docs" if docs else None, redoc_url=None,
                  openapi_url="/openapi.json" if docs else None)
    app.add_middleware(BodyLimit)
    app.state.db, app.state.signer, app.state.accounts = db, signer, accounts

    def http_error(exc: AccountError) -> HTTPException:
        headers = {"X-Requires-2FA": "1"} if exc.requires_2fa else None
        return HTTPException(exc.status, exc.message, headers=headers)

    def client_ip(request: Request) -> str:
        # Behind a proxy, run uvicorn with --proxy-headers so this is the real address.
        return request.client.host if request.client else "unknown"

    def _user(request: Request, creds: HTTPAuthorizationCredentials | None, enforce_2fa: bool) -> dict:
        # Programs send "Authorization: Bearer ..."; the web app sends the session cookie.
        token = creds.credentials if creds else request.cookies.get(session_cookie)
        if not token:
            raise HTTPException(401, "Not authenticated")
        try:
            return accounts.authenticate(token, enforce_2fa=enforce_2fa)
        except AccountError as exc:
            raise http_error(exc) from None

    def current_user(request: Request, creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> dict:
        """The signed-in user. If their company requires two-factor and this session didn't
        use it, only the account pages (below) are reachable."""
        return _user(request, creds, enforce_2fa=True)

    def account_user(request: Request, creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> dict:
        """For the pages a user needs to set up two-factor or change their password."""
        return _user(request, creds, enforce_2fa=False)

    def start_session(response: Response, result: dict) -> dict:
        """When a sign-in step produced an access token, also put it in the session cookie.
        (The token stays in the JSON too, for programs that use the API directly.)"""
        if isinstance(result, dict) and result.get("access_token"):
            response.set_cookie(session_cookie, result["access_token"], max_age=SESSION_TTL, path="/",
                                httponly=True, secure=https, samesite="strict")
        return result

    def require(*roles: str):
        def dependency(user: dict = Depends(current_user)) -> dict:
            if user["role"] not in roles:
                raise HTTPException(403, "Insufficient role")
            return user
        return dependency

    def weights_for(org: str) -> dict[str, float]:
        return learned_weights(db.feedback_counts(org))

    def run_detection_for(user: dict) -> dict:
        org = db.get_org(user["org"])
        cfg = replace(DEFAULT_CONFIG, approval_threshold=org["approval_threshold"])
        invoices = db.detection_inputs(user["org"])
        found = detect(invoices, cfg)
        db.save_flags(user["org"], found)
        flagged = len({a.invoice_id for a in found})
        db.audit(user["org"], user["sub"], "detection_run",
                 f"{len(invoices)} invoices, {flagged} flagged")
        return {"invoices_checked": len(invoices), "invoices_flagged": flagged,
                "anomalies": len(found)}

    def read_upload(body: FileIn):
        try:
            data = base64.b64decode(body.content_base64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(422, "The file could not be read. Try uploading it again.") from None
        try:
            return read_table(body.filename, data, body.sheet)
        except FileProblem as exc:
            raise HTTPException(422, str(exc)) from None

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        if settings.force_https and request.url.scheme == "http" and not request.url.path.startswith("/health"):
            # Behind a proxy that ends TLS, uvicorn --proxy-headers sets the scheme from
            # X-Forwarded-Proto, so only real plain-HTTP requests are redirected.
            return RedirectResponse(str(request.url.replace(scheme="https")), status_code=308)
        if request.method not in SAFE_METHODS:
            # Cross-site request forgery: another site can make a visitor's browser send a
            # form here, cookie included. The session cookie is SameSite=Strict, which stops
            # that in current browsers; as a second lock, a request that relies on the cookie
            # must carry a header other sites can't add, and come from this site.
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") not in (str(request.base_url).rstrip("/"), settings.public_url):
                return JSONResponse({"detail": "Request from another site refused."}, status_code=403)
            uses_cookie = session_cookie in request.cookies and "authorization" not in request.headers
            if uses_cookie and request.headers.get("x-requested-with") != "fetch":
                return JSONResponse({"detail": "Missing X-Requested-With header."}, status_code=403)
        response = await call_next(request)
        if settings.force_https:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        path = request.url.path
        if path == "/" or path.startswith("/app/"):
            response.headers["Content-Security-Policy"] = APP_CSP
            response.headers["Cache-Control"] = "no-cache"
        elif path != "/docs":
            response.headers.setdefault("Content-Security-Policy", API_CSP)
            # Answers hold company data: never keep them in browser or proxy caches.
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    # ---------------- web app ----------------
    @app.get("/", include_in_schema=False)
    def web_index() -> Response:
        return Response((WEB / "index.html").read_bytes(), media_type="text/html")

    @app.get("/app/{name}", include_in_schema=False)
    def web_asset(name: str) -> Response:
        if name not in WEB_FILES or name == "index.html":
            raise HTTPException(404, "Not found")
        return Response((WEB / name).read_bytes(), media_type=WEB_FILES[name])

    @app.get("/app", include_in_schema=False)
    def web_redirect() -> RedirectResponse:
        return RedirectResponse("/")

    # ---------------- sample files (synthetic, so no sign-in needed) ----------------
    SAMPLE_DOC_INFO = {
        "wayne-office-1042.pdf": "A normal invoice from a regular vendor.",
        "stark-freight-8811.pdf": "Asks to be paid into a bank account this vendor hasn't used before.",
        "umbrella-ltd-3307.xlsx": "An Excel invoice whose total doesn't match its own lines and tax.",
        "acme-suplies-0091.pdf": "From \"Acme Suplies\", a look-alike of your vendor Acme Supplies.",
    }

    @app.get("/samples/documents")
    def list_sample_documents() -> list[dict]:
        """Made-up invoice documents (PDF and Excel) for trying document reading."""
        return [{"name": n, "description": d} for n, d in SAMPLE_DOC_INFO.items()]

    @app.get("/samples/documents/{name}")
    def get_sample_document(name: str) -> Response:
        files = {n: data for n, data, _ in sample_docs()}
        if name not in files:
            raise HTTPException(404, "No such sample")
        media = "application/pdf" if name.endswith(".pdf") else XLSX
        return attachment(files[name], name, media)

    @app.get("/samples")
    def list_samples() -> list[dict]:
        """Sample Excel files in the shapes real exports take, plus a blank template."""
        return [{"name": n, "title": t, "description": d} for n, (t, d, _) in SAMPLES.items()]

    @app.get("/samples/{name}")
    def get_sample(name: str) -> Response:
        if name == "invoice-documents.zip":
            return attachment(sample_zip(), name, "application/zip")
        if name not in SAMPLES:
            raise HTTPException(404, "No such sample")
        return attachment(build_sample(name), name, XLSX)

    # ---------------- session ----------------
    @app.get("/health")
    def health() -> dict:
        """Liveness: the process is up (for load balancers)."""
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready() -> dict:
        """Readiness: the database answers."""
        try:
            db.ping()
        except Exception:
            raise HTTPException(503, "Database unavailable") from None
        return {"status": "ready"}

    # ---------------- signing in ----------------
    def run(action):
        try:
            return action()
        except AccountError as exc:
            raise http_error(exc) from None

    @app.post("/login")
    def login(body: LoginIn, request: Request, response: Response) -> dict:
        """Step 1. Returns an access token, or {"mfa_required": true, "mfa_token": ...} when
        the user has two-factor on; then send the code to POST /login/2fa."""
        return start_session(response, run(lambda: accounts.login(body.username, body.password, client_ip(request))))

    @app.post("/login/2fa")
    def login_2fa(body: SecondFactorIn, request: Request, response: Response) -> dict:
        """Step 2: a code from the authenticator app, or a one-time recovery code."""
        return start_session(response, run(lambda: accounts.login_second_factor(body.mfa_token, body.code,
                                                                                client_ip(request))))

    @app.post("/logout")
    def logout(response: Response) -> dict:
        """End the browser session (removes the session cookie)."""
        response.delete_cookie(session_cookie, path="/", secure=https, httponly=True, samesite="strict")
        return {"signed_out": True}

    @app.get("/auth/providers")
    def sso_providers() -> list[dict]:
        """Single sign-on options configured on this server (for the sign-in page)."""
        return accounts.sso_providers()

    @app.get("/auth/{name}/start", include_in_schema=False)
    def sso_start(name: str) -> RedirectResponse:
        url, sealed = run(lambda: accounts.sso_start(name))
        response = RedirectResponse(url, status_code=302)
        response.set_cookie(sso_cookie, sealed, max_age=600, httponly=True, samesite="lax", path="/auth/",
                            secure=settings.public_url.startswith("https://"))
        return response

    @app.get("/auth/{name}/callback", include_in_schema=False)
    def sso_callback(name: str, request: Request) -> RedirectResponse:
        target, token = accounts.sso_callback(name, dict(request.query_params), request.cookies.get(sso_cookie),
                                              client_ip(request))
        response = RedirectResponse(target, status_code=302)
        response.delete_cookie(sso_cookie, path="/auth/")
        start_session(response, {"access_token": token})
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/password/forgot")
    def forgot_password(body: ForgotIn, request: Request) -> dict:
        """Email a reset link. The reply is the same whether or not the account exists."""
        return run(lambda: accounts.forgot_password(body.username, client_ip(request)))

    @app.post("/password/reset")
    def reset_password(body: ResetIn) -> dict:
        return run(lambda: accounts.reset_password(body.token, body.new_password))

    # ---------------- your account ----------------
    @app.get("/me")
    def me(user: dict = Depends(account_user)) -> dict:
        org = db.get_org(user["org"])
        return {"username": user["sub"], "role": user["role"], "company": org["name"],
                "two_factor_enabled": user["two_factor"], "two_factor_required": user["require_2fa"],
                "signed_in_with_two_factor": user["mfa"], "sso_only": user["sso_only"],
                "email_enabled": settings.email_enabled}

    @app.get("/me/2fa")
    def two_factor_status(user: dict = Depends(account_user)) -> dict:
        return accounts.two_factor_status(user)

    @app.post("/me/2fa/setup")
    def two_factor_setup(user: dict = Depends(account_user)) -> dict:
        """A new secret, as a QR code and as text, for the authenticator app."""
        return run(lambda: accounts.setup_2fa(user))

    @app.post("/me/2fa/enable")
    def two_factor_enable(body: CodeIn, response: Response, user: dict = Depends(account_user)) -> dict:
        """Confirm with a code from the app. Returns ten recovery codes (shown once) and a new
        access token for a session that counts as two-factor."""
        return start_session(response, run(lambda: accounts.enable_2fa(user, body.code)))

    @app.post("/me/2fa/disable")
    def two_factor_disable(body: DisableTwoFactorIn, response: Response, user: dict = Depends(account_user)) -> dict:
        return start_session(response, run(lambda: accounts.disable_2fa(user, body.password, body.code)))

    @app.post("/me/2fa/recovery-codes")
    def two_factor_new_codes(body: CodeIn, user: dict = Depends(current_user)) -> dict:
        return run(lambda: accounts.new_recovery_codes(user, body.code))

    @app.post("/me/password")
    def change_password(body: ChangePasswordIn, response: Response, user: dict = Depends(account_user)) -> dict:
        """Ends every other session; returns a new access token for this one."""
        return start_session(response, run(lambda: accounts.change_password(user, body.current_password,
                                                                            body.new_password)))

    # ---------------- company administration ----------------
    @app.get("/users")
    def list_users(user: dict = Depends(require("admin"))) -> list[dict]:
        return db.list_users(user["org"])

    @app.post("/users", status_code=201)
    def add_user(body: UserIn, user: dict = Depends(require("admin"))) -> dict:
        """Admins add people to their own company only. Use their email address as the
        username: it's what single sign-on and password-reset emails match on."""
        username = body.username.strip().lower() if "@" in body.username else body.username.strip()
        if db.auth_lookup(username):
            raise HTTPException(409, "Username is already taken")
        try:
            db.create_user(user["org"], username, body.password, body.role)
        except Conflict:
            raise HTTPException(409, "Username is already taken") from None
        method = "password" if body.password else "single sign-on only"
        db.audit(user["org"], user["sub"], "user_created", f"{username} as {body.role} ({method})")
        return {"username": username, "role": body.role, "sso_only": body.password is None}

    @app.post("/users/{username}/password")
    def admin_set_password(username: str, body: NewPasswordIn, user: dict = Depends(require("admin"))) -> dict:
        """Give someone a temporary password (ends their sessions)."""
        run(lambda: accounts.admin_set_password(user, username, body.new_password))
        return {"username": username}

    @app.post("/users/{username}/2fa/reset")
    def admin_reset_2fa(username: str, user: dict = Depends(require("admin"))) -> dict:
        """Turn off someone's two-factor, e.g. after they lost their phone and recovery codes."""
        run(lambda: accounts.admin_reset_2fa(user, username))
        return {"username": username}

    @app.get("/settings")
    def get_settings(user: dict = Depends(current_user)) -> dict:
        return settings_view(db.get_org(user["org"]))

    def settings_view(org: dict) -> dict:
        return {"approval_threshold": org["approval_threshold"], "require_2fa": org["require_2fa"],
                "ai_extraction": org["ai_extraction"], "ai_available": bool(settings.anthropic_api_key)}

    @app.put("/settings")
    def put_settings(body: SettingsIn, user: dict = Depends(require("admin"))) -> dict:
        if body.require_2fa and not user["two_factor"]:
            raise HTTPException(409, "Turn on two-factor for your own account first, so you aren't locked out.")
        if body.ai_extraction and not settings.anthropic_api_key:
            raise HTTPException(409, "AI reading needs an Anthropic API key on the server (ANTHROPIC_API_KEY).")
        db.set_org_settings(user["org"], body.approval_threshold, body.require_2fa, body.ai_extraction)
        changes = {k: v for k, v in body.model_dump().items() if v is not None}
        db.audit(user["org"], user["sub"], "settings_changed", ", ".join(f"{k}={v}" for k, v in changes.items()))
        return settings_view(db.get_org(user["org"]))

    @app.get("/audit")
    def audit_log(limit: int = 100, user: dict = Depends(require("admin", "auditor"))) -> list[dict]:
        return db.list_audit(user["org"], limit)

    # ---------------- invoices ----------------
    @app.post("/invoices", status_code=201)
    def add_invoice(inv: InvoiceIn, user: dict = Depends(require("admin", "accountant"))) -> dict:
        try:
            db.add_invoice(user["org"], inv.id, inv.vendor, inv.amount, inv.date.isoformat(),
                           inv.bank_account, user["sub"])
        except Conflict:
            raise HTTPException(409, "Invoice id already exists") from None
        return {"id": inv.id}

    def queue_items(org: str, ids=None) -> dict[str, dict]:
        """Review-queue items (with invoice details) keyed by invoice id."""
        items = [i for i in full_queue(org) if ids is None or i["invoice_id"] in ids]
        details = db.get_invoices(org, [i["invoice_id"] for i in items])
        for item in items:
            inv = details.get(item["invoice_id"], {})
            item.update(vendor=inv.get("vendor"), amount=inv.get("amount"), date=inv.get("invoice_date"))
        return {i["invoice_id"]: i for i in items}

    @app.post("/export/annotated")
    def export_annotated(body: MappedFileIn, user: dict = Depends(current_user)) -> Response:
        """Your own workbook back, with each row's risk, review status and reasons added in new
        columns, risky rows highlighted, and a ranked 'Flagged invoices' sheet. Invoices are
        matched by invoice number; import the file first so they have been checked."""
        table = read_upload(body)
        try:
            result = convert(table, {f: body.mapping.get(f) for f in FIELDS}, body.date_order)
        except FileProblem as exc:
            raise HTTPException(422, str(exc)) from None
        org = user["org"]
        stored = db.get_invoices(org, list(result.lines))
        lines = {i: line for i, line in result.lines.items() if i in stored}
        queue = queue_items(org, set(lines))
        try:
            data, name, media = annotate(body.filename, base64.b64decode(body.content_base64), table, lines,
                                         result.errors, queue, db.get_org(org)["name"])
        except Exception:
            raise HTTPException(422, "This workbook could not be rewritten. Try saving it as .xlsx "
                                     "or .csv and uploading it again.") from None
        db.audit(org, user["sub"], "workbook_exported",
                 f"{body.filename[:80]}: {len(queue)} flagged of {len(lines)} matched")
        return attachment(data, name, media)

    @app.get("/export/review.xlsx")
    def export_review(include_dismissed: bool = True, user: dict = Depends(current_user)) -> Response:
        """The review queue as a formatted Excel workbook."""
        org = user["org"]
        items = sorted(queue_items(org).values(), key=lambda i: (-i["risk"], i["invoice_id"]))
        if not include_dismissed:
            items = [i for i in items if i["status"] != "dismissed"]
        counts = {s: sum(i["status"] == s for i in items) for s in ("open", "confirmed", "dismissed")}
        company = db.get_org(org)["name"]
        data = review_report(items, company, {"invoices": db.count_invoices(org), "flagged": len(items), **counts})
        db.audit(org, user["sub"], "report_exported", f"review queue, {len(items)} rows")
        slug = re.sub(r"[^A-Za-z0-9]+", "-", company).strip("-").lower() or "company"
        return attachment(data, f"review-queue-{slug}.xlsx", XLSX)

    @app.get("/export/payment-file.xlsx")
    def export_payment_file(user: dict = Depends(current_user)) -> Response:
        """Every invoice sorted by what should happen to its payment: Ready to pay, Review first,
        On hold, Do not pay, with totals. The corrected batch to hand to whoever pays."""
        org = user["org"]
        company = db.get_org(org)["name"]
        data = payment_file(db.all_invoices(org), queue_items(org), company)
        db.audit(org, user["sub"], "report_exported", "payment file")
        slug = re.sub(r"[^A-Za-z0-9]+", "-", company).strip("-").lower() or "company"
        return attachment(data, f"payment-file-{slug}.xlsx", XLSX)

    @app.post("/invoices/bulk")
    def add_invoices(invoices: list[InvoiceIn],
                     user: dict = Depends(require("admin", "accountant"))) -> dict:
        """Load up to 1,000 invoices per call (for ERP exports). Ids already stored, or repeated
        within the request, are skipped and listed."""
        if len(invoices) > MAX_BULK:
            raise HTTPException(413, f"At most {MAX_BULK} invoices per request")
        rows = [{"id": i.id, "vendor": i.vendor, "amount": i.amount, "date": i.date.isoformat(),
                 "bank_account": i.bank_account} for i in invoices]
        skipped = db.add_invoices(user["org"], rows, user["sub"])
        added = len(rows) - len(skipped)
        db.audit(user["org"], user["sub"], "invoices_imported", f"{added} added, {len(skipped)} skipped")
        return {"added": added, "skipped": skipped}

    @app.get("/invoices")
    def list_invoices(sort: str = "invoice_date", limit: int = 50,
                      user: dict = Depends(current_user)) -> list[dict]:
        try:
            return db.list_invoices(user["org"], sort, limit)
        except ValueError:
            raise HTTPException(422, "Invalid sort column") from None

    @app.post("/import/preview")
    def import_preview(body: FileIn, user: dict = Depends(require("admin", "accountant"))) -> dict:
        """Read an uploaded Excel or CSV file and suggest which column is which."""
        result = preview(read_upload(body))
        # A single invoice form (not a list) has no invoice-number or amount column and few
        # rows: point the person to document reading instead of a confusing column match.
        missing = [f for f in ("id", "amount", "vendor") if not result["mapping"].get(f)]
        result["looks_like_document"] = bool(missing) and result["total_rows"] <= 80
        return result

    # ---------------- invoice documents (one invoice per file) ----------------
    def intake_call(action):
        try:
            return action()
        except IntakeError as exc:
            raise HTTPException(exc.status, exc.message) from None

    @app.post("/documents/extract")
    def documents_extract(body: DocumentIn, user: dict = Depends(require("admin", "accountant"))) -> dict:
        """Read one invoice document (PDF, Excel invoice form, or a scan/photo) and return a
        draft for a person to check. Nothing is saved."""
        try:
            data = base64.b64decode(body.content_base64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(422, "The file could not be read. Try uploading it again.") from None
        return intake_call(lambda: intake.extract(user, body.filename, data))

    @app.post("/documents/import")
    def documents_import(body: DocumentsImportIn, user: dict = Depends(require("admin", "accountant"))) -> dict:
        """Save checked drafts as invoices (with a record of each document), then run detection."""
        result = intake_call(lambda: intake.import_drafts(user, [i.model_dump() for i in body.items]))
        result["detection"] = run_detection_for(user) if body.run_detection and result["added"] else None
        return result

    @app.get("/documents/stats")
    def documents_stats(user: dict = Depends(current_user)) -> dict:
        return db.document_stats(user["org"])

    @app.post("/import")
    def import_file(body: ImportIn, user: dict = Depends(require("admin", "accountant"))) -> dict:
        """Import an uploaded file using the confirmed column mapping, then (by default)
        run detection so the review queue is up to date."""
        table = read_upload(body)
        mapping = {f: body.mapping.get(f) for f in FIELDS}
        try:
            result = convert(table, mapping, body.date_order)
        except FileProblem as exc:
            raise HTTPException(422, str(exc)) from None
        existing = db.add_invoices(user["org"], result.invoices, user["sub"])
        added = len(result.invoices) - len(existing)
        db.audit(user["org"], user["sub"], "file_imported",
                 f"{body.filename[:80]}: {added} added, {len(existing)} already stored, "
                 f"{len(result.errors)} rows with problems")
        detection = None
        if body.run_detection and added:
            detection = run_detection_for(user)
            new_ids = {inv["id"] for inv in result.invoices} - set(existing)
            detection["flagged_from_file"] = len({a["invoice_id"] for a in db.list_anomalies(user["org"], 5000)
                                                  if a["invoice_id"] in new_ids})
        return {"added": added, "already_stored": len(existing), "blank_rows": result.blank_rows,
                "error_count": len(result.errors), "errors": result.errors[:200],
                "detection": detection}

    @app.get("/invoices/{inv_id:path}/bank")
    def bank_account(inv_id: str, user: dict = Depends(require("admin"))) -> dict:
        try:
            account = db.get_bank_account(user["org"], inv_id)
        except KeyError:
            raise HTTPException(404, "Invoice not found") from None
        db.audit(user["org"], user["sub"], "bank_account_viewed", inv_id)
        return {"id": inv_id, "bank_account": account}

    # ---------------- detection and review ----------------
    @app.post("/detect")
    def run_detection(user: dict = Depends(require("admin", "accountant"))) -> dict:
        return run_detection_for(user)

    @app.get("/anomalies")
    def anomalies(user: dict = Depends(current_user)) -> list[dict]:
        return db.list_anomalies(user["org"])

    @app.get("/review-queue")
    def queue(limit: int = 50, include_dismissed: bool = False,
              user: dict = Depends(current_user)) -> list[dict]:
        """Flagged invoices ranked by risk (0-100), with reasons and review status.
        Risk uses this company's learned rule weights."""
        ranked = full_queue(user["org"])
        if not include_dismissed:
            ranked = [i for i in ranked if i["status"] != "dismissed"]
        ranked = ranked[:max(1, min(limit, 500))]
        details = db.get_invoices(user["org"], [i["invoice_id"] for i in ranked])
        for item in ranked:
            inv = details.get(item["invoice_id"], {})
            item.update(vendor=inv.get("vendor"), amount=inv.get("amount"), date=inv.get("invoice_date"))
        return ranked

    def full_queue(org: str) -> list[dict]:
        """Ranked flags with review status, payment decision and next steps."""
        ranked = review_queue([Anomaly(**a) for a in db.list_anomalies(org, 5000)], weights_for(org))
        reviews = db.review_details(org)
        for item in ranked:
            review = reviews.get(item["invoice_id"], {})
            item["status"] = review.get("verdict", "open")
            item["action"] = review.get("action")
            item["note"] = review.get("note")
            item["reviewer"] = review.get("reviewer")
            item["suggested_action"] = suggested_action(item["rules"])
            item["decision"] = payment_decision(item)
            item["fixes"] = fix_steps(item["rules"])
        return ranked

    @app.get("/summary")
    def summary(user: dict = Depends(current_user)) -> dict:
        """Numbers for the dashboard."""
        org = user["org"]
        queue = full_queue(org)
        by_status = {s: sum(i["status"] == s for i in queue) for s in ("open", "confirmed", "dismissed")}
        open_rules: dict[str, int] = {}
        for item in queue:
            if item["status"] == "open":
                for rule in set(item["rules"]):
                    open_rules[rule] = open_rules.get(rule, 0) + 1
        decisions = {i["invoice_id"]: i["decision"] for i in queue}
        money = {d: 0.0 for d in ("pay", "review", "hold", "do_not_pay")}
        counts = dict.fromkeys(money, 0)
        for inv in db.all_invoices(org):
            decision = decisions.get(inv["id"], "pay")
            money[decision] += inv["amount"]
            counts[decision] += 1
        return {"company": db.get_org(org)["name"], "invoices": db.count_invoices(org),
                "flagged": len(queue), **by_status,
                "decisions": counts, "amounts": {k: round(v, 2) for k, v in money.items()},
                "high_risk_open": sum(i["status"] == "open" and i["risk"] >= 70 for i in queue),
                "open_by_rule": dict(sorted(open_rules.items(), key=lambda kv: -kv[1])),
                "last_detection": db.last_audit(org, "detection_run"),
                "last_import": db.last_audit(org, "file_imported")}

    @app.post("/review/{invoice_id:path}")
    def review(invoice_id: str, body: ReviewIn,
               user: dict = Depends(require("admin", "accountant"))) -> dict:
        """Mark a flagged invoice as a real problem (confirmed) or a false alarm (dismissed).
        These decisions tune the rule weights for your company."""
        try:
            action = db.set_review(user["org"], invoice_id, body.verdict, user["sub"], body.action, body.note)
        except KeyError:
            raise HTTPException(404, "No flags on that invoice") from None
        db.audit(user["org"], user["sub"], "invoice_reviewed", f"{invoice_id}: {body.verdict}, {action}")
        return {"invoice_id": invoice_id, "status": body.verdict, "action": action, "decision": action}

    @app.get("/metrics")
    def metrics(user: dict = Depends(current_user)) -> dict:
        """Measured precision per rule from this company's reviews, and the weights in use."""
        counts = db.feedback_counts(user["org"])
        weights = weights_for(user["org"])
        rules = {}
        for rule, base in RULE_WEIGHT.items():
            confirmed, dismissed = counts.get(rule, (0, 0))
            total = confirmed + dismissed
            rules[rule] = {"confirmed": confirmed, "dismissed": dismissed,
                           "precision": round(confirmed / total, 3) if total else None,
                           "base_weight": base, "weight": weights[rule]}
        reviews = db.list_reviews(user["org"])
        confirmed = sum(v == "confirmed" for v in reviews.values())
        return {"reviewed": len(reviews), "confirmed": confirmed,
                "precision": round(confirmed / len(reviews), 3) if reviews else None,
                "rules": rules}

    @app.post("/explain")
    def explain_anomalies(user: dict = Depends(current_user)) -> dict:
        return {"explanation": explain(db.list_anomalies(user["org"], 20),
                                       settings.anthropic_api_key)}

    return app
