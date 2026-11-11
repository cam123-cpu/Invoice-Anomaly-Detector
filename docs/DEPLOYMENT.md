# Running it for a company

This guide takes the app from "runs on a laptop" to a hosted service a finance team can use:
HTTPS, PostgreSQL with row-level security, secrets kept out of `.env` files, automatic
backups of the data **and** the keys, single sign-on, two-factor authentication and
password-reset emails.

There are two ways to host it:

- **A. One server with Docker** (`docker-compose.prod.yml`). Everything in one place:
  HTTPS certificates, PostgreSQL, the app and nightly backups. Good for a pilot or a small
  company. Tested end to end in CI on every push.
- **B. A cloud platform** (Azure, AWS or Google Cloud): the platform runs the container, a
  managed PostgreSQL service holds the data, and the cloud's secrets manager holds the keys.
  Less to look after, and backups are handled for you. The building blocks are the same; this
  guide lists the settings to use. (Not tested in CI: it needs a cloud account.)

---

## What must be kept secret, and where it goes

| Secret | What it does | If lost | If leaked |
|---|---|---|---|
| `SECRET_KEY` | Signs sign-in tokens and SSO state | Everyone has to sign in again | Attacker can forge sign-ins: replace it |
| `ENCRYPTION_KEYS` | Encrypts bank details (AES-256-GCM) | **Bank details can never be read again**, including in backups | Bank details readable by whoever also has the database |
| `INDEX_KEY` | Keyed fingerprint used to compare bank accounts | Detection can't compare accounts any more | Weakens the protection of bank details |
| `DATABASE_URL` | The web app's limited database login (`invoice_app`) | Reset it with `migrate` | Access is still limited by row-level security |
| `DATABASE_ADMIN_URL` | Owner login, used only by `migrate` and the CLI | Reset it in PostgreSQL | Full database access: replace it |
| `SMTP_PASSWORD`, `*_CLIENT_SECRET` | Email and SSO | Get new ones from the provider | Revoke at the provider |

Every setting can be given in either of two ways, and the app treats them the same:

- `NAME=value` as an environment variable (how most cloud secrets managers inject secrets), or
- `NAME_FILE=/path/to/file` pointing at a file holding the value (how Docker and Kubernetes
  secrets, and the Azure/AWS/Google secrets-store drivers, mount secrets).

A `.env` file is only for local development. In production, nothing secret is written into
the image, the compose file, or the environment listing (`docker inspect`).

### Start-up checks

When the app or any CLI command starts, it checks that the encryption keys match the
database (a small encrypted "canary" value is stored in the `meta` table the first time).
With the wrong keys it refuses to start, with a clear message, instead of quietly writing
data nobody can read later. `GET /health` says the process is up; `GET /health/ready` says
the database answers (use it for load-balancer health checks).

---

## A. One server with Docker

You need a Linux server (2 GB of memory is plenty to start) with Docker installed, a domain
name pointing at it (an `A` record, e.g. `invoices.yourcompany.com`), and ports 80 and 443
open.

### 1. Create the secrets (once)

```bash
git clone https://github.com/<you>/<repo>.git invoice-detector && cd invoice-detector
python3 -m pip install -e .            # Python 3.11+; only needed to create the secrets
python3 -m invoice_detector.cli init-deploy deploy/secrets
```

This writes one file per secret into `deploy/secrets/` (a folder only you can open, git-ignored),
with matching database passwords. It refuses to overwrite existing files, because new
encryption keys would make the existing data unreadable.

**Back up `deploy/secrets/` right now**, somewhere other than the server: a company password
manager (1Password, Bitwarden, Keeper: store each file as a secure note) or an encrypted USB
key in a safe. Do it before any real invoices go in. See [Backing up the keys](#backing-up-the-keys).

### 2. Start it

```bash
export DOMAIN=invoices.yourcompany.com
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml ps        # app should become "healthy"
```

What runs:

| Service | Image | Job |
|---|---|---|
| `caddy` | `caddy:2` | HTTPS on 443, gets and renews a Let's Encrypt certificate automatically, redirects HTTP to HTTPS |
| `app` | built from `Dockerfile` | On start, runs `migrate` (tables, row-level security, key check), then serves on port 8000, reachable only by Caddy |
| `db` | `postgres:16` | The data, on the `db-data` volume; not exposed outside the server |
| `backup` | `postgres:16` | `pg_dump` every 24 hours into `./backups/`, keeps 14 days |

The app also sends `Strict-Transport-Security`, so browsers refuse plain HTTP to your
domain from then on, and redirects any plain-HTTP request it receives to HTTPS
(`FORCE_HTTPS=1`).

### 3. First company and administrator

```bash
C="docker compose -f docker-compose.prod.yml exec app python -m invoice_detector.cli"
$C create-org "Your Company"
$C create-user "Your Company" you@yourcompany.com admin     # asks for a password
```

Sign in at `https://invoices.yourcompany.com`, open **Your account** and turn on two-factor
authentication. Then, in **Settings**, tick *Everyone must use two-factor authentication*
and add the rest of the team.

### Updating to a new version

```bash
git pull
docker compose -f docker-compose.prod.yml up -d --build app
```

The app runs `migrate` on start; it only adds what is missing, so it is safe to run every time.

---

## B. A cloud platform

The container image is the same. What changes is who runs each piece.

| Piece | Azure | AWS | Google Cloud |
|---|---|---|---|
| Run the container, HTTPS | App Service (Linux, container) or Container Apps | App Runner or ECS Fargate + Application Load Balancer | Cloud Run |
| Managed PostgreSQL | Azure Database for PostgreSQL – Flexible Server | Amazon RDS for PostgreSQL | Cloud SQL for PostgreSQL |
| Secrets manager | Key Vault (Key Vault references in app settings) | Secrets Manager (`secrets` in the task definition) | Secret Manager (`--set-secrets`) |
| Data backups | Automatic, 7–35 days, point-in-time restore | Automated backups, 1–35 days, point-in-time restore | Automated backups + point-in-time recovery |

Steps, whichever cloud:

1. **Create the database** (PostgreSQL 14 or newer), with automated backups on (the default
   on all three; set retention to at least 14 days) and access limited to your app's private
   network. Require TLS: add `?sslmode=require` to both database URLs.
2. **Generate the secrets** with `python -m invoice_detector.cli gen-keys` (prints
   `SECRET_KEY`, `ENCRYPTION_KEYS`, `INDEX_KEY`) and choose a password for `invoice_app`.
   Store each one in the secrets manager.
3. **Run `migrate` once** from a machine that can reach the database (or as a one-off job
   on the platform), with `DATABASE_ADMIN_URL` = the database's admin login,
   `APP_DB_PASSWORD` = the `invoice_app` password, plus the three keys:
   `python -m invoice_detector.cli migrate`. This creates the tables, the limited
   `invoice_app` role and the row-level-security policies.
4. **Deploy the container** with these settings, secrets coming from the secrets manager:

   ```
   DATABASE_URL       = postgresql://invoice_app:<password>@<host>:5432/<db>?sslmode=require   (secret)
   SECRET_KEY, ENCRYPTION_KEYS, INDEX_KEY                                                     (secrets)
   PUBLIC_URL         = https://invoices.yourcompany.com
   FORCE_HTTPS        = 1
   FORWARDED_ALLOW_IPS = *          # the platform's load balancer is the only way in
   API_DOCS           = 0          # no public /docs page
   ```

   Do **not** give the running app `DATABASE_ADMIN_URL`: then it can't change the schema or
   turn off row-level security even if it were compromised. (Without it, the container
   skips `migrate` on start; run step 3 again after each upgrade.)
5. Point the health check at `/health/ready`, and map your domain (the platform provides the
   HTTPS certificate).

Managed services keep the data backups for you. **They do not back up your encryption keys
outside the secrets manager**: do the next section too.

---

## Reading invoice documents

PDFs and Excel invoices are read on the server with nothing extra. Photos and scanned PDFs need
OCR: the Docker image includes Tesseract, so on option A it just works. On a cloud platform the
same image works too; if you build your own image, install `tesseract-ocr` and the `ocr` extra.

Uploaded files are read in a separate, short-lived process with a 45-second limit and (on Linux)
a 2 GB memory limit, so a broken file can't hang the app. Leave `DOC_ISOLATION` at its default.

**AI reading** (optional) sends invoices the app can't read by itself to Anthropic's API. It
needs `ANTHROPIC_API_KEY` set as a secret (`ANTHROPIC_API_KEY_FILE` works too) and an admin
to switch it on per company in Settings. Each use is in the activity log; each company is limited
to 100 an hour. Check that sending supplier invoices to Anthropic fits your company's data policy
before switching it on.

## Backups

### The data

On option A, the `backup` service writes `backups/invoices-<time>.dump` daily. Those dumps
contain bank details only in encrypted form, so they are safe to copy elsewhere, but they are
useless without the keys. **Copy them off the server** too: a disk failure would otherwise
take the database and its backups together. For example, nightly with cron:

```bash
# Azure:  az storage blob upload-batch -d backups -s ./backups --overwrite false
# AWS:    aws s3 sync ./backups s3://yourcompany-invoice-backups/
# Google: gsutil rsync ./backups gs://yourcompany-invoice-backups/
```

Useful commands:

```bash
D="docker compose -f docker-compose.prod.yml"
$D exec backup bash /backup.sh once                                   # back up now
$D exec backup bash /backup.sh restore /backups/invoices-<time>.dump  # restore
$D exec app python -m invoice_detector.cli migrate                    # always after a restore
```

`migrate` after a restore puts back the `invoice_app` permissions and row-level-security
policies, and checks the keys match the restored data.

**Test a restore** when you set this up and every few months after, onto a spare server or a
copy: an untested backup is a hope, not a backup. CI does a backup → wipe → restore → read
cycle on every push.

### Backing up the keys

The keys are deliberately **not** in the database backups: someone who steals a backup
shouldn't get the keys with it. That means you must keep them yourself.

- Keep **two copies in different places**, for example the company password manager and a
  sealed envelope or encrypted USB key in the office safe, held by two different people.
- On a cloud platform, the secrets manager keeps them, but also export them once into the
  password manager: deleting the vault or losing access to the cloud account must not
  lose them. Turn on soft-delete / deletion protection for the vault or secret.
- Whenever you add an encryption key (below), update both copies before deploying it.

To check you can recover: start a fresh test copy from the latest backup using only the keys
from the password manager, then run `migrate`. If the keys are wrong it says so immediately.

### Adding a new encryption key

`ENCRYPTION_KEYS` holds numbered keys, `1:<key>,2:<key>`. New data is encrypted with the
highest number; older values stay readable as long as their key stays in the list. To start
using a fresh key, generate one (`python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"`),
append it as the next number, back it up, then deploy. Never remove an old key while data
encrypted with it still exists (including in backups you might restore).

---

## Signing in

### Two-factor authentication

Built in, nothing to configure. Each person turns it on in **Your account** with any
authenticator app (Microsoft Authenticator, Google Authenticator, 1Password, Authy…) and gets
ten one-time recovery codes. An admin can require it for the whole company in **Settings**;
people without it are then sent to set it up before they can do anything else. If someone
loses their phone and their recovery codes, an admin can turn their two-factor off
(Settings → people → *Turn off two-factor*) so they can set it up again.

People who sign in with Microsoft or Google get their second factor from that account
(set it up there, in Entra ID or Google Workspace), so the app doesn't ask again.

### Single sign-on with Microsoft (Entra ID / Office 365)

1. In the Azure portal: **Microsoft Entra ID → App registrations → New registration**.
   - Name: *Invoice Review*. Supported accounts: *this organizational directory only*.
   - Redirect URI: platform **Web**, `https://invoices.yourcompany.com/auth/microsoft/callback`
2. Copy the **Application (client) ID** and **Directory (tenant) ID**.
3. **Certificates & secrets → New client secret**; copy the value (it is shown once; note
   the expiry date and renew it before then).
4. Set on the app (the secret through the secrets manager):
   `MICROSOFT_CLIENT_ID`, `MICROSOFT_TENANT_ID`, `MICROSOFT_CLIENT_SECRET`.

The tenant ID must be your company's own: the app refuses `common`, `organizations` and
`consumers`, so only accounts from your directory can sign in.

### Single sign-on with Google Workspace

1. In Google Cloud console: **APIs & Services → OAuth consent screen**: user type
   **Internal** (only your Workspace accounts).
2. **Credentials → Create credentials → OAuth client ID**, type *Web application*,
   authorized redirect URI `https://invoices.yourcompany.com/auth/google/callback`.
3. Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.

### Any other OpenID Connect provider (Okta, Auth0, Keycloak, OneLogin…)

Set `OIDC_ISSUER` (e.g. `https://yourcompany.okta.com`), `OIDC_CLIENT_ID`,
`OIDC_CLIENT_SECRET`, and optionally `OIDC_LABEL` (button text). Redirect URI:
`https://invoices.yourcompany.com/auth/sso/callback`.

### Who can sign in with SSO

SSO signs a person in only if an admin has already added them, with their work email as the
username (Settings → *Add a person*). Tick *Single sign-on only* to create them with no
password at all. Someone who isn't on the list is turned away even if their Microsoft or
Google sign-in succeeds; removing them from your directory also stops them signing in here.

### Password-reset emails

For people who sign in with a password, *Forgot your password?* emails a link valid for
30 minutes (only to usernames that are email addresses). Configure any SMTP service, for
example Microsoft 365, Google Workspace, SendGrid, Amazon SES or Mailgun:

```
SMTP_HOST=smtp.office365.com   SMTP_PORT=587   SMTP_SECURITY=starttls   # or ssl (port 465)
SMTP_USER=invoices@yourcompany.com   SMTP_PASSWORD=...   (secret)
MAIL_FROM=Invoice Review <invoices@yourcompany.com>
```

`PUBLIC_URL` must be set: it's the address used in the link. Without email configured, the
page tells people to ask an admin, who can set a new password in Settings or with
`cli reset-password`. The response is the same whether or not the username exists, so the
form can't be used to find out who has an account.

---

## Checklist before real invoices go in

- [ ] Site opens on `https://`, and `http://` redirects to it
- [ ] Secrets stored in a secrets manager or `deploy/secrets/` (not `.env`), and **keys backed up in two places**
- [ ] Data backups running, copied off the server, and **one restore tested**
- [ ] Admin account has two-factor on; *Everyone must use two-factor* ticked
- [ ] SSO tested with one real account, or password-reset email tested
- [ ] Each person added with their own account and the right role (admin / accountant / auditor)
- [ ] Someone named as responsible for updates, renewing the SSO client secret, and checking backups
