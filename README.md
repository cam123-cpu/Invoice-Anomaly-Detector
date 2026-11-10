# Invoice Anomaly Detector

[![CI](https://github.com/cam123-cpu/Invoice-Anomaly-Detector/actions/workflows/ci.yml/badge.svg)](https://github.com/cam123-cpu/Invoice-Anomaly-Detector/actions/workflows/ci.yml)

Flags suspicious invoices for **human review**, using eleven transparent audit rules plus an
Isolation Forest, and ranks them in a risk-scored review queue. It produces signals, not proof
of fraud.

## What it detects

| Rule | Weight | Idea |
|---|---|---|
| `duplicate` | 0.80 | Same vendor + amount + date as an earlier invoice |
| `bank_account_change` | 0.80 | Established vendor, first payment to a new bank account |
| `shared_bank_account` | 0.70 | One bank account paid to several vendors |
| `amount_outlier` | 0.55–0.75 | Far above the vendor's own history (robust z-score on log amounts) |
| `split_invoice` | 0.50 | Several invoices in a few days, each under the approval threshold, together over it |
| `near_duplicate` | 0.50 | Same vendor + amount a few days apart |
| `similar_vendor_name` | 0.50 | Vendor name almost identical to another vendor's |
| `new_vendor_large` | 0.40 | Large invoice from a vendor with almost no history |
| `ml_outlier` | 0.35 | Isolation Forest over amount, vendor deviation, timing and vendor history |
| `below_threshold` | 0.20 | Just under the approval threshold |
| `round_amount` | 0.15 | Round figures such as 4,000.00 |
| `weekend_invoice` | 0.10 | Dated on a weekend |

**Risk score (0–100):** weights combine as a noisy-OR, `risk = 1 − Π(1 − weight)`. Several weak
signals add up, but they never outrank one strong signal. Thresholds live in `DetectionConfig`
(`src/invoice_detector/detection.py`).

## How good is it?

```bash
python -m invoice_detector.cli evaluate
```

On 10 synthetic datasets (2,583 invoices, 14 injected anomalies each), at a risk cut-off of 30:

| Metric | Result |
|---|---|
| Precision | 96.9% |
| Recall | 87.9% |
| Top-k hit rate | 97.1% |
| Per-rule recall | 100% for all eleven rules |

The missed 12% are invoices whose only signal is a low-weight one (a round amount or a weekend
date), which score below the cut-off by design.

**These numbers are an upper bound.** The anomalies were injected by the same code base that
detects them, and real invoices are messier. Measure on your own labelled data before trusting
any figure here.

## Security features

- **Secrets in environment variables** (`.env` is git-ignored; `.env.example` is the template)
- **Passwords hashed** with salted scrypt; constant-time comparison
- **Rate-limited login**: 5 failures per 15 minutes per IP + username, then a lockout
- **Signed, expiring tokens** and **role-based access** (admin / accountant / auditor)
- **Parameterized SQL everywhere**; sort columns go through an allow-list
- **Bank accounts encrypted** with AES-256-GCM (key rotation supported) plus a keyed blind
  index, so detection compares accounts without ever decrypting them
- **LLM privacy**: explanations get only invoice IDs, rule names and numbers
- **Postgres row-level security** in `postgres/` for a production database

## Run it locally

```bash
python -m venv .venv
source .venv/bin/activate            # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"

python -m invoice_detector.cli init                  # creates .env with fresh secrets
python -m invoice_detector.cli create-user alice admin
python -m invoice_detector.cli seed                  # synthetic invoices with injected anomalies
python -m invoice_detector.cli detect                # prints the ranked review queue
uvicorn invoice_detector.api:create_app --factory --reload
```

Open http://127.0.0.1:8000/docs, call `/login`, click **Authorize** and paste the token, then
try `/detect`, `/review-queue` and `/anomalies`.

### Use your own invoices

```bash
python -m invoice_detector.cli import-csv invoices.csv
python -m invoice_detector.cli detect
```

The CSV needs the columns `id,vendor,amount,date` (date as `yyyy-mm-dd`) and may include
`bank_account`. Bad rows and already-stored ids are skipped and counted.

## API

| Endpoint | Who | What |
|---|---|---|
| `POST /login` | anyone | Returns a bearer token (rate limited) |
| `POST /invoices` | admin, accountant | Add an invoice |
| `GET /invoices` | any role | List invoices (no bank data) |
| `GET /invoices/{id}/bank` | admin | Decrypt one bank account |
| `POST /detect` | admin, accountant | Run detection and store the flags |
| `GET /review-queue` | any role | Flagged invoices ranked by risk, with reasons |
| `GET /anomalies` | any role | Every individual flag |
| `POST /explain` | any role | Plain-English summary (uses Claude if a key is set) |

## Test it

```bash
pytest -v
ruff check .
```

Tests cover each detection rule, a quality bar on synthetic data (so a threshold change that
hurts precision or recall fails the build), password hashing, token tampering and expiry,
rate-limit lockouts, encryption, SQL-injection strings, CSV import and the API.

## Layout

```
src/invoice_detector/   detection, evaluate, demo, api, cli, db, crypto, security, config, explain
tests/                  unit tests (unittest style, run by pytest)
postgres/               row-level-security SQL and client (for production; untested here)
prototypes/             the original Gleam version
.github/workflows/      CI: lint, tests on Linux and Windows, quality report, dependency audit
```

## Docker

```bash
docker build -t invoice-detector .
docker run -p 8000:8000 --env-file .env -v invoice-data:/data invoice-detector
```

## Before real use

- Move from SQLite to PostgreSQL and apply `postgres/rls_setup.sql`.
- Keep keys in a secrets manager (AWS/GCP KMS, Vault), not a `.env` file. Losing the keys
  makes the encrypted bank accounts unreadable.
- Serve behind HTTPS. Behind a proxy, run uvicorn with `--proxy-headers` so rate limits see
  real client IPs.
- The login limiter is in memory; use Redis if you run more than one server.
- Tune `DetectionConfig` on your own data, especially `approval_threshold`.
- Check the model name in `explain.py` against current Anthropic docs.
