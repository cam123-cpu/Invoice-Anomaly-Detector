"""End-to-end test of the web app in a real browser (Chromium via Playwright).

Runs only when RUN_E2E=1. It starts the real server on a temporary database with two
companies, then signs in, uploads a messy Excel file, reviews flags, checks that an
uploaded <script>-style vendor name is shown as text and never run, and checks that
another company sees none of it. Screenshots go to E2E_SCREENSHOTS (default: e2e-shots/).

Set E2E_BASE_URL to test an already-running server that has the same users instead.
"""
import io
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from datetime import datetime

from openpyxl import load_workbook

RUN = os.environ.get("RUN_E2E") == "1"
SHOTS = os.environ.get("E2E_SCREENSHOTS", "e2e-shots")
USERS = {"alice": ("Acme Inc", "admin", "alice-password-123"),
         "audrey": ("Acme Inc", "auditor", "audrey-password-123"),
         "bob": ("Globex", "admin", "bob-password-12345"),
         "carmen": ("Initech", "accountant", "carmen-password-123")}
XSS_VENDOR = '<img src=x onerror="window.__pwned=1">Evil Vendor'


def messy_excel() -> bytes:
    """What a real export looks like: a title row, odd column names, mixed date and
    amount formats, a totals row, a bad row, and a duplicate invoice."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "AP export"
    ws.append(["Accounts payable register - Q1 2026"])
    ws.append([])
    ws.append(["Invoice #", "Supplier", "Invoice Date", "Total", "IBAN", "Memo"])
    ws.append(["Q1-001", "Northwind Traders", datetime(2026, 2, 3), 1250.0, "GB33 BUKB 2020 1555 5555 55", "Feb"])
    ws.append(["Q1-002", "Northwind Traders", "17/02/2026", "$1,250.00", "GB33 BUKB 2020 1555 5555 55", "dup"])
    ws.append(["Q1-003", XSS_VENDOR, "18/02/2026", "980,00", "", "xss"])
    ws.append(["Q1-004", "Contoso Ltd", "not a date", 300, "", "bad"])
    ws.append(["Q1-005", "Northwind Traders", datetime(2026, 2, 3), 1250.0, "GB33 BUKB 2020 1555 5555 55", "exact dup"])
    ws.append([None, "TOTAL", None, 5030.0, None, None])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def xlsx(path):
    """Open a downloaded workbook (Playwright saves downloads without an extension)."""
    with open(path, "rb") as f:
        return load_workbook(io.BytesIO(f.read()))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(RUN, "set RUN_E2E=1 to run the browser test")
class BrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(SHOTS, exist_ok=True)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server = None
        cls.base = os.environ.get("E2E_BASE_URL")
        if not cls.base:
            cls._start_server()
        from playwright.sync_api import sync_playwright

        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def _start_server(cls):
        from invoice_detector import cli
        from invoice_detector.config import Settings
        from invoice_detector.db import Database
        from invoice_detector.demo import generate_demo

        old = os.getcwd()
        os.chdir(cls.tmp.name)
        try:
            cli.main(["init"])
            db = Database.from_settings(Settings.from_env())
            orgs = {name: db.create_org(name) for name in ("Acme Inc", "Globex", "Initech")}
            for user, (company, role, password) in USERS.items():
                db.create_user(orgs[company], user, password, role)
            rows, _ = generate_demo()
            db.add_invoices(orgs["Acme Inc"], rows, "seed")
            db.close()
        finally:
            os.chdir(old)
        port = free_port()
        cls.base = f"http://127.0.0.1:{port}"
        cls.server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "invoice_detector.api:create_app", "--factory",
             "--port", str(port), "--log-level", "warning"], cwd=cls.tmp.name)
        for _ in range(100):
            try:
                urllib.request.urlopen(cls.base + "/health", timeout=1)
                return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        if cls.server:
            cls.server.terminate()
            cls.server.wait(10)
        cls.tmp.cleanup()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1360, "height": 900}, device_scale_factor=1)
        self.page = self.context.new_page()
        self.errors = []
        # The browser logs every non-2xx response; the deliberate wrong-password attempt is expected.
        self.page.on("console", lambda m: m.type == "error" and "status of 401" not in m.text
                     and self.errors.append(m.text))
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("dialog", lambda d: (self.errors.append("dialog: " + d.message), d.dismiss()))

    def tearDown(self):
        self.context.close()

    def shot(self, name):
        self.page.screenshot(path=os.path.join(SHOTS, name + ".png"), full_page=True)

    def login(self, user):
        p = self.page
        p.goto(self.base + "/")
        p.fill("#username", user)
        p.fill("#password", USERS[user][2])
        p.click("button[type=submit]")
        p.wait_for_selector("h1:text('Dashboard')")

    def test_full_workflow(self):
        p = self.page
        p.goto(self.base + "/")
        p.wait_for_selector("#username")
        self.shot("01-login")
        p.fill("#username", "alice")
        p.fill("#password", "wrong-password")
        p.click("button[type=submit]")
        p.wait_for_selector("text=Wrong username or password.")

        self.login("alice")
        self.assertIn("Acme Inc", p.inner_text(".brand-org"))
        self.assertIn("Dashboard", p.title())

        # run detection from the review queue
        p.click("nav a[href='#/review']")
        p.click("button:text('Run detection')")
        p.wait_for_selector("td.mono:text('INV-0249')")
        self.shot("03-review-queue")
        first_id = p.inner_text("tbody tr.clickable >> nth=0 >> td.mono")
        self.assertEqual(first_id, "INV-0249")  # highest risk first
        p.click("tbody tr.clickable >> nth=0")
        p.wait_for_selector("tr.details:not(.hidden) li")
        open_before = int(p.inner_text(".tabs button.on").split("(")[1].rstrip(")"))
        p.click("button[aria-label='Confirm INV-0249 is a problem']")
        p.wait_for_selector("text=INV-0249 marked as a real problem")
        p.click("button[aria-label='Dismiss INV-0255 as a false alarm']")
        p.wait_for_selector("text=INV-0255 dismissed as a false alarm")
        self.assertEqual(int(p.inner_text(".tabs button.on").split("(")[1].rstrip(")")), open_before - 2)
        self.assertEqual(p.locator("td.mono:text-is('INV-0249')").count(), 0)  # left the Open tab
        p.click(".tabs button:text('Confirmed')")
        p.wait_for_selector("td.mono:text-is('INV-0249')")
        p.fill("input[type=search]", "nothing-matches-this")
        p.wait_for_selector("text=No matches")

        # dashboard reflects the decisions
        p.click("nav a[href='#/dashboard']")
        p.wait_for_selector("text=How each check is performing")
        self.assertIn("1", p.inner_text(".kpi:has-text('Confirmed problems') .value"))
        self.shot("02-dashboard")

        # upload a messy Excel export
        p.click("nav a[href='#/upload']")
        p.set_input_files("#file-input", files=[{"name": "ap-export.xlsx", "buffer": messy_excel(),
            "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}])
        p.wait_for_selector("text=Which column is which?")
        self.assertEqual(p.input_value("#map-id"), "Invoice #")
        self.assertEqual(p.input_value("#map-vendor"), "Supplier")
        self.assertEqual(p.input_value("#map-amount"), "Total")
        self.assertEqual(p.input_value("#map-date"), "Invoice Date")
        self.assertEqual(p.input_value("#map-bank_account"), "IBAN")
        self.assertEqual(p.input_value("#date-order"), "DMY")  # 17/02/2026 proves day-first
        self.shot("04-upload-mapping")
        p.click("button:text('Import invoices')")
        p.wait_for_selector("text=Rows that were not imported")
        self.assertEqual(p.inner_text(".kpi:has-text('Invoices added') .value"), "4")
        self.assertEqual(p.inner_text(".kpi:has-text('Rows with problems') .value"), "1")
        self.assertIn("not a recognised date", p.inner_text("table"))
        self.shot("05-upload-results")

        # your own workbook back, with the results written in
        with p.expect_download() as dl:
            p.click("button:text('Download your file with flags')")
        self.assertEqual(dl.value.suggested_filename, "ap-export - checked.xlsx")
        wb = xlsx(dl.value.path())
        ws = wb["AP export"]
        self.assertEqual([ws.cell(3, c).value for c in range(7, 11)], ["Risk", "Review status", "Flags", "Why flagged"])
        self.assertEqual(ws.cell(1, 1).value, "Accounts payable register - Q1 2026")  # original untouched
        by_id = {ws.cell(r, 1).value: r for r in range(4, 10)}
        self.assertGreater(ws.cell(by_id["Q1-005"], 7).value, 0)
        self.assertIn("Duplicate", ws.cell(by_id["Q1-005"], 9).value)
        self.assertEqual(ws.cell(by_id["Q1-004"], 8).value, "Not imported")
        self.assertEqual(ws.cell(by_id["Q1-003"], 2).value, XSS_VENDOR)
        self.assertIn("Flagged invoices", wb.sheetnames)

        # the new invoices were checked, and the hostile vendor name is inert text
        p.click("a:text('Open review queue')")
        with p.expect_download() as dl:
            p.click("button:text('Download Excel')")
        report = xlsx(dl.value.path())["Review queue"]
        self.assertEqual([c.value for c in report[1]][:3], ["Risk", "Invoice", "Vendor"])
        self.assertEqual(report.cell(2, 2).value, "INV-0249")  # highest risk first
        p.fill("input[type=search]", "Q1-")
        p.wait_for_selector("td.mono:text-is('Q1-005')")
        self.assertIn("Duplicate", p.inner_text("tr:has(td.mono:text-is('Q1-005'))"))
        p.click("nav a[href='#/invoices']")
        p.fill("input[type=search]", "Evil")
        p.wait_for_selector("td:text-is('" + XSS_VENDOR + "')")
        self.assertIsNone(p.evaluate("window.__pwned"))
        self.assertEqual(p.locator("img[src=x]").count(), 0)
        p.fill("input[type=search]", "Q1-001")
        p.click("tr:has(td.mono:text-is('Q1-001')) >> button:text('Show')")
        p.wait_for_selector("text=GB33 BUKB 2020 1555 5555 55")
        self.shot("06-invoices")

        # settings: add a colleague; activity log shows what happened
        p.click("nav a[href='#/settings']")
        p.fill("#new-username", "carol@acme.example")
        p.fill("#new-password", "carol-password-123")
        p.select_option("#new-role", "accountant")
        p.click("button:text('Add person')")
        p.wait_for_selector("td:text-is('carol@acme.example')")
        self.shot("07-settings")
        p.click("nav a[href='#/audit']")
        p.wait_for_selector("td:text-is('Imported a file')")
        for action in ("Reviewed an invoice", "Viewed a bank account", "Added a user", "Ran detection"):
            self.assertGreater(p.locator(f"td:text-is('{action}')").count(), 0, action)
        self.shot("08-activity-log")

        p.click("text=Sign out")
        p.wait_for_selector("#username")
        self.assertEqual(self.errors, [])

    def test_auditor_is_read_only(self):
        p = self.page
        self.login("audrey")
        nav = p.inner_text("nav")
        self.assertNotIn("Upload invoices", nav)
        self.assertIn("Activity log", nav)
        p.click("nav a[href='#/review']")
        p.wait_for_selector("h1:text('Review queue')")
        self.assertEqual(p.locator("button[aria-label^='Confirm'], button:text-is('Run detection')").count(), 0)
        p.goto(self.base + "/#/upload")  # typing the address doesn't help either
        p.wait_for_selector("h1:text('Dashboard')")
        self.assertEqual(self.errors, [])

    def test_other_company_sees_nothing(self):
        p = self.page
        self.login("bob")
        self.assertIn("Globex", p.inner_text(".brand-org"))
        p.wait_for_selector("text=No invoices yet")
        p.click("nav a[href='#/invoices']")
        p.wait_for_selector("text=0 invoices.")
        p.click("nav a[href='#/review']")
        p.wait_for_selector("text=Nothing waiting for review")
        self.assertEqual(self.errors, [])

    def test_phone_layout_has_no_sideways_scroll(self):
        ctx = self.browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2)
        self.page = ctx.new_page()
        try:
            self.login("alice")
            for route in ("dashboard", "review", "upload", "invoices", "settings"):
                self.page.goto(f"{self.base}/#/{route}")
                self.page.wait_for_selector("h1")
                self.page.wait_for_timeout(300)
                overflow = self.page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
                self.assertLessEqual(overflow, 1, route)
            self.page.goto(self.base + "/#/review")
            self.page.wait_for_selector("h1:text('Review queue')")
            self.shot("09-phone-review")
        finally:
            ctx.close()

    def test_samples_and_multi_sheet_workbooks(self):
        p = self.page
        self.login("carmen")
        p.click("nav a[href='#/upload']")
        p.wait_for_selector("text=No file handy? Try a sample")
        self.assertEqual(p.locator(".sample").count(), 4)
        self.shot("10-samples")
        with p.expect_download() as dl:
            p.click(".sample:has-text('Blank template') >> button:text('Download')")
        self.assertEqual(xlsx(dl.value.path()).sheetnames[0], "Invoices")

        p.click("button[data-sample='ap-register-2026.xlsx']")
        p.wait_for_selector("#sheet")
        self.assertEqual(p.input_value("#sheet"), "Q1 2026")
        self.assertEqual(p.input_value("#map-id"), "Ref")
        self.assertEqual(p.input_value("#map-amount"), "Amount due")
        self.shot("11-sheet-picker")
        p.click("button:text('Import invoices')")
        p.wait_for_selector("text=Rows that were not imported")
        self.assertIn("TBC", p.inner_text("table"))
        for sheet in ("Q2 2026", "Q3 2026"):
            p.click(f"button:text('Import sheet \"{sheet}\"')")
            p.wait_for_selector("text=We've reused the matching you chose last time")
            self.assertEqual(p.input_value("#sheet"), sheet)
            p.click("button:text('Import invoices')")
            p.wait_for_selector(".kpi:has-text('Invoices added')")
        self.assertGreater(int(p.inner_text(".kpi:has-text('Flagged from this file') .value")), 0)

        p.click("nav a[href='#/upload']")
        p.click("button[data-sample='european-supplier-ledger.xlsx']")
        p.wait_for_selector("text=Which column is which?")
        self.assertEqual(p.input_value("#date-order"), "DMY")
        self.assertEqual(p.input_value("#map-amount"), "Gross amount (EUR)")
        p.click("button:text('Import invoices')")
        p.wait_for_selector(".kpi:has-text('Invoices added')")
        self.assertEqual(p.inner_text(".kpi:has-text('Invoices added') .value"), "260")
        p.click("a:text('Open review queue')")
        p.wait_for_selector("td.mono:text('RE-2026-0251')")
        self.assertEqual(self.errors, [])

    def test_security_headers(self):
        with urllib.request.urlopen(self.base + "/") as resp:
            csp = resp.headers.get("Content-Security-Policy", "")
        self.assertIn("script-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)


if __name__ == "__main__":
    unittest.main()
