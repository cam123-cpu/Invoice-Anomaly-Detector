# Invoice Anomaly Detector

[![CI](https://github.com/cam123-cpu/Invoice-Anomaly-Detector/actions/workflows/ci.yml/badge.svg)](https://github.com/cam123-cpu/Invoice-Anomaly-Detector/actions/workflows/ci.yml)

Flags suspicious invoices for **human review**. Many companies can share one deployment, each
seeing only its own data. Twelve signals (eleven audit rules plus an Isolation Forest) feed a
risk-ranked review queue, and each company's review decisions tune the scoring for that company.
It produces signals, not proof of fraud.

It comes with a **web app** for accounts-payable teams: upload an Excel or CSV export, match its
columns once, work a ranked review queue with Confirm and Dismiss buttons, and **download your own
workbook back with every row's risk, status and reasons written in**.

**New here? Follow the [tutorial](docs/TUTORIAL.md).**

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
(`src/invoice_detector/detection.py`); the approval threshold is a per-company setting.

**Learning from reviewers:** when staff mark flagged invoices as confirmed or dismissed, each
rule's weight for that company is multiplied by `(confirmed + 5) / (confirmed + dismissed + 5)`.
Rules that keep raising false alarms fade, and `GET /metrics` reports the measured precision
per rule. This re-weights the rules; it does not retrain the Isolation Forest.

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

## Many companies, kept apart

| Layer | What it does |
|---|---|
| Signed token | Every request carries the caller's company inside a tamper-proof token |
| Application | Every query is filtered by that company's `org_id` |
| PostgreSQL row-level security | The database refuses to return or accept another company's rows, even for a query with no filter |
| Least-privilege role | The web server's database role cannot create companies, edit or delete invoices, alter the audit log, or switch the protection off |
| Per-company keys | Bank accounts are encrypted with a key derived for each company, so one company's data cannot be decrypted with another's key |

`tests/test_tenancy.py` proves each of these, and CI runs it against a real PostgreSQL 16 server
on every push. SQLite, used for local runs, has the first two layers and the last.

**Limits to know about:**

- Row-level security protects against application bugs such as a forgotten filter. It does not
  help if an attacker can run arbitrary SQL as the app role, because the role sets the current
  company itself. Parameterized queries everywhere are the defence against that.
- Role checks (admin, accountant, auditor) are enforced in the application, not the database.
- No single sign-on or multi-factor login yet, and the login rate limiter is per server.
- One database connection per server process. Add a connection pool for real load.
- Amounts are stored as floating-point numbers, which is fine for detection but not for ledgers.

## Other security features

- **Secrets in environment variables** (`.env` is git-ignored; `.env.example` is the template)
- **Passwords hashed** with salted scrypt; constant-time comparison
- **Rate-limited login**: 5 failures per 15 minutes per IP + username, then a lockout
- **Parameterized SQL everywhere**; sort columns go through an allow-list
- **Bank accounts encrypted** with AES-256-GCM (key rotation supported) plus a keyed blind
  index, so detection compares accounts without ever decrypting them
- **Audit trail** of logins, imports, detection runs, reviews and bank-account views
- **LLM privacy**: explanations get only invoice IDs, rule names and numbers

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate            # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"

python -m invoice_detector.cli init                       # creates .env with fresh secrets
python -m invoice_detector.cli create-org "Acme Inc"
python -m invoice_detector.cli create-user "Acme Inc" alice admin
python -m invoice_detector.cli seed "Acme Inc"            # optional: synthetic invoices with planted anomalies
uvicorn invoice_detector.api:create_app --factory --reload
```

Then open http://127.0.0.1:8000 and sign in. The [tutorial](docs/TUTORIAL.md) walks through
uploading your own files, the review queue, the PostgreSQL setup and how to fit this into a
payment process. The raw API is documented at http://127.0.0.1:8000/docs.

## The web app

| Page | What it does |
|---|---|
| Dashboard | Invoices, open and high-risk flags, confirmed problems, false alarms, flag accuracy, and how each check is performing |
| Review queue | Flagged invoices by risk with reasons; Confirm or Dismiss each one |
| Upload invoices | Excel (.xlsx) or CSV; suggested column matching, date-format detection, per-row error report, remembered mappings |
| Invoices | Search and sort; admins can reveal a bank account (logged) |
| Activity log | The company's audit trail |
| Settings | Approval threshold, people and roles |

### Excel in, Excel out

- **Reads real exports:** .xlsx (including multi-sheet workbooks, where you pick the sheet) and
  .csv. Title rows, `$1,234.50` or `1.234,56`, Excel dates, day-first dates, blank and totals rows
  are all handled, and every row that can't be used is listed with the reason.
- **Writes results into your workbook:** "Download your file with flags" returns your own file
  with Risk, Review status, Flags and Why flagged columns added to each row, risky rows shaded,
  a ranked *Flagged invoices* sheet and an *About these flags* sheet. Your cells and formulas are
  left exactly as they were (pictures and charts are not copied, a limit of the Excel library).
- **Reports:** the review queue downloads as a formatted workbook from the Dashboard or Review queue.
- **Safe to open:** text the app writes is always stored as text, so a vendor name like
  `=HYPERLINK(...)` can never become a live formula in Excel.
- **Samples:** four example files in [`samples/`](samples/), also one click away on the Upload page.

It is plain HTML, CSS and JavaScript served by the API, with no third-party scripts, under a strict
Content-Security-Policy. Uploaded text is always displayed as text, never run. CI tests it in a
real browser (Chromium) against the real server, including a file with a hostile vendor name,
role restrictions, company isolation and a phone-sized screen.

## API

| Endpoint | Who | What |
|---|---|---|
| `POST /login` | anyone | Returns a bearer token (rate limited) |
| `GET /me` | any role | Your username, role and company |
| `POST /import/preview`, `POST /import` | admin, accountant | Read an Excel/CSV file and suggest a column mapping; import it |
| `POST /invoices`, `POST /invoices/bulk` | admin, accountant | Add one invoice, or up to 1,000 |
| `GET /summary` | any role | Dashboard numbers |
| `POST /export/annotated` | any role | Your uploaded workbook with results written in (.xlsx) |
| `GET /export/review.xlsx` | any role | The review queue as an Excel report |
| `GET /samples`, `GET /samples/{name}` | anyone | Sample Excel files and a blank template |
| `GET /invoices` | any role | List invoices (no bank data) |
| `GET /invoices/{id}/bank` | admin | Decrypt one bank account (audited) |
| `POST /detect` | admin, accountant | Run detection and store the flags |
| `GET /review-queue` | any role | Flagged invoices ranked by risk, with reasons and status |
| `POST /review/{id}` | admin, accountant | Mark a flagged invoice confirmed or dismissed |
| `GET /metrics` | any role | Measured precision per rule and the weights in use |
| `GET /anomalies` | any role | Every individual flag |
| `GET`, `PUT /settings` | any role, admin | The company's approval threshold |
| `GET`, `POST /users` | admin | List or add users in your own company |
| `GET /audit` | admin, auditor | The company's audit trail |
| `POST /explain` | any role | Plain-English summary (uses Claude if a key is set) |

Everything is scoped to the caller's company.

## Test it

```bash
pytest -v
ruff check .
```

Tests cover each detection rule, a quality bar on synthetic data (so a threshold change that
hurts precision or recall fails the build), feedback learning, tenant isolation, password
hashing, token tampering and expiry, rate-limit lockouts, encryption, SQL-injection strings,
Excel/CSV parsing, the API and the web app in a real browser. To include the PostgreSQL tests, see part 2 of the tutorial.

## Layout

```
src/invoice_detector/   detection, evaluate, demo, importer, excel_out, samples, api, cli, db, schema, crypto, security, config, explain
src/invoice_detector/web/  the web app (index.html, app.js, app.css)
tests/                  unit tests (unittest style, run by pytest); tests/e2e is the browser test
docs/TUTORIAL.md        step-by-step guide
samples/                sample Excel files (regenerate with scripts/make_samples.py)
prototypes/             the original Gleam version
.github/workflows/      CI: lint, tests on Linux, Windows and PostgreSQL, browser test, Docker build, dependency audit
```

## Docker

```bash
docker build -t invoice-detector .
docker run -p 8000:8000 --env-file .env -v invoice-data:/data invoice-detector
```
