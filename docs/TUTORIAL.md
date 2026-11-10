# Tutorial: run it and use it

Part 1 gets you running on one computer in about ten minutes, using the web app. Part 2 is the production
setup with PostgreSQL. Part 3 is how a company fits it into its payment process.

Commands are shown for Windows PowerShell. On macOS or Linux, activate the environment with
`source .venv/bin/activate` instead, and the rest is the same.

---

## Part 1: run it on your computer

### 1. Get the code and install it

You need Python 3.11, 3.12 or 3.13. Open a normal PowerShell window (not "as administrator").

```powershell
cd $HOME\Downloads
Invoke-WebRequest https://github.com/cam123-cpu/Invoice-Anomaly-Detector/archive/refs/heads/main.zip -OutFile invoice-app.zip
Expand-Archive invoice-app.zip -DestinationPath invoice-app
cd invoice-app\Invoice-Anomaly-Detector-main

py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest
```

All tests should pass. Some are reported as skipped: those need PostgreSQL (Part 2) or the browser test setup.

> If `Expand-Archive` says the folder already exists, you have an older copy. Pick a new name,
> for example `-DestinationPath invoice-app-2`, and `cd` into that instead. Starting fresh
> avoids reusing an old `invoices.db`, which earlier versions created without companies.

### 2. Create your secrets, a company and a login

Run these **one at a time**. `create-user` asks for a password of at least 12 characters, and
nothing appears while you type, which is normal.

```powershell
python -m invoice_detector.cli init
python -m invoice_detector.cli create-org "Acme Inc"
python -m invoice_detector.cli create-user "Acme Inc" alice admin
```

Want sample data to explore? Also run `python -m invoice_detector.cli seed "Acme Inc"`, which
adds 258 made-up invoices with 14 planted problems.

### 3. Start the app

```powershell
uvicorn invoice_detector.api:create_app --factory --reload
```

Wait for `Uvicorn running on http://127.0.0.1:8000`, then open **http://127.0.0.1:8000** in your
browser. Leave the terminal window open; press Ctrl+C there to stop the app.

### 4. Sign in

Enter `alice` and the password you chose. The left-hand menu has everything:

| Page | What it's for |
|---|---|
| **Dashboard** | How many invoices you have, how many need review, how many are high risk, and how accurate the flags have been |
| **Review queue** | Flagged invoices, highest risk first, with the reasons. This is where the work happens |
| **Upload invoices** | Add invoices from an Excel or CSV export |
| **Invoices** | Everything stored. Admins can reveal a bank account, and each reveal is logged |
| **Activity log** | Who signed in, imported, reviewed or viewed bank details (admins and auditors) |
| **Settings** | Your company's approval threshold, and adding colleagues (admins) |

### 5. Upload a file

1. Export invoices from your accounting system or save your spreadsheet as **.xlsx** or **.csv**.
   One row per invoice, with at least an invoice number, vendor, amount and date. A bank account
   column is optional but switches on the bank checks.
2. Open **Upload invoices** and drop the file on the page.
3. **Match the columns.** The app guesses which of your columns is which, from names like
   "Invoice #", "Supplier", "Invoice Date" or "Total". Check each one and fix any wrong guess.
4. **Check the date format.** If your dates look like 03/05/2026, say whether that means
   March 5 or 3 May. The app works it out when the file settles it, and warns you when it can't.
5. Click **Import invoices**. You'll see how many were added, how many were already stored, and
   every row that couldn't be used, with the reason. Detection runs automatically afterwards.

The app handles what real exports look like: title rows above the header, amounts like
`$1,250.00` or `1.250,00 €`, Excel dates, blank lines and totals rows. It remembers your column
matching, so next month's export from the same system needs no changes. Re-uploading a file is
safe, because invoices already stored are skipped.

### 6. Work the review queue

Each row shows a **risk score** from 0 to 100, the invoice, and coloured tags saying why it was
flagged. Click a row for the full explanation, then decide:

- **Confirm** if it's a real problem, such as a duplicate payment or a changed bank account you
  didn't expect.
- **Dismiss** if it's a false alarm.

Decided invoices leave the **Open** tab; the other tabs show them again, and you can change your
mind. Your decisions teach the app: checks that keep raising false alarms at your company count
for less from then on, and the Dashboard shows how each check is performing.

**Run detection** at the top of the queue re-checks everything, for example after you change the
approval threshold in Settings.

### 7. Add colleagues

In **Settings**, an admin adds people with a temporary password and one of three roles:

| Role | Can |
|---|---|
| Auditor | See everything except bank accounts. Can't change anything |
| Accountant | Also upload, run detection and review flags |
| Admin | Also manage people and settings, and reveal bank accounts |

### 8. Add a second company

Each company's data is completely separate. To see it for yourself:

```powershell
python -m invoice_detector.cli create-org "Globex"
python -m invoice_detector.cli create-user "Globex" bob admin
```

Sign out, sign in as `bob`, and Globex starts empty. Nothing from Acme is visible, even by
typing an invoice address directly.

### 9. Without the browser

The same steps work from the command line, which is handy for scheduled jobs:

```powershell
python -m invoice_detector.cli import "Acme Inc" march-invoices.xlsx
python -m invoice_detector.cli detect "Acme Inc"
```

The raw API, for connecting other systems, is documented at http://127.0.0.1:8000/docs.

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

`create-org`, `create-user`, `uvicorn` and the web app all work the same way.

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

1. **Feed it.** Upload each week's or month's invoice export on the Upload page. For automatic
   feeds, a scheduled job can run `cli import`, or another system can send invoices to
   `POST /invoices/bulk`.
2. **Set the approval threshold** in Settings to match the company's sign-off policy.
3. **Run it in shadow mode for a few weeks.** Have accounts-payable staff work the review queue
   after each upload, without holding any payments yet.
4. **Watch the Dashboard.** "How each check is performing" shows which checks are right for this
   company and which are noise. The risk scores adjust on their own as reviews accumulate.
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
| `Password too short` straight away | Commands were pasted as a block, so the next line was taken as the password. Run `create-user` on its own |
| The page won't open | The `uvicorn` window must stay open while you use the app. Start it again |
| "Your session expired" | Sign-ins last one hour. Sign in again |
| "Too many attempts" on sign-in | Five wrong passwords. Wait 15 minutes or restart the app |
| An upload says ".xls isn't supported" | In Excel, use File > Save As and choose .xlsx or .csv |
| A package fails to build during install | Use Python 3.11 to 3.13. The newest Python release may not have builds for every library yet |
