-- PostgreSQL row-level security for the invoice system.
-- Run as the database owner. Works on plain Postgres, Supabase and Neon.
-- Not tested against a live database here; try it on a scratch DB first.

-- 1. A limited app role. It must NOT be a superuser and must NOT have BYPASSRLS.
--    Create with LOGIN and a password from your secrets manager, e.g.:
--    CREATE ROLE app_user LOGIN PASSWORD '...' NOSUPERUSER NOBYPASSRLS;
CREATE ROLE app_user NOLOGIN NOSUPERUSER NOBYPASSRLS;

-- 2. Tables
CREATE TABLE memberships (
  user_id uuid NOT NULL,
  org_id  uuid NOT NULL,
  role    text NOT NULL CHECK (role IN ('admin', 'accountant', 'auditor')),
  PRIMARY KEY (user_id, org_id)
);

CREATE TABLE invoices (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id       uuid NOT NULL,
  vendor       text NOT NULL,
  amount       numeric(14,2) NOT NULL,
  invoice_date date NOT NULL,
  bank_enc     text NOT NULL,            -- ciphertext from pubkey_db.py
  created_by   uuid NOT NULL,
  flagged      boolean NOT NULL DEFAULT false
);

-- 3. Who is the current user? The app sets this per transaction (see rls_client.py).
--    On Supabase, replace the body with: SELECT auth.uid();
CREATE FUNCTION app_user_id() RETURNS uuid
LANGUAGE sql STABLE AS $$
  SELECT nullif(current_setting('app.user_id', true), '')::uuid
$$;

-- 4. Lock everything down, then grant the minimum.
REVOKE ALL ON memberships, invoices FROM PUBLIC;
GRANT SELECT ON memberships TO app_user;
GRANT SELECT, INSERT, DELETE ON invoices TO app_user;
GRANT UPDATE (vendor, amount, invoice_date, flagged) ON invoices TO app_user;  -- not org_id/created_by/bank_enc
GRANT EXECUTE ON FUNCTION app_user_id() TO app_user;

-- 5. Turn on RLS. FORCE applies it to the table owner too.
ALTER TABLE memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE memberships FORCE  ROW LEVEL SECURITY;
ALTER TABLE invoices    ENABLE ROW LEVEL SECURITY;
ALTER TABLE invoices    FORCE  ROW LEVEL SECURITY;
-- With RLS on and no matching policy, access is denied by default.

-- 6. Policies
-- Users can see only their own memberships.
CREATE POLICY memberships_read ON memberships FOR SELECT TO app_user
  USING (user_id = app_user_id());

-- Any member of the org can read its invoices (admin, accountant, auditor).
CREATE POLICY invoices_read ON invoices FOR SELECT TO app_user
  USING (EXISTS (SELECT 1 FROM memberships m
                 WHERE m.org_id = invoices.org_id AND m.user_id = app_user_id()));

-- Admins and accountants can add invoices, only as themselves.
CREATE POLICY invoices_insert ON invoices FOR INSERT TO app_user
  WITH CHECK (created_by = app_user_id() AND EXISTS (
    SELECT 1 FROM memberships m
    WHERE m.org_id = invoices.org_id AND m.user_id = app_user_id()
      AND m.role IN ('admin', 'accountant')));

-- Admins and accountants can edit; the new row must stay in an org they can edit.
CREATE POLICY invoices_update ON invoices FOR UPDATE TO app_user
  USING (EXISTS (
    SELECT 1 FROM memberships m
    WHERE m.org_id = invoices.org_id AND m.user_id = app_user_id()
      AND m.role IN ('admin', 'accountant')))
  WITH CHECK (EXISTS (
    SELECT 1 FROM memberships m
    WHERE m.org_id = invoices.org_id AND m.user_id = app_user_id()
      AND m.role IN ('admin', 'accountant')));

-- Only admins can delete.
CREATE POLICY invoices_delete ON invoices FOR DELETE TO app_user
  USING (EXISTS (
    SELECT 1 FROM memberships m
    WHERE m.org_id = invoices.org_id AND m.user_id = app_user_id()
      AND m.role = 'admin'));

-- 7. Check it works (run in psql after loading sample rows):
--   BEGIN;
--   SET LOCAL ROLE app_user;
--   SELECT set_config('app.user_id', '<some-user-uuid>', true);
--   SELECT * FROM invoices;     -- only that user's orgs
--   ROLLBACK;
