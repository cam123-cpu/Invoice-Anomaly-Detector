# Invoice Anomaly Detector

Flags suspicious invoices for **human review** using transparent rules plus an Isolation Forest.
It produces signals, not proof of fraud.

## What it detects

| Rule | Severity | Idea |
|---|---|---|
| `duplicate` | high | Same vendor + amount + date as an earlier invoice |
| `shared_bank_account` | high | One bank account paid to several vendors (fake-vendor signal) |
| `amount_outlier` | medium/high | Robust z-score (median/MAD) vs the vendor's history |
| `ml_outlier` | medium | Isolation Forest over amount, vendor deviation, weekday, day of month |
| `round_amount` | low | Round figures such as 9,000.00 |
| `weekend_invoice` | low | Dated on a weekend |

## Security features

- **Secrets in environment variables** (`.env` is git-ignored; `.env.example` is the template)
- **Passwords hashed** with salted scrypt; constant-time comparison
- **Rate-limited login**: 5 failures per 15 min per IP + username, then a lockout
- **Signed, expiring tokens** and **role-based access** (admin / accountant / auditor)
- **Parameterized SQL everywhere**; sort columns go through an allow-list
- **Bank accounts encrypted** with AES-256-GCM (key rotation supported) plus a keyed blind index
- **LLM privacy**: explanations get only invoice IDs, rule names and numbers; no bank or vendor data
- **Postgres row-level security** in `postgres/` for a production database

## Layout

```
src/invoice_detector/   config, security, crypto, db, detection, explain, api, cli, demo
tests/                  unit tests (unittest style, run by pytest)
postgres/               row-level-security SQL and client (for production)
prototypes/             the original Gleam version
.github/workflows/      CI: lint, tests, dependency audit
```

## Run it locally

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

python -m invoice_detector.cli gen-keys > .env           # creates your secrets (Windows PowerShell: pipe to Out-File -Encoding ascii .env)
python -m invoice_detector.cli create-user alice admin
python -m invoice_detector.cli seed                 # 205 synthetic invoices, 5 injected anomalies
uvicorn invoice_detector.api:create_app --factory --reload
```

Open http://127.0.0.1:8000/docs, click **Authorize** after calling `/login`, then try `/detect` and `/anomalies`.

```bash
TOKEN=$(curl -s -X POST localhost:8000/login -H 'content-type: application/json' \
  -d '{"username":"alice","password":"YOUR_PASSWORD"}' | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -X POST localhost:8000/detect -H "Authorization: Bearer $TOKEN"
curl localhost:8000/anomalies -H "Authorization: Bearer $TOKEN"
```

## Test it

```bash
pytest -v          # or: PYTHONPATH=src:tests python -m unittest discover -s tests -v
ruff check .
```

The tests cover password hashing, token tampering and expiry, rate-limit lockouts, encryption
(tampering, wrong context, key rotation), SQL-injection strings, and detection recall on data
with known injected anomalies. API tests need `fastapi` and `httpx` (installed by `.[dev]`).

## Put it on GitHub

```bash
git init
git add .
git status                      # CONFIRM .env and *.db are NOT listed
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/invoice-anomaly-detector.git
git push -u origin main
```

Then on GitHub: **Actions** tab (CI runs automatically), **Settings > Code security** (enable
Dependabot alerts, secret scanning and push protection), **Settings > Branches** (require the CI
check before merging).

## Docker

```bash
docker build -t invoice-detector .
docker run -p 8000:8000 --env-file .env -v invoice-data:/data invoice-detector
```

## Before real use

- Move from SQLite to PostgreSQL and apply `postgres/rls_setup.sql`.
- Keep keys in a secrets manager (AWS/GCP KMS, Vault), not a `.env` file.
- Serve behind HTTPS. Behind a proxy, run uvicorn with `--proxy-headers` so rate limits see real IPs.
- The login limiter is in memory; use Redis if you run more than one server.
- Tune thresholds on your own data and measure false positives before trusting any flag.
- Check the model name in `explain.py` against current Anthropic docs.
