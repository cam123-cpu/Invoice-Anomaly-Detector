"""Database schema for SQLite (local, single server) and PostgreSQL (production).

Every table carries org_id. On PostgreSQL, row-level security (RLS) makes the database
itself refuse to return or accept another company's rows, even if application code
forgets a WHERE clause.
"""

APP_ROLE = "invoice_app"  # the limited role the web server connects as

_TABLES = """
CREATE TABLE IF NOT EXISTS orgs (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  approval_threshold {REAL} NOT NULL DEFAULT 10000 CHECK (approval_threshold > 0)
);
CREATE TABLE IF NOT EXISTS users (
  username TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES orgs(id),
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('admin', 'accountant', 'auditor'))
);
CREATE TABLE IF NOT EXISTS invoices (
  org_id TEXT NOT NULL REFERENCES orgs(id),
  id TEXT NOT NULL,
  vendor TEXT NOT NULL,
  amount {REAL} NOT NULL CHECK (amount > 0),
  invoice_date TEXT NOT NULL,
  bank_enc TEXT,
  bank_idx TEXT,
  created_by TEXT NOT NULL,
  PRIMARY KEY (org_id, id)
);
CREATE TABLE IF NOT EXISTS anomalies (
  org_id TEXT NOT NULL,
  invoice_id TEXT NOT NULL,
  rule TEXT NOT NULL,
  severity TEXT NOT NULL,
  detail TEXT NOT NULL,
  PRIMARY KEY (org_id, invoice_id, rule),
  FOREIGN KEY (org_id, invoice_id) REFERENCES invoices(org_id, id)
);
CREATE TABLE IF NOT EXISTS reviews (
  org_id TEXT NOT NULL,
  invoice_id TEXT NOT NULL,
  verdict TEXT NOT NULL CHECK (verdict IN ('confirmed', 'dismissed')),
  action TEXT CHECK (action IN ('pay', 'hold', 'do_not_pay')),
  note TEXT,
  rules TEXT NOT NULL,
  reviewer TEXT NOT NULL,
  reviewed_at TEXT NOT NULL,
  PRIMARY KEY (org_id, invoice_id),
  FOREIGN KEY (org_id, invoice_id) REFERENCES invoices(org_id, id)
);
CREATE TABLE IF NOT EXISTS audit_log (
  id {AUTO_ID},
  org_id TEXT NOT NULL REFERENCES orgs(id),
  at TEXT NOT NULL,
  username TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT NOT NULL
);
"""

SQLITE_SCHEMA = (_TABLES.replace("{REAL}", "REAL")
                 .replace("{AUTO_ID}", "INTEGER PRIMARY KEY AUTOINCREMENT"))

POSTGRES_TABLES = (_TABLES.replace("{REAL}", "DOUBLE PRECISION")
                   .replace("{AUTO_ID}", "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY"))

# Columns added after the first release. Each entry: (table, column, definition).
# Databases created by older versions get them on start-up (SQLite) or `migrate` (PostgreSQL).
ADDED_COLUMNS = [
    ("reviews", "action", "TEXT CHECK (action IN ('pay', 'hold', 'do_not_pay'))"),
    ("reviews", "note", "TEXT"),
]

# Idempotent: safe to run on every deploy.
POSTGRES_SECURITY = """
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'invoice_app') THEN
    CREATE ROLE invoice_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  END IF;
END $$;

-- The current company, set by the app at the start of every transaction.
CREATE OR REPLACE FUNCTION app_org_id() RETURNS text
LANGUAGE sql STABLE AS $$ SELECT nullif(current_setting('app.org_id', true), '') $$;

-- Login must find a user before the company is known. This one narrow function
-- runs with the owner's rights; nothing else can read across companies.
CREATE OR REPLACE FUNCTION auth_lookup(p_username text)
RETURNS TABLE (username text, password_hash text, role text, org_id text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT u.username, u.password_hash, u.role, u.org_id FROM users u WHERE u.username = p_username
$$;
REVOKE ALL ON FUNCTION auth_lookup(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth_lookup(text) TO invoice_app;
GRANT EXECUTE ON FUNCTION app_org_id() TO invoice_app;

-- Least privilege: the app cannot create companies, cannot edit or delete invoices,
-- and cannot change or delete audit records.
REVOKE ALL ON orgs, users, invoices, anomalies, reviews, audit_log FROM PUBLIC, invoice_app;
GRANT SELECT ON orgs TO invoice_app;
GRANT UPDATE (approval_threshold) ON orgs TO invoice_app;
GRANT SELECT, INSERT ON users TO invoice_app;
GRANT SELECT, INSERT ON invoices TO invoice_app;
GRANT SELECT, INSERT, DELETE ON anomalies TO invoice_app;
GRANT SELECT, INSERT, UPDATE ON reviews TO invoice_app;
GRANT SELECT, INSERT ON audit_log TO invoice_app;

ALTER TABLE orgs      ENABLE ROW LEVEL SECURITY;
ALTER TABLE users     ENABLE ROW LEVEL SECURITY;
ALTER TABLE invoices  ENABLE ROW LEVEL SECURITY;
ALTER TABLE anomalies ENABLE ROW LEVEL SECURITY;
ALTER TABLE reviews   ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
-- With RLS on, a row is visible or writable only if a policy allows it. With no
-- company set, app_org_id() is NULL, nothing matches, and every table looks empty.

DROP POLICY IF EXISTS tenant_isolation ON orgs;
CREATE POLICY tenant_isolation ON orgs
  USING (id = app_org_id()) WITH CHECK (id = app_org_id());
DROP POLICY IF EXISTS tenant_isolation ON users;
CREATE POLICY tenant_isolation ON users
  USING (org_id = app_org_id()) WITH CHECK (org_id = app_org_id());
DROP POLICY IF EXISTS tenant_isolation ON invoices;
CREATE POLICY tenant_isolation ON invoices
  USING (org_id = app_org_id()) WITH CHECK (org_id = app_org_id());
DROP POLICY IF EXISTS tenant_isolation ON anomalies;
CREATE POLICY tenant_isolation ON anomalies
  USING (org_id = app_org_id()) WITH CHECK (org_id = app_org_id());
DROP POLICY IF EXISTS tenant_isolation ON reviews;
CREATE POLICY tenant_isolation ON reviews
  USING (org_id = app_org_id()) WITH CHECK (org_id = app_org_id());
DROP POLICY IF EXISTS tenant_isolation ON audit_log;
CREATE POLICY tenant_isolation ON audit_log
  USING (org_id = app_org_id()) WITH CHECK (org_id = app_org_id());
"""
