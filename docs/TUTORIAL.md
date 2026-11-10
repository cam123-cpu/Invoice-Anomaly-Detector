# Tutorial: run it and use it

Part 1 gets you running on one computer in about ten minutes. Part 2 is the production
setup with PostgreSQL. Part 3 is how a company fits it into its payment process.

Commands are shown for Windows PowerShell. On macOS or Linux, activate the environment with
`source .venv/bin/activate` instead, and the rest is the same.

---

## Part 1: run it on your computer

### 1. Get the code and install it

You need Python 3.11, 3.12 or 3.13. Open a normal PowerShell window (not "as administrator").

```powershell
cd $HOME\Downloads
Invoke-WebRequest https://github.com/cam123-cpu/Invoice-Anomaly-Detector/archive/refs/heads/main.zip -OutFile iad.zip
Expand-Archive iad.zip -DestinationPath . -Force
cd Invoice-Anomaly-Detector-main

py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest
```

All tests should pass. A few are reported as skipped: those need PostgreSQL and run in Part 2.

> Upgrading from an earlier copy? Start in a fresh folder as above. The old `invoices.db`
> has no companies in it and cannot be reused.

### 2. Create your secrets, two companies and their users

```powershell
python -m invoice_detector.cli init

python -m invoice_detector.cli create-org "Acme Inc"
python -m invoice_detector.cli create-org "Globex"

python -m invoice_detector.cli create-user "Acme Inc" alice admin
python -m invoice_detector.cli create-user "Globex" bob admin
```

Each `create-user` asks for a password of at least 12 characters. Typing stays invisible.
Usernames are unique across all companies, so in real use make them email addresses.

### 3. Load demo data and run detection

```powershell
python -m invoice_detector.cli seed "Acme Inc"
python -m invoice_detector.cli detect "Acme Inc"
```

You get a ranked list like this. The 14 invoices from INV-0245 up are the planted anomalies.

```
INV-0249     risk  94  bank_account_change, shared_bank_account
INV-0247     risk  84  amount_outlier, ml_outlier
INV-0245     risk  80  duplicate
```

Globex has no invoices yet. That is the point: the two companies share nothing.

### 4. Start the web interface

```powershell
uvicorn invoice_detector.api:create_app --factory --reload
```

Open http://127.0.0.1:8000/docs. Leave the terminal running, and press Ctrl+C there to stop.

### 5. Log in

1. Open **POST /login**, click **Try it out**, enter `alice` and her password, then **Execute**.
2. Copy the long `access_token` value from the response, without the quotes.
3. Click **Authorize** at the top right, paste it, click **Authorize**, then **Close**.
4. Run **GET /me**. It shows your username, role and company.

The token lasts one hour. After that you get a 401 and log in again.

### 6. Work the review queue

| Step | Endpoint | What to look for |
|---|---|---|
| Run the checks | `POST /detect` | Counts of invoices checked and flagged |
| See the queue | `GET /review-queue` | Invoices ranked by `risk`, with `rules`, `details` and `status` |
| Decide | `POST /review/{invoice_id}` | Body `{"verdict": "confirmed"}` for a real problem, or `"dismissed"` for a false alarm |
| See the effect | `GET /review-queue` | Dismissed invoices drop out. Add `include_dismissed=true` to see them |
| Measure | `GET /metrics` | Precision per rule from your decisions, and each rule's current `weight` |

Try it: dismiss INV-0255 (a round amount) and INV-0256 (a weekend date), then confirm INV-0245.
`GET /metrics` now shows `round_amount` and `weekend_invoice` with a lower `weight` than their
`base_weight`. The more a rule's flags are dismissed at your company, the less it counts
towards risk. It takes several reviews to move a weight much, by design.

### 7. See that companies are isolated

1. Click **Authorize**, then **Logout**.
2. Log in as `bob` and authorize with his token.
3. `GET /invoices` and `GET /review-queue` return empty lists.
4. `GET /invoices/INV-0245/bank` returns 404. Bob cannot reach Acme's data even by exact id.

### 8. Everything else on the page

| Endpoint | Who | Use |
|---|---|---|
| `POST /invoices` | admin, accountant | Add one invoice |
| `POST /invoices/bulk` | admin, accountant | Add up to 1,000 at once. Existing ids are skipped |
| `GET /invoices` | everyone | List invoices. Bank details are never included |
| `GET /invoices/{id}/bank` | admin | Decrypt one bank account. Each use is written to the audit log |
| `GET /anomalies` | everyone | Every individual flag |
| `POST /explain` | everyone | Plain-English summary of the top flags |
| `GET` / `PUT /settings` | everyone / admin | Your company's approval threshold, used by two of the rules |
| `GET` / `POST /users` | admin | List or add users in your own company |
| `GET /audit` | admin, auditor | Who logged in, imported, ran detection, reviewed or viewed bank data |

Roles: an **auditor** can only read. An **accountant** can also add invoices, run detection and
review. An **admin** can also manage users and settings and view bank accounts.

### 9. Use your own invoices

Make a CSV with these columns. The date must be `yyyy-mm-dd`, and `bank_account` is optional.

```csv
id,vendor,amount,date,bank_account
INV-1001,Acme Supplies,480.25,2026-03-02,DE89370400440532013000
```

```powershell
python -m invoice_detector.cli import-csv "Globex" invoices.csv
python -m invoice_detector.cli detect "Globex"
```

Rows with a bad amount or date, and ids that are already stored, are skipped and counted.

---

## Part 2: production setup with PostgreSQL

Part 1 stores everything in one SQLite file and keeps companies apart in application code.
On PostgreSQL the database enforces the boundary as well, using row-level security: the web
server connects as a limited role, `invoice_app`, which can only ever see the rows of the
company set for the current transaction.

> The code path below runs in this project's CI against a real PostgreSQL 16 server on every
> push. The `docker-compose.yml` file itself has not been run by the author.

### 1. Start a database

Use a managed PostgreSQL (AWS RDS, Google Cloud SQL, Azure, Supabase, Neon), or locally with
Docker Desktop:

```powershell
docker compose up -d
pip install -e ".[dev,postgres]"
```

### 2. Add three lines to `.env`

```
DATABASE_ADMIN_URL=postgresql://postgres:local-owner-password@localhost:5432/invoices
DATABASE_URL=postgresql://invoice_app:choose-an-app-password@localhost:5432/invoices
APP_DB_PASSWORD=choose-an-app-password
```

### 3. Create the tables and security policies

```powershell
python -m invoice_detector.cli migrate
```

This is safe to re-run on every deploy. It creates the tables, the `invoice_app` role with the
password above, its permissions, and the row-level-security policies.

### 4. Carry on exactly as in Part 1

`create-org`, `create-user`, `seed`, `detect` and `uvicorn` all work the same way.

### 5. Prove the isolation yourself

```powershell
$env:TEST_DATABASE_ADMIN_URL = "postgresql://postgres:local-owner-password@localhost:5432/invoices"
$env:TEST_DATABASE_URL = "postgresql://invoice_app:choose-an-app-password@localhost:5432/invoices"
pytest tests/test_tenancy.py -v
```

**Warning: the tests empty every table. Point them at a scratch database, never at real data.**

`PostgresRowLevelSecurityTests` send raw SQL with no company filter, as the app's own database
role, and check that PostgreSQL returns only the current company's rows, refuses to write
into another company, and refuses to let the role switch the protection off.

### 6. Who holds which credential

| Credential | Where it lives | What it can do |
|---|---|---|
| `DATABASE_URL` (`invoice_app`) | The web server | Read and write one company at a time. Cannot create companies, edit or delete invoices, or alter the audit log |
| `DATABASE_ADMIN_URL` (owner) | An operator's machine or deploy job only | Migrate, create companies, bypass row-level security |
| `SECRET_KEY`, `ENCRYPTION_KEYS`, `INDEX_KEY` | A secrets manager | Sign logins and decrypt bank accounts. Losing them makes bank data unreadable |

### 7. Production checklist

- Serve over HTTPS behind a reverse proxy, and start uvicorn with `--proxy-headers`.
- Keep all secrets in a secrets manager, not a `.env` file on disk.
- Back up the database and, separately, the encryption keys.
- Run one web server, or move the login rate limiter to Redis before running several.
- Add single sign-on and multi-factor authentication before giving it to a finance team.

---

## Part 3: fitting it into a company's process

1. **Feed it.** Export new invoices from the ERP or accounting system each night and load them
   with `import-csv`, or have the ERP send them to `POST /invoices/bulk`.
2. **Set the approval threshold** with `PUT /settings` to match the company's sign-off policy.
3. **Run it in shadow mode for a few weeks.** Run `POST /detect` daily and have accounts-payable
   staff review the queue, without holding any payments yet.
4. **Read `GET /metrics`.** It shows which rules are right for this company and which are noise.
   The risk scores adjust on their own as reviews accumulate.
5. **Go live.** Before each payment run, run detection and hold anything open in the queue
   above a risk level the team agrees on, until someone has reviewed it.
6. **Keep people deciding.** A flag means "look at this", never "this is fraud". Nothing in this
   tool rejects or blocks an invoice by itself.

To automate steps 1 and 3, a script logs in once and reuses the token:

```python
import httpx

api = httpx.Client(base_url="https://invoices.example.com")
token = api.post("/login", json={"username": "erp-bot", "password": PASSWORD}).json()["access_token"]
api.headers["Authorization"] = f"Bearer {token}"

api.post("/invoices/bulk", json=todays_invoices)
api.post("/detect")
for item in api.get("/review-queue").json():
    if item["status"] == "open" and item["risk"] >= 50:
        print(item["invoice_id"], item["risk"], item["rules"])
```

## Troubleshooting

| Message | Fix |
|---|---|
| `No module named 'invoice_detector'` | Activate the environment, then run `pip install -e ".[dev]"` in the project folder |
| `Missing required environment variable` | Run `python -m invoice_detector.cli init` in the project folder |
| `This database file is from an older version` | Delete `invoices.db`, then redo steps 2 and 3 of Part 1 |
| `No company named ...` | Create it with `create-org`, and use quotes if the name has spaces |
| 401 on every request | The token expired or was not pasted into **Authorize**. Log in again |
| 429 on login | Five wrong passwords. Wait 15 minutes or restart the server |
| A package fails to build during install | Use Python 3.11 to 3.13. The newest Python release may not have builds for every library yet |
