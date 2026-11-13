# Tutorial: run it and use it

Part 1 gets you running on one computer in about ten minutes, using the web app. Part 2 is the production
setup with PostgreSQL. Part 3 is how a company fits it into its payment process.

Commands are shown for Windows PowerShell. On macOS or Linux, activate the environment with
`source .venv/bin/activate` instead, and the rest is the same.

---

> **Windows shortcut:** right-click `setup.ps1` > *Run with PowerShell*, then `start.ps1`.
> They do steps 1-4 below for you and work on laptops that block `pip.exe`.

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
python -m pip install -e ".[dev]"
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
python -m uvicorn invoice_detector.api:create_app --factory --reload
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
   *No file to hand?* The Upload page has four samples with **Try it** buttons, and a blank template.
2. Open **Upload invoices** and drop the file on the page.
3. **Pick the sheet**, if the workbook has several with invoices in them.
4. **Match the columns.** The app guesses which of your columns is which, from names like
   "Invoice #", "Supplier", "Invoice Date" or "Total". Check each one and fix any wrong guess.
5. **Check the date format.** If your dates look like 03/05/2026, say whether that means
   March 5 or 3 May. The app works it out when the file settles it, and warns you when it can't.
6. Click **Import invoices**. You'll see how many were added, how many were already stored,
   how many from this file were flagged, and every row that couldn't be used, with the reason.
   Detection runs automatically.

The app handles what real exports look like: title rows above the header, amounts like
`$1,250.00` or `1.250,00 €`, Excel dates, blank lines and totals rows. It remembers your column
matching, so next month's export from the same system needs no changes. Re-uploading a file is
safe, because invoices already stored are skipped. For a workbook with one sheet per month or
quarter, use the **Import sheet "..."** button on the results page to work through them in order.

### 6. Get the results back in Excel

On the results page, click **Download your file with flags**. You get your own workbook back,
named "... - checked.xlsx", with:

- six new columns on the right of your data: **Risk** (0-100), **Review status**, **Payment
  decision**, **Flags**, **Why flagged** and **What to do**, filled in for every row, with
  high-risk rows shaded red and medium amber;
- "No flags" in green for rows that passed, and "Not imported" with the reason for rows that
  couldn't be read;
- a **Flagged invoices** sheet listing just the problems, highest risk first, with filters;
- an **About these flags** sheet explaining every flag.

Your own cells and formulas are not changed. Pictures and charts in the workbook are not copied
into the download, so keep your original. Download it again after reviewing, and the Review
status column shows your team's decisions.

For a report of everything flagged so far, across all your uploads, use **Download Excel** on
the Review queue page or **Download Excel report** on the Dashboard.

### 6b. Or upload the invoices themselves

Not every invoice arrives in an export. On **Upload invoices**, choose **Invoice documents (PDF,
Excel, scans)** and drop in invoice files: PDFs, Excel invoices made from a template, or photos and
scans. Several at once is fine.

To try it, click **Read the sample invoices** (load the sample data first so the checks have a
history to compare with). You'll see a table with one row per file:

- **Amber** boxes are values the reader wasn't sure of; **red** ones it couldn't find. Hover over
  a box to see the text it was read from, and correct anything that's wrong.
- Notes under a row explain anything worth checking: totals that don't add up, a vendor spelled
  almost like one you already have, payment to a person rather than the business.
- Untick a row to leave that file out. Nothing is saved until you click **Add invoices**.

- Under each file name is its **type**: Invoice or Credit note. A credit note is recognised by
  its title; change the box if the reader got it wrong. For a credit note, a second box says
  which invoice it corrects, when the reader found it.
- E-invoices (an .xml file, or a PDF with the data inside) say *E-invoice data* under the file
  name: every value comes from the data, not from reading the page.
- If you correct a field, the app remembers where that vendor prints it, and reads their next
  invoice there.

The results show which of the new invoices were flagged and why. With the samples you'll see a
new bank account (Stark Freight), a look-alike vendor (Acme Suplies), totals that don't add up
(Umbrella Ltd), and a Globex invoice with the credit note that cancels it: the two are flagged
as a pair so the invoice isn't paid. In a list, a negative amount or a Type column saying
"credit" does the same. Upload the same file again and it is recognised and not added twice.

Photos and scans need the optional OCR extra (see the README). If your company switches on **AI
reading** in Settings (it needs an Anthropic API key on the server), files the app can't read by
itself are sent to Claude instead; each one is noted in the activity log.

### 7. Work the review queue

Each row shows a **risk score** from 0 to 100, the invoice, and coloured tags saying why it was
flagged. Click a row to see **why it was flagged** and **what to do**, then decide:

- **Confirm** if it's a real problem. Confirming also decides what happens to the payment:
  - **Do not pay** for duplicates (the money would be paid twice);
  - **Hold** for anything that needs checking first, such as a changed bank account or an
    unusual amount.
- **Dismiss** if it's a false alarm. The invoice goes back to **Pay**.

After confirming, the row stays open with a **Payment decision** box: change it (for example to
*Pay anyway* once the vendor has confirmed their new bank details) and add a note, then click
**Save decision**.

The app never edits or deletes your invoices. They stay exactly as the supplier sent them,
which auditors need. "Fixing" a problem means stopping or holding the payment and doing the
follow-up the **What to do** list describes, such as calling the vendor on a number you already
had.

Decided invoices leave the **Open** tab; the other tabs show them again, and you can change your
mind. Your decisions also teach the app: checks that keep raising false alarms at your company
count for less from then on.

**Run detection** at the top of the queue re-checks everything, for example after you change the
approval threshold in Settings.

### 7b. Second approval and separation of duties

Once your company has two or more admins or accountants (step 9), two rules apply:

- You can't review an invoice you added yourself; a colleague does it.
- Releasing a flagged invoice of 10,000 or more (change the amount in **Settings**) needs a
  second person. After the first review it shows **Needs 2nd approval**; a colleague who
  neither reviewed nor added it clicks **Approve**.

### 7c. Mark invoices paid

On **Invoices**, the *Payment* column shows each invoice's decision. When it says **Pay**,
**Mark paid** records the payment. Paid invoices drop out of the payment file (they're listed
on its *Already paid* sheet), and if the same invoice arrives again, the flag says the first
one was already paid. An admin can **Undo** a payment marked by mistake.

### 8. Get the corrected payment list

When reviewing is done, click **Download payment file** (on the Review queue or the Dashboard).
It's an Excel workbook that sorts **every** invoice by what should happen to its payment:

| Sheet | What's in it |
|---|---|
| Summary | Count and total amount for each decision |
| Ready to pay | No problems, or flags dismissed as false alarms |
| Review first | Flagged but not reviewed yet: finish these before the payment run |
| On hold | Confirmed problems to check first, with what to do and your notes |
| Do not pay | Confirmed problems such as duplicates |

Hand the **Ready to pay** sheet to whoever runs payments. The Dashboard's **Payments** section
shows the same totals, including how much money was stopped.

Your own workbook (step 6) also gets **Payment decision** and **What to do** columns, so
downloading it again after reviewing shows every row's outcome next to your original data.

### 9. Add colleagues

In **Settings**, an admin adds people with a temporary password and one of three roles:

| Role | Can |
|---|---|
| Auditor | See everything except bank accounts. Can't change anything |
| Accountant | Also upload, run detection and review flags |
| Admin | Also manage people and settings, and reveal bank accounts |

### 10. Add a second company

Each company's data is completely separate. To see it for yourself:

```powershell
python -m invoice_detector.cli create-org "Globex"
python -m invoice_detector.cli create-user "Globex" bob admin
```

Sign out, sign in as `bob`, and Globex starts empty. Nothing from Acme is visible, even by
typing an invoice address directly.

### 11. Without the browser

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

### 7. Putting it on a server

[DEPLOYMENT.md](DEPLOYMENT.md) is the full guide. In short, on a Linux server with Docker and
a domain name pointing at it:

```bash
python3 -m invoice_detector.cli init-deploy deploy/secrets   # then back this folder up!
DOMAIN=invoices.yourcompany.com docker compose -f docker-compose.prod.yml up -d --build
```

That gives you HTTPS with an automatic certificate, PostgreSQL with row-level security,
secrets read from files instead of `.env`, and a database backup every night. It runs one web
server; to run several, the sign-in rate limiter would need to move to a shared store such as
Redis first.

### 8. Sign-in for a finance team

Try these on your own computer first; they work the same once hosted.

1. **Two-factor:** open **Your account → Turn on two-factor**, scan the QR code with an
   authenticator app (Microsoft Authenticator, Google Authenticator, 1Password…), type the
   6-digit code, and **save the recovery codes** it shows you. Sign out and back in: after your
   password it asks for a code.
2. **Require it for everyone:** **Settings → Everyone must use two-factor authentication**.
   (You have to have it on yourself first, so you can't lock yourself out.)
3. **Password reset by email:** for a test, add `MAIL_DIR=outbox` to `.env` and restart. Add a
   person whose username is an email address, click *Forgot your password?* on the sign-in
   page, and the email appears as a `.eml` file in `outbox/`. In production you set `SMTP_*`
   instead.
4. **Microsoft or Google sign-in:** needs an app registration with Microsoft or Google and a
   public `https://` address, so it is set up when hosting; see
   [DEPLOYMENT.md](DEPLOYMENT.md#single-sign-on-with-microsoft-entra-id--office-365).

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
| Forgot a password | `python -m invoice_detector.cli reset-password alice` (nothing shows while you type the new one) |
| `The encryption keys don't match this database` | The `.env` (or secrets) were replaced. Put back the keys the database was created with, or for a throwaway test database delete `invoices.db` |
| Lost the phone with the authenticator app | Sign in with a recovery code, or ask an admin: Settings → *Turn off two-factor* for you |
| Two-factor code always "not right" | The phone's clock is off. Turn on automatic date and time on the phone |
| *Forgot your password?* sends nothing | Email isn't set up (`SMTP_*` or `MAIL_DIR`), or the username isn't an email address. An admin can reset it in Settings |
| "Username ... is already taken" | That login already exists. Sign in with it, or reset its password as above |
| An upload says ".xls isn't supported" | In Excel, use File > Save As and choose .xlsx or .csv |
| "This PDF is a scan" or "Reading photos and scans needs OCR" | Install the OCR extra and Tesseract (README, Quick start), or ask an admin to switch on AI reading |
| "This looks like a single invoice rather than a list" | Click **Read it as an invoice document** |
| A package fails to build during install | Use Python 3.11 to 3.13. The newest Python release may not have builds for every library yet |
