"""FastAPI service: login (rate limited), role-based access, detection, explanations.

Run:  uvicorn invoice_detector.api:create_app --factory
"""
import sqlite3
from datetime import date as Date

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from .config import Settings
from .crypto import Vault
from .db import Database
from .detection import Anomaly, detect, review_queue
from .explain import explain
from .security import (DUMMY_HASH, AuthError, RateLimiter, RateLimitError, TokenSigner,
                       verify_password)


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class InvoiceIn(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    vendor: str = Field(min_length=1, max_length=120)
    amount: float = Field(gt=0, lt=1e12)
    date: Date
    bank_account: str | None = Field(default=None, max_length=64)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db = Database(settings.db_path, Vault.from_settings(settings))
    signer = TokenSigner(settings.secret_key)
    limiter = RateLimiter()
    bearer = HTTPBearer(auto_error=False)

    app = FastAPI(title="Invoice Anomaly Detector", version="0.2.0")
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
        user = db.get_user(body.username)
        ok = verify_password(body.password, user["password_hash"] if user else DUMMY_HASH)
        if not (user and ok):
            limiter.record_failure(key)
            raise HTTPException(401, "Invalid credentials")  # same message either way
        limiter.record_success(key)
        return {"access_token": signer.make(user["username"], user["role"]),
                "token_type": "bearer"}

    @app.post("/invoices", status_code=201)
    def add_invoice(inv: InvoiceIn, user: dict = Depends(require("admin", "accountant"))) -> dict:
        try:
            db.add_invoice(inv.id, inv.vendor, inv.amount, inv.date.isoformat(),
                           inv.bank_account, user["sub"])
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Invoice id already exists") from None
        return {"id": inv.id}

    @app.get("/invoices")
    def list_invoices(sort: str = "invoice_date", limit: int = 50,
                      user: dict = Depends(current_user)) -> list[dict]:
        try:
            return db.list_invoices(sort, limit)
        except ValueError:
            raise HTTPException(422, "Invalid sort column") from None

    @app.get("/invoices/{inv_id}/bank")
    def bank_account(inv_id: str, user: dict = Depends(require("admin"))) -> dict:
        try:
            return {"id": inv_id, "bank_account": db.get_bank_account(inv_id)}
        except KeyError:
            raise HTTPException(404, "Invoice not found") from None

    @app.post("/detect")
    def run_detection(user: dict = Depends(require("admin", "accountant"))) -> dict:
        invoices = db.detection_inputs()
        found = detect(invoices)
        db.save_flags(found)
        return {"invoices_checked": len(invoices), "anomalies": len(found)}

    @app.get("/anomalies")
    def anomalies(user: dict = Depends(current_user)) -> list[dict]:
        return db.list_anomalies()

    @app.get("/review-queue")
    def queue(limit: int = 50, user: dict = Depends(current_user)) -> list[dict]:
        """Flagged invoices ranked by combined risk (0-100), with the reasons."""
        ranked = review_queue([Anomaly(**a) for a in db.list_anomalies(1000)])
        return ranked[:max(1, min(limit, 500))]

    @app.post("/explain")
    def explain_anomalies(user: dict = Depends(current_user)) -> dict:
        return {"explanation": explain(db.list_anomalies(20), settings.anthropic_api_key)}

    return app
