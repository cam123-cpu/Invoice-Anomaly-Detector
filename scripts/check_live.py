"""Check a deployed server from the outside, the way an attacker would first look at it.

    python scripts/check_live.py https://invoices.yourcompany.com
    python scripts/check_live.py https://invoices.yourcompany.com --login-limit   # also tests lock-out

Run it from any computer after deploying and after each update. It needs only Python, and it
only reads: it never signs in and never changes anything. --login-limit sends a few wrong
passwords for a made-up user to see that sign-in is rate limited; it does not touch real
accounts. Exit code 0 means every check passed.

This is a quick first pass, not a substitute for a professional penetration test.
"""
from __future__ import annotations

import argparse
import datetime as dt
import http.client
import json
import socket
import ssl
import sys
import urllib.parse

RESULTS: list[tuple[bool, str, str]] = []
CAFILE: str | None = None     # a private certificate authority to trust (tests only)


def check(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append((bool(ok), name, detail))


def request(url: str, method: str = "GET", body: dict | None = None, headers: dict | None = None,
            verify: bool = True) -> http.client.HTTPResponse:
    u = urllib.parse.urlsplit(url)
    ctx = ssl.create_default_context(cafile=CAFILE) if verify else ssl._create_unverified_context()
    conn = (http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=15, context=ctx) if u.scheme == "https"
            else http.client.HTTPConnection(u.hostname, u.port or 80, timeout=15))
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"User-Agent": "invoice-detector-live-check", **({"Content-Type": "application/json"} if data else {}),
            **(headers or {})}
    conn.request(method, (u.path or "/") + ("?" + u.query if u.query else ""), body=data, headers=hdrs)
    resp = conn.getresponse()
    resp.body = resp.read(200_000)
    return resp


def tls_checks(host: str, port: int, min_days: int = 14) -> None:
    ctx = ssl.create_default_context(cafile=CAFILE)
    try:
        with socket.create_connection((host, port), timeout=15) as raw, ctx.wrap_socket(raw, server_hostname=host) as s:
            cert, version = s.getpeercert(), s.version()
        ends = dt.datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), dt.timezone.utc)
        days = (ends - dt.datetime.now(dt.timezone.utc)).days
        check(True, "Certificate is valid and trusted", f"for {host}")
        check(days >= min_days, "Certificate is not about to expire", f"{days} days left")
        check(version in ("TLSv1.2", "TLSv1.3"), "Modern TLS", version)
    except ssl.SSLError as exc:
        check(False, "Certificate is valid and trusted", str(exc))
        return
    old = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    old.check_hostname, old.verify_mode = False, ssl.CERT_NONE
    try:
        old.minimum_version = old.maximum_version = ssl.TLSVersion.TLSv1_1
        with socket.create_connection((host, port), timeout=15) as raw, old.wrap_socket(raw, server_hostname=host):
            check(False, "Old TLS 1.0/1.1 refused", "the server accepted TLS 1.1")
    except (ssl.SSLError, ValueError, OSError):
        check(True, "Old TLS 1.0/1.1 refused")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", help="the site's address, e.g. https://invoices.example.com")
    ap.add_argument("--allow-http", action="store_true", help=argparse.SUPPRESS)  # testing on this computer only
    ap.add_argument("--cafile", help=argparse.SUPPRESS)                          # trust a test certificate authority
    ap.add_argument("--min-days", type=int, default=14, help="warn when the certificate expires sooner (default 14)")
    ap.add_argument("--login-limit", action="store_true", help="also check that wrong passwords get locked out")
    args = ap.parse_args()
    global CAFILE
    CAFILE = args.cafile
    base = args.url.rstrip("/")
    u = urllib.parse.urlsplit(base)
    if u.scheme != "https" and not args.allow_http:
        print("Give the https:// address.")
        return 2
    host = u.hostname

    if u.scheme == "https":
        tls_checks(host, u.port or 443, args.min_days)

    try:
        r = request(f"http://{host}/" if u.scheme == "https" else base + "/")
        loc = r.getheader("Location", "")
        check(r.status in (301, 302, 307, 308) and loc.startswith("https://"), "Plain http redirects to https",
              f"{r.status} {loc}")
    except OSError as exc:
        check(True, "Plain http redirects to https", f"port 80 closed ({exc.__class__.__name__})")

    home = request(base + "/")
    h = {k.lower(): v for k, v in home.getheaders()}
    check(home.status == 200, "Site answers", str(home.status))
    hsts = h.get("strict-transport-security", "")
    check("max-age=" in hsts and int(hsts.split("max-age=")[1].split(";")[0]) >= 15_552_000,
          "HSTS on (at least 6 months)", hsts or "missing")
    for name, want in (("content-security-policy", "default-src"), ("x-content-type-options", "nosniff"),
                       ("x-frame-options", "DENY"), ("referrer-policy", "no-referrer")):
        check(want.lower() in h.get(name, "").lower(), f"Header {name}", h.get(name, "missing"))
    check("server" not in h or not any(c.isdigit() for c in h["server"]), "Server version not shown",
          h.get("server", "hidden"))

    for path in ("/docs", "/openapi.json", "/redoc", "/.env", "/.git/config", "/deploy/secrets/secret_key"):
        r = request(base + path)
        check(r.status in (401, 403, 404), f"Not exposed: {path}", str(r.status))

    for path in ("/me", "/invoices", "/review-queue", "/users", "/settings", "/audit"):
        r = request(base + path)
        check(r.status == 401, f"Needs sign-in: {path}", str(r.status))

    r = request(base + "/health/ready")
    check(r.status == 200 and len(r.body) < 200, "Health check says nothing sensitive", r.body[:80].decode(errors="replace"))

    r = request(base + "/login", "POST", {"username": "x", "password": "y"}, {"Origin": "https://evil.example"})
    check(r.status == 403, "Requests from other sites refused", str(r.status))
    acao = r.getheader("Access-Control-Allow-Origin")
    check(acao in (None, base), "Other sites can't read answers (CORS)", acao or "not allowed")

    r = request(base + "/login", "POST", {"username": "live-check-nobody", "password": "wrong-password-1"})
    text = r.body.decode(errors="replace")
    check(r.status == 401 and "Traceback" not in text and "sqlite" not in text.lower() and "psycopg" not in text,
          "Wrong password gives a plain refusal", f"{r.status} {text[:80]}")
    cookie = r.getheader("Set-Cookie") or ""
    check(not cookie, "No session cookie for a failed sign-in", cookie[:60])

    if args.login_limit:
        statuses = []
        for i in range(25):
            statuses.append(request(base + "/login", "POST", {"username": "live-check-nobody",
                                                              "password": f"wrong-password-{i}"}).status)
            if statuses[-1] == 429:
                break
        check(429 in statuses, "Repeated wrong passwords are locked out", f"after {len(statuses)} tries")

    width = max(len(n) for _, n, _ in RESULTS)
    for ok, name, detail in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
    failed = sum(not ok for ok, _, _ in RESULTS)
    print(f"\n{len(RESULTS) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
