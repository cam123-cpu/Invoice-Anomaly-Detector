"""A stand-in identity provider for the browser test (python tests/e2e/fake_idp_server.py PORT).

It behaves like Microsoft Entra ID or Google at the protocol level: discovery document,
published signing keys, a sign-in page, and a token endpoint that checks the client secret
and the PKCE verifier before returning a signed ID token. Anyone can "sign in" as any
email address, so it must only ever be used for tests.
"""
import base64
import hashlib
import html
import os
import secrets
import sys
from urllib.parse import parse_qs, urlencode

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fake_idp import FakeIdP  # noqa: E402

PORT = int(sys.argv[1])
ISSUER = f"http://127.0.0.1:{PORT}"
CLIENT_ID, CLIENT_SECRET = "test-client", "test-secret"
idp = FakeIdP(ISSUER, CLIENT_ID)
pending: dict[str, dict] = {}  # code -> {claims, challenge, redirect_uri}


async def discovery(request: Request):
    return JSONResponse(idp.get_json(ISSUER + "/.well-known/openid-configuration"))


async def jwks(request: Request):
    return JSONResponse(idp.get_json(ISSUER + "/jwks"))


async def authorize(request: Request):
    if request.method == "GET":
        q = request.query_params
        hidden = "".join(f'<input type="hidden" name="{k}" value="{html.escape(q.get(k, ""))}">'
                         for k in ("redirect_uri", "state", "nonce", "code_challenge", "client_id"))
        return HTMLResponse(f"""<!doctype html><title>Test IdP</title>
            <h1>Test identity provider</h1><form method="post">{hidden}
            <label>Email <input name="email" id="idp-email"></label>
            <button type="submit">Continue</button></form>""")
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    if form.get("client_id") != CLIENT_ID:
        return HTMLResponse("unknown client", status_code=400)
    code = secrets.token_urlsafe(16)
    pending[code] = {"claims": idp.claims(form["email"], form["nonce"]), "challenge": form["code_challenge"],
                     "redirect_uri": form["redirect_uri"]}
    return RedirectResponse(form["redirect_uri"] + "?" + urlencode({"code": code, "state": form["state"]}), 302)


async def token(request: Request):
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    entry = pending.pop(form.get("code", ""), None)
    verifier = form.get("code_verifier", "")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    if (entry is None or form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET
            or challenge != entry["challenge"] or form.get("redirect_uri") != entry["redirect_uri"]):
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    return JSONResponse({"id_token": idp.sign(entry["claims"]), "token_type": "Bearer"})


app = Starlette(routes=[
    Route("/.well-known/openid-configuration", discovery), Route("/jwks", jwks),
    Route("/authorize", authorize, methods=["GET", "POST"]), Route("/token", token, methods=["POST"]),
])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
