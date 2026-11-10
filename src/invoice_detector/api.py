"""FastAPI service for many companies: login (rate limited), role-based access,
detection, reviewer feedback and an audit trail. Every request is scoped to the
company inside the caller's signed token.

Run:  uvicorn invoice_detector.api:create_app --factory
"""
from dataclasses import replace
from datetime import date as Date
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from .config import Settings
from .db import Conflict, Database
from .detection import (DEFAULT_CONFIG, RULE_WEIGHT, Anomaly, detect, learned_weights,
                        review_queue)
from .explain import explain
from .security import (DUMMY_HASH, AuthError, RateLimiter, RateLimitError, TokenSigner,
                       verify_password)

MAX_BULK = 1000


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class InvoiceIn(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    vendor: str = Field(min_length=1, max_length=120)
    amount: float = Field(gt=0, lt=1e12)
    date: Date
    bank_account: str | None = Field(default=None, max_length=64)


class UserIn(BaseModel):
    username: str = Field(pattern=r"^[A-Za-z0-9_.@-]{3,64}$")
    password: str = Field(min_length=12, max_length=256)
    role: Literal["admin", "accountant", "auditor"]


class ReviewIn(BaseModel):
    verdict: Literal["confirmed", "dismissed"]


class SettingsIn(BaseModel):
    approval_threshold: float = Field(gt=0, lt=1e12)


def create_app(settings: Settings | None = None, db: Database | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db = db or Database.from_settings(settings)
    signer = TokenSigner(settings.secret_key)
    limiter = RateLimiter()
    bearer = HTTPBearer(auto_error=False)

    app = FastAPI(title="Invoice Anomaly Detector", version="0.3.0")
    app.state.db, app.state.signer = db, signer

    def current_user(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> dict:
        if creds is None:
            raise HTTPException(401, "Not authenticated")
        try:
            return signer.read(creds.credentials)
        except AuthError:
            raise HTTPException(401, "Invalid or expired token") from None

    def require(*roles: str):
        def dependency(user: dict = Depends(current_user)) -> dict:
            if user["role"] not in roles:
                raise HTTPException(403, "Insufficient role")
            return user
        return dependency

    def weights_for(org: str) -> dict[str, float]:
        return learned_weights(db.feedback_counts(org))

    # ---------------- session ----------------
    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/login")
    def login(body: LoginIn, request: Request) -> dict:
        ip = request.client.host if request.client else "unknown"
        key = f"{ip}:{body.username.lower()}"
        try:
            limiter.check(key)
        except RateLimitError:
            raise HTTPException(429, "Too many attempts. Try again later.") from None
        user = db.auth_lookup(body.username)
        ok = verify_password(body.password, user["password_hash"] if user else DUMMY_HASH)
        if not (user and ok):
            limiter.record_failure(key)
            if user:
                db.audit(user["org_id"], user["username"], "login_failed", f"from {ip}")
            raise HTTPException(401, "Invalid credentials")  # same message either way
        limiter.record_success(key)
        db.audit(user["org_id"], user["username"], "login", f"from {ip}")
        return {"access_token": signer.make(user["username"], user["role"], user["org_id"]),
                "token_type": "bearer"}

    @app.get("/me")
    def me(user: dict = Depends(current_user)) -> dict:
        org = db.get_org(user["org"])
        return {"username": user["sub"], "role": user["role"], "company": org["name"]}

    # ---------------- company administration ----------------
    @app.get("/users")
    def list_users(user: dict = Depends(require("admin"))) -> list[dict]:
        return db.list_users(user["org"])

    @app.post("/users", status_code=201)
    def add_user(body: UserIn, user: dict = Depends(require("admin"))) -> dict:
        """Admins add users to their own company only."""
        try:
            db.create_user(user["org"], body.username, body.password, body.role)
        except Conflict:
            raise HTTPException(409, "Username is already taken") from None
        db.audit(user["org"], user["sub"], "user_created", f"{body.username} as {body.role}")
        return {"username": body.username, "role": body.role}

    @app.get("/settings")
    def get_settings(user: dict = Depends(current_user)) -> dict:
        return {"approval_threshold": db.get_org(user["org"])["approval_threshold"]}

    @app.put("/settings")
    def put_settings(body: SettingsIn, user: dict = Depends(require("admin"))) -> dict:
        db.set_approval_threshold(user["org"], body.approval_threshold)
        db.audit(user["org"], user["sub"], "settings_changed",
                 f"approval_threshold={body.approval_threshold}")
        return {"approval_threshold": body.approval_threshold}

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

    @app.post("/invoices/bulk")
    def add_invoices(invoices: list[InvoiceIn],
                     user: dict = Depends(require("admin", "accountant"))) -> dict:
        """Load up to 1,000 invoices per call (for ERP exports). Existing ids are skipped."""
        if len(invoices) > MAX_BULK:
            raise HTTPException(413, f"At most {MAX_BULK} invoices per request")
        added, skipped = 0, []
        for inv in invoices:
            try:
                db.add_invoice(user["org"], inv.id, inv.vendor, inv.amount, inv.date.isoformat(),
                               inv.bank_account, user["sub"])
                added += 1
            except Conflict:
                skipped.append(inv.id)
        db.audit(user["org"], user["sub"], "invoices_imported", f"{added} added, {len(skipped)} skipped")
        return {"added": added, "skipped": skipped}

    @app.get("/invoices")
    def list_invoices(sort: str = "invoice_date", limit: int = 50,
                      user: dict = Depends(current_user)) -> list[dict]:
        try:
            return db.list_invoices(user["org"], sort, limit)
        except ValueError:
            raise HTTPException(422, "Invalid sort column") from None

    @app.get("/invoices/{inv_id}/bank")
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

    @app.get("/anomalies")
    def anomalies(user: dict = Depends(current_user)) -> list[dict]:
        return db.list_anomalies(user["org"])

    @app.get("/review-queue")
    def queue(limit: int = 50, include_dismissed: bool = False,
              user: dict = Depends(current_user)) -> list[dict]:
        """Flagged invoices ranked by risk (0-100), with reasons and review status.
        Risk uses this company's learned rule weights."""
        org = user["org"]
        ranked = review_queue([Anomaly(**a) for a in db.list_anomalies(org, 5000)], weights_for(org))
        reviews = db.list_reviews(org)
        for item in ranked:
            item["status"] = reviews.get(item["invoice_id"], "open")
        if not include_dismissed:
            ranked = [i for i in ranked if i["status"] != "dismissed"]
        return ranked[:max(1, min(limit, 500))]

    @app.post("/review/{invoice_id}")
    def review(invoice_id: str, body: ReviewIn,
               user: dict = Depends(require("admin", "accountant"))) -> dict:
        """Mark a flagged invoice as a real problem (confirmed) or a false alarm (dismissed).
        These decisions tune the rule weights for your company."""
        try:
            db.set_review(user["org"], invoice_id, body.verdict, user["sub"])
        except KeyError:
            raise HTTPException(404, "No flags on that invoice") from None
        db.audit(user["org"], user["sub"], "invoice_reviewed", f"{invoice_id}: {body.verdict}")
        return {"invoice_id": invoice_id, "status": body.verdict}

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
