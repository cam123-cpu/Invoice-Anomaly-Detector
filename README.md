# Invoice Anomaly Detector

[![CI](https://github.com/cam123-cpu/Invoice-Anomaly-Detector/actions/workflows/ci.yml/badge.svg)](https://github.com/cam123-cpu/Invoice-Anomaly-Detector/actions/workflows/ci.yml)

Flags suspicious invoices for **human review**. Many companies can share one deployment, each
seeing only its own data. Twelve signals (eleven audit rules plus an Isolation Forest) feed a
risk-ranked review queue, and each company's review decisions tune the scoring for that company.
It produces signals, not proof of fraud.

It comes with a **web app** for accounts-payable teams: upload an Excel or CSV export, match its
columns once, and work a ranked review queue. **Confirming a problem decides what happens to the
payment** (Do not pay, Hold, or Pay anyway) and shows what to do next. Download your own workbook
back with every row's risk, decision and next steps written in, and a **payment file** that
sorts every invoice into Ready to pay, Review first, On hold and Do not pay.

**New here? Follow the [tutorial](docs/TUTORIAL.md).** To understand how the code works, read
[How it works](docs/HOW-IT-WORKS.md): architecture, data structures, algorithms and security, explained in depth.

## What it detects

| Rule | Weight | Idea |
|---|---|---|
| `duplicate` | 0.80 | Same vendor + amount + date as an earlier invoice |
| `bank_account_change` | 0.80 | Established vendor, first payment to a new bank account |
| `shared_bank_account` | 0.70 | One bank account paid to several vendors |
| `duplicate_number` | 0.70 | The vendor's invoice number seen before in another spelling (`INV-1001` / `INV1001`), with a new date or amount |
| `amount_outlier` | 0.55–0.75 | Far above the vendor's own history (robust z-score on log amounts) |
| `totals_mismatch` | 0.45 | The invoice's own line items, subtotal, tax and total don't agree (invoices read from documents) |
| `credited_invoice` / `matched_credit` | 0.65 | An invoice and the credit note that cancels it in full (flagged as a pair) |
| `duplicate_credit` | 0.50 | The same credit note recorded twice |
| `unmatched_credit` | 0.45 | A credit note from a vendor with nothing (or less) billed, or naming an invoice that isn't on file, is another vendor's, or is already fully credited |
| `reissued_invoice` | 0.35 | Same vendor and amount as an invoice a credit note cancelled: held until the credit is confirmed (high risk if the original was already paid) |
| `future_date` | 0.40 | Dated more than 7 days after today |
| `sequential_numbers` | 0.30 | The vendor's invoices to you are numbered with no gaps: you may be its only customer (a shell company sign). A company-wide internal numbering is recognised and ignored |
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

## Credit notes

A credit note is a document type of its own, not a negative invoice. It is stored with a
positive amount and its type, and shown with a minus sign, so every total is net.

- **In a list:** a negative amount (`-120.00`, `(120.00)`, `120.00-`) or a Type column that
  says credit ("Credit Memo", "Vendor Credit", "Gutschrift"...) makes the row a credit note.
- **As a document:** recognised by its title ("Credit note", "Avoir", "Rechnungskorrektur"...);
  the review screen has a Type box to change it.
- **Which invoice it corrects:** read from e-invoice data, from the page ("Credit for invoice
  INV-5521", "zu Rechnung Nr.", "Avoir sur facture n°"...), from an *Applies to* column, or typed
  on the review screen.
- **Checks of their own:** a credit that cancels an invoice in full (the one it names, or else
  one for exactly its amount from the same vendor) is flagged together with that invoice
  (*Cancelled by a credit note* / *Cancels an invoice*), so the invoice isn't paid and the credit
  isn't also deducted elsewhere. A credit is also flagged if it comes from a vendor with nothing
  billed, names an invoice that isn't on file or belongs to another vendor, pushes the credits
  for one invoice above the invoice itself, or is recorded twice.
- **The re-issued invoice** is not called a duplicate of the one it replaces, but it waits
  (*Replaces a cancelled invoice*, hold) until the credit is confirmed with the vendor. Otherwise
  a fake credit note could "cancel" an invoice that was already paid and slip its copy through.
- Credits for part of an invoice (a price adjustment, a return) are ordinary: they reduce what
  the vendor is paid. They are left out of the checks that learn what is normal for a vendor.
- The dashboard and the payment file show how much credit is included and how much is ready
  to deduct.

## Invoice documents (PDF, e-invoices, Excel invoices, scans)

Besides exports (one invoice per row), the app reads the invoices themselves, one per file:

| File | How it's read | Where |
|---|---|---|
| E-invoice: ZUGFeRD, Factur-X, XRechnung (in a PDF or as .xml), UBL / PEPPOL, FatturaPA | Its structured data, exactly; the page is checked against it | On your server |
| PDF made by software | Its text and where each word sits on the page (pdfplumber) | On your server |
| Excel invoice form (like Microsoft's invoice templates) | Each cell's value and position | On your server |
| Photo or scanned PDF | OCR with Tesseract, then the same reading | On your server (optional extra) |
| Anything the above can't read | Claude, through a fixed JSON form | Only if an admin switches on AI reading |

All of them become the same thing, positioned pieces of text, and one reader finds the fields
the way a person reads a form: a label ("Invoice #", "Amount due", "Bill to") and the value
after it, beside it or under it. The vendor comes from the payee line or the name at the top,
never the customer. Every field gets a confidence, and the invoice's own arithmetic is checked
(line items → subtotal → + tax and shipping − discount and deposit → total).

Nothing is saved until a person has checked each invoice: the review screen outlines unsure
fields in amber and missing ones in red, and shows the text each value came from. Then:

- the same file can't be imported twice (its fingerprint is stored, not the file);
- a vendor spelled differently only in formatting ("Inc." vs "INC") is matched to the existing
  vendor, but a different spelling ("Acme Suplies") is kept as printed and pointed out, since
  that's what a look-alike vendor looks like;
- corrections are recorded, and totals that don't add up become a detection flag.

The reader knows English, German, French, Dutch, Spanish and Italian labels
(`doc_languages.py`; adding a language is adding its words), month names and day-first dates,
and recognises a credit note by its title or by a total printed negative.

**E-invoices are read exactly** (`einvoice.py`). Many European invoices carry their data as
XML, inside the PDF or as the file itself. When it is there, every field comes from it, with
no guessing. It is also a check: if the page shows a total, invoice number or bank account
that the data doesn't contain anywhere, the PDF contradicts itself, which is how an altered
invoice looks, and the invoice is flagged. The XML is parsed with defusedxml (no DTDs or
entities), at most 2 MB; a hostile attachment just means the page is read instead.

**It learns from corrections.** When someone corrects the invoice number, date or total to a
value that was printed beside another label, the app remembers that label for that vendor,
in that company only. The vendor's next invoice is read there, with a note saying so. A
lesson that keeps being corrected away is dropped, and each new lesson is in the activity log.

**How accurate?** Two different answers, and the second is the one that matters:

- *On generated invoices*: `cli evaluate-documents 200` reads 200 invoices this project
  generates itself. Every field 100%, every planted totals error caught, no false alarms.
  That only shows the reader handles the layouts its author thought of.
- *On real invoices nobody here designed*: `scripts/real_invoice_benchmark.py` downloads two
  public collections (156 different PDFs from 24 vendors, mostly German, French and Dutch)
  and compares the reading with their known answers:

  | Set (reading the page only) | Documents | All four fields right | First run |
  |---|---|---|---|
  | Real company invoices (invoice2data) | 10 | 70% | 10% |
  | E-invoices, vendors used for tuning | 102 | 79% | 0% |
  | E-invoices, vendors held out | 44 | 84% | 0% (73% after the first round of fixes, before any fix made from this set) |
  | **E-invoices read from their own data** | 146 | **100%** | n/a |

  By field, reading the page of all 156: invoice number 90%, vendor 96%, date 96%, total 94%.
  One document in 156 was wrongly told its totals don't add up. CI re-runs this on every push
  and fails if any set drops below 60% or e-invoice data isn't read exactly.

So for a paper or plain-PDF invoice expect roughly four in five to need no correction, and
one in five to need a field fixed on the review screen; an e-invoice needs none. Vendors whose name exists only inside a logo image, and
receipts that aren't invoices, are the usual misses. Measure it on your own vendors before
relying on it: put a sample in a folder with a `truth.csv` and run
`cli evaluate-documents --folder DIR`.

**Safety:** uploaded files are parsed in a separate process with a time limit (and a memory
limit on Linux), so a broken or hostile file can't hang the server. Files must really be what
their name says, and oversized images are refused. AI reading sends the file to Anthropic, so
it is off by default, switched on per company, rate limited and written to the activity log;
text inside an invoice is treated as data, never as instructions.

## How good is it?

```bash
python -m invoice_detector.cli evaluate
```

On 10 synthetic datasets (2,703 invoices, 17 planted cases each, covering every rule), at a
risk cut-off of 30:

| Metric | Result |
|---|---|
| Precision | 98.5% |
| Recall | 92.9% |
| Top-k hit rate | 98.6% |
| Per-rule recall | 100% for all eighteen planted rules |

The missed 12% are invoices whose only signal is a low-weight one (a round amount or a weekend
date), which score below the cut-off by design.

**These numbers are an upper bound.** The anomalies were injected by the same code base that
detects them, and real invoices are messier. Measure on your own labelled data before trusting
any figure here.

## Payment controls

The checks an auditor asks about first are built in (`controls.py`):

- **Separation of duties.** Whoever added an invoice can't release its flags; someone else
  reviews it. Applies whenever the company has two or more people who can review.
- **Second approval.** Releasing a flagged invoice at or above the company's approval
  threshold (Settings; 10,000 by default), by dismissing its flags or choosing "pay anyway",
  needs a second person: not the reviewer and not whoever added it. Until then it stays in
  *Review first*. Changing the review clears the approval.
- **Paid status.** Invoices whose decision is *Pay* can be marked paid (one at a time on the
  Invoices page, or in bulk with `POST /payments`). Paid invoices leave the payment file
  (sheet *Already paid*). A duplicate or re-issue of a paid invoice says so, and a credit
  note for a paid invoice becomes a refund due. Admins can undo a payment marked by mistake.
- **Partial credits** are shown against the invoice they name: *Pay 800.00 after credit note
  CN-1 (-100.00)*.
- Every review, approval, payment, undo and settings change is in the activity log.

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
- Detection runs inside the web request (a lock makes two copies of the app take turns per company). A very large company would move it to a background job.
- Microsoft, Google and SMTP setups are tested against stand-ins in CI, not the real services; check them with one real account when you set them up.
- One database connection per server process. Add a connection pool for real load.
- Amounts are stored as floating-point numbers, which is fine for detection but not for ledgers.

## Signing in

- **Single sign-on** with Microsoft (Entra ID / Office 365), Google Workspace, or any OpenID
  Connect provider (Okta, Auth0, Keycloak…). Authorization-code flow with PKCE; ID tokens are
  verified (signature, issuer, audience, expiry, nonce). Only people an admin has added can
  sign in, and SSO-only accounts have no password at all.
- **Two-factor authentication** with any authenticator app (TOTP, RFC 6238), shown as a QR code
  drawn by the app itself, plus ten single-use recovery codes. Admins can require it for the
  whole company; codes can't be replayed.
- **Self-service password reset** by email (30-minute, single-use link; same answer whether or
  not the account exists), and change-password in *Your account*. Changing a password signs
  out every other session.

## Hosting

[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) covers running it for a company:

- **Several copies of the app:** the app keeps nothing in memory between requests. Sign-in
  rate limits live in the database, and database upgrades and detection runs take a lock, so
  any number of copies can run behind a load balancer. `APP_REPLICAS=3` in
  `docker-compose.prod.yml` does it on one server; CI starts three copies and checks that they
  share one set of rate limits.
- **One server:** `docker-compose.prod.yml` runs HTTPS (Caddy, automatic Let's Encrypt
  certificates), PostgreSQL with row-level security, the app and nightly backups.
  `cli init-deploy` creates the secret files. CI starts this whole stack on every push and checks
  HTTPS, sign-in, and a backup → wipe → restore cycle.
- **Cloud:** Azure, AWS or Google Cloud with managed PostgreSQL (automatic backups,
  point-in-time restore) and the cloud's secrets manager.
- Every secret can come from an environment variable or a file (`NAME_FILE`), so nothing
  secret lives in `.env`, the image or the compose file. The app refuses to start with keys that
  don't match the database, and the guide covers backing up the keys separately from the data.

## Other security features

- **Secrets from a secrets manager or files** (`NAME` or `NAME_FILE`); `.env` only for local use
- **HTTPS enforced** in production: HTTP redirects to HTTPS, plus HSTS
- **Browser session in an HttpOnly cookie** (`__Host-`, Secure, SameSite=Strict) that page
  scripts can't read, with cross-site request forgery checks; programs use bearer tokens
- **Unknown request fields refused**, request size capped, uploads checked for their real file
  type and for zip bombs
- **Security headers** on every response (CSP, frame, referrer, permissions, cross-origin) and
  no caching of API answers; API docs switched off in production
- **Secret scanning** of the whole git history and a blocking dependency audit in CI
- **Penetration-tested**: findings and fixes are in [docs/SECURITY-REVIEW.md](docs/SECURITY-REVIEW.md),
  and every attack is kept as a test (`tests/test_attacks.py`) that fails if the hole reopens
- **Sign-out ends the session on the server**, on every device; an admin can **remove a
  person's access** at once (password, reset links and single sign-on)
- **Passwords hashed** with salted scrypt; constant-time comparison
- **Rate-limited sign-in**: 5 failures per 15 minutes per IP + username, then a lockout; 30 per
  address across all usernames (stops bots trying many accounts); limits on two-factor codes;
  at most 3 reset emails per person per hour
- **Parameterized SQL everywhere**; sort columns go through an allow-list
- **Bank accounts encrypted** with AES-256-GCM (key rotation supported) plus a keyed blind
  index, so detection compares accounts without ever decrypting them
- **Audit trail** of logins, imports, detection runs, reviews and bank-account views
- **LLM privacy**: explanations get only invoice IDs, rule names and numbers

## Quick start

**Windows, the easy way:** download the code (green *Code* button > *Download ZIP*, extract
it), then right-click `setup.ps1` > *Run with PowerShell*, and afterwards `start.ps1` the same
way. They run every tool as `python -m ...`, which works on laptops whose security policy
blocks `pip.exe` or `uvicorn.exe`.

**Any system, by hand:**

```bash
python -m venv .venv
source .venv/bin/activate            # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"

python -m invoice_detector.cli init                       # creates .env with fresh secrets
python -m invoice_detector.cli create-org "Acme Inc"
python -m invoice_detector.cli create-user "Acme Inc" alice admin
python -m invoice_detector.cli seed "Acme Inc"            # optional: synthetic invoices with planted anomalies
python -m uvicorn invoice_detector.api:create_app --factory --reload
```

Reading photos and scans of invoices is optional: `pip install -e ".[ocr]"` plus the Tesseract
program (Windows: the UB Mannheim installer; macOS: `brew install tesseract`; Linux:
`apt install tesseract-ocr`). PDFs and Excel invoices work without it. The Docker image includes it.

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
| Your account | Two-factor authentication, recovery codes, change password |
| Settings | Approval threshold, require two-factor, people and roles, reset a person's password or two-factor |

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
| `POST /login`, `POST /login/2fa` | anyone | Session cookie and bearer token, or a second step asking for a two-factor code (rate limited) |
| `POST /logout` | anyone | End the browser session |
| `GET /auth/providers`, `GET /auth/{provider}/start` | anyone | Single sign-on (Microsoft, Google, OpenID Connect) |
| `POST /password/forgot`, `POST /password/reset` | anyone | Email a reset link; set a new password with it |
| `GET /me`, `POST /me/password` | any role | Your account; change your password |
| `GET /me/2fa`, `POST /me/2fa/setup` · `enable` · `disable` · `recovery-codes` | any role | Two-factor authentication |
| `POST /users/{name}/password`, `POST /users/{name}/2fa/reset` | admin | Help someone who is locked out |
| `POST /users/{name}/access` | admin | Remove a person's access, or give it back |
| `GET /health`, `GET /health/ready` | anyone | Up; database reachable (for load balancers) |
| `POST /import/preview`, `POST /import` | admin, accountant | Read an Excel/CSV file and suggest a column mapping; import it |
| `POST /documents/extract` | admin, accountant | Read one invoice document (PDF, Excel form, scan) into a draft; saves nothing |
| `POST /documents/import` | admin, accountant | Save checked drafts as invoices, then run detection |
| `GET /documents/stats` | any role | Documents read, by method, and fields people corrected |
| `GET /samples/documents`, `GET /samples/invoice-documents.zip` | anyone | Sample invoice documents |
| `POST /invoices`, `POST /invoices/bulk` | admin, accountant | Add one invoice, or up to 1,000 |
| `GET /summary` | any role | Dashboard numbers |
| `POST /export/annotated` | any role | Your uploaded workbook with results written in (.xlsx) |
| `GET /export/review.xlsx` | any role | The review queue as an Excel report |
| `GET /export/payment-file.xlsx` | any role | Every invoice sorted by payment decision, with totals |
| `GET /samples`, `GET /samples/{name}` | anyone | Sample Excel files and a blank template |
| `GET /invoices` | any role | List invoices (no bank data) |
| `GET /invoices/{id}/bank` | admin | Decrypt one bank account (audited) |
| `POST /detect` | admin, accountant | Run detection and store the flags |
| `GET /review-queue` | any role | Flagged invoices ranked by risk, with reasons and status |
| `POST /review/{id}` | admin, accountant | Confirm or dismiss; optional payment action and note |
| `GET /metrics` | any role | Measured precision per rule and the weights in use |
| `GET /anomalies` | any role | Every individual flag |
| `GET`, `PUT /settings` | any role, admin | Approval threshold; require two-factor; AI reading |
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
src/invoice_detector/   detection, decisions, evaluate, demo, importer, documents, intake, isolation, ai_extract, doc_samples, excel_out, samples, api, accounts, oidc, totp, qr, mailer, cli, db, schema, crypto, security, config, explain
src/invoice_detector/web/  the web app (index.html, app.js, app.css)
tests/                  unit tests (unittest style, run by pytest); tests/e2e is the browser test
docs/TUTORIAL.md        step-by-step guide
docs/HOW-IT-WORKS.md    how the code works, in depth
docs/DEPLOYMENT.md      hosting for a company: HTTPS, PostgreSQL, secrets, backups, SSO, email
deploy/                 Caddyfile, container start-up script, backup script
docker-compose.prod.yml production stack
samples/                sample Excel files (regenerate with scripts/make_samples.py)
prototypes/             the original Gleam version
.github/workflows/      CI: lint, tests on Linux, Windows and PostgreSQL, browser test (incl. SSO, 2FA, reset email), production stack, dependency audit
```

## Docker

Quick local try-out with SQLite:

```bash
docker build -t invoice-detector .
docker run -p 8000:8000 --env-file .env -v invoice-data:/data invoice-detector
```

For a real deployment use `docker-compose.prod.yml`; see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
