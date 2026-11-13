# Security review (penetration test), November 2026

A review of the app from an attacker's point of view: reading the code for each class of
weakness, trying the attacks against the running pieces, fixing what was real, and keeping
each attack as an automated test so the hole can't quietly come back.

**Scope:** the web API, sign-in (passwords, two-factor, single sign-on, reset), sessions,
access between companies and roles, file uploads (lists and invoice documents), Excel
output, the detector, and the production container setup.

**Not covered:** the server and network the app is deployed on, real Microsoft / Google /
SMTP / Anthropic services (tested against stand-ins), and anything a professional third-party
test would add. This is a careful self-review, not a certificate; before handling real payment
data, have an independent tester repeat it.

## Findings and fixes

| # | Severity | What an attacker could do | Fix |
|---|---|---|---|
| 1 | High | **Freeze the server.** The look-alike vendor check compared every vendor with every other inside the web request: 1,500 vendors took 21 s, and one upload with 50,000 distinct names would occupy the server for hours. | Pairs that can't match (very different length or letters) are skipped; very large lists compare only names sharing a start or an end; a hard ceiling bounds the work. Same results as before on the tests; 50,000 vendors: about 4 s. |
| 2 | Medium | **Use a stolen session after sign-out.** Signing out only deleted the browser's cookie; a copied token kept working until it expired (up to an hour). | Every token is tied to a per-user counter that sign-out raises, so all of that person's sessions end on the server. |
| 3 | Medium | **Keep access after leaving.** There was no way to remove a person; a leaver's password kept working. | Admins can remove access: the person is signed out at once, and password, reset links and single sign-on all refuse. The last admin can't be removed; nobody can remove themselves. |
| 4 | Medium | **Impersonate a colleague through Microsoft sign-in.** The app trusted the `email` claim first, which Microsoft doesn't guarantee belongs to the person. | The directory-assigned sign-in name is used; `email` only when Microsoft marks it domain-verified. For any provider, an address marked unverified is refused. |
| 5 | Medium | **Guess a two-factor code.** The "new recovery codes" action checked a code with no attempt limit, so a hijacked session could try all 1,000,000 codes and collect recovery codes: a permanent way past two-factor. | The same 5-attempt limit as other identity checks. |
| 6 | Medium | **Crash or stall the server with a hostile spreadsheet.** Protection against XML "bombs" relied on a library that was not a declared requirement, and list uploads were parsed inside the web server process. | `defusedxml` is now required; list uploads are parsed in the same isolated, time- and memory-limited process as invoice documents. |
| 7 | Low | **Use one two-factor or recovery code twice** by sending two requests at the same instant (check, then mark as used, was two steps). | Marking a code as used is one conditional database update: exactly one request wins. |
| 8 | Low | **Find out who has an account.** "Forgot password" answered more slowly for real accounts, because it waited for the email to send. | Emails are sent in the background; the response time no longer depends on the account. |
| 9 | Low | **Show their own text on the real sign-in page** with a crafted link (`#/login?error=...`), for example a fake "call this number" notice. | Messages travel as short-lived tokens signed by the server; anything else is ignored. |
| 10 | Low | **Send flag details to the AI service without the company's opt-in**, and repeat it or document reading without limit. | AI summaries now follow the company's AI switch and are audited; summaries and document reading have per-company limits. |
| 11 | Low | **Steal a token with an injected script at sign-in.** The token was in an HttpOnly cookie, but also in the JSON the page received. | The web app no longer receives the token at all; only programs calling the API do. |
| 12 | Low | **Guess long but obvious passwords** such as `password1234` or the username plus digits. | Common words, keyboard runs, repeats and username-based passwords are refused wherever a password is set. |

### Second round (after several servers, credit notes, e-invoices and learning)

| # | Severity | What an attacker could do | Fix |
|---|---|---|---|
| 13 | Medium | **Get a duplicate paid with a fake credit note.** Copy an invoice that was already paid, and add a credit note "cancelling" the original: the copy was no longer a duplicate and went straight to *Ready to pay*. | The copy is flagged *Replaces a cancelled invoice* (hold) until the credit is confirmed with the vendor. |
| 14 | Medium | **Tie up the document reader** with one very long line of text in a PDF: reading was quadratic, 1.6 s for 6,000 characters, so a page of such lines held a reader for its full 45 s. | Values are read from at most 200 characters; a file with more than 4,000 pieces of text (largest real invoice tested: 269) is refused. |
| 15 | Low | **Dodge the per-address sign-in limit behind the load balancer** by sending a forged `X-Forwarded-For`. Caddy already replaced the header by default, but nothing guaranteed it. | The Caddyfile sets the header from the connection explicitly; CI forges it on 40 attempts and checks the limit still stops them. |
| 16 | Low | **Learn which usernames were attacked** from a copy of the rate-limit table: keys were a plain hash of the username. | Keys are an HMAC with the server's index key. |
| 17 | Low | **Teach the reader a misleading label** for a vendor (an insider correcting one invoice), so colleagues see a wrong pre-filled value. | Every value is still shown for checking, with a note when it was read from a learned label; the totals check still applies; each new lesson is in the activity log with who taught it. |

### Third round (payment controls, and a sweep of every route)

| # | Severity | What an attacker could do | Fix |
|---|---|---|---|
| 18 | Medium | **Enter a fake invoice and wave it through yourself.** Anyone who could review could release an invoice they had added. | Separation of duties: whoever added an invoice can't release its flags, and a release at or above the approval threshold needs a second, different person (`controls.py`). |
| 19 | Medium | **Guess a password from a botnet.** The sign-in limit counted per address + username: every address got five fresh guesses at the same account, so 1,000 addresses meant 5,000 guesses every 15 minutes. | A second limit counts wrong passwords for the account from everywhere: 20 in an hour locks it for 15 minutes. |
| 20 | Low | **Crash request handling, or have input reflected back.** Fuzzing every route found 120 server errors: a body such as `1e309` (infinity) made the default validation answer, which echoed the input, impossible to encode. | Validation errors say where the problem is and never echo input; over-nested JSON is a 400; anything unexpected is logged and answered as JSON with no internals. The fuzz test runs on every push. |
| 21 | Low | **Make a payment run fail** by marking the same invoice paid twice at the same moment (a database error on PostgreSQL). | Marking paid is one insert that does nothing if the invoice is already paid. |

Checked in this round and found sound: every route's sign-in and role requirement (listed and compared with what each does); CSRF protection on the new DELETE route; formula injection through the new payment-file text (cells are always text); another company's invoice in `/approve` or `/payments` (not found).

Checked in the second round and found sound: XML in e-invoices (DTDs and entities refused, 2 MB cap, 140,000-level nesting, 380,000 elements and 140,000 attributes each parse in under a second); a 100 MB attachment compressed into a small PDF (refused by pypdf's limit, the page is read instead); a credit note's reference (validated like an invoice number, shown as text, scoped to the company); lessons (signed in the draft, reduced to letters, isolated per company by row-level security).

## Checked and found sound

These were attacked and held, so nothing was changed:

- **SQL injection:** every query is parameterised; the two places that build SQL from names use fixed allow-lists.
- **Script injection (XSS):** the page never builds HTML from data, and the Content-Security-Policy blocks inline and third-party scripts.
- **Spreadsheet formula injection:** text starting with `=`, `+`, `-` or `@` is neutralised in every Excel file the app writes.
- **Cross-site request forgery:** SameSite=Strict cookie, required custom header, Origin check.
- **Reaching another company's data:** every query is scoped by the signed-in company, and on PostgreSQL row-level security enforces it in the database (including the new documents table).
- **Roles:** every route except a short public list requires sign-in (enforced by a test), and changes require the right role.
- **Token forgery and misuse:** HMAC-signed, constant-time comparison, purpose-bound (a reset link or a document draft can't act as a session).
- **Single sign-on protocol:** state, nonce, PKCE, signature algorithm allow-list (no `none`), issuer, audience and expiry checks; codes can't be replayed.
- **Path traversal and header injection:** static files and samples come from fixed lists; download filenames are sanitised.
- **Zip bombs, fake file types, oversized requests, decompression-bomb images:** refused.
- **Client address spoofing** to dodge rate limits: the address comes from the connection, and forwarded headers are trusted only from the configured proxy.

## Known limits that remain

- Sign-in rate limits are kept in the database (keyed hashes, one atomic statement per attempt), so they hold across any number of app copies. An attacker can still lock a known username out for 15 minutes by guessing wrong five times; that is the trade the limiter makes.
- On SQLite (laptop use) company separation relies on the application code alone; PostgreSQL adds the database-level guarantee.
- A company admin can tell that a username is taken by another company (usernames are global).
- Signing out ends all of that person's sessions, not just one device.
- A credit note confirmed by a reviewer releases its re-issued invoice only by the reviewer's judgement: the app can't check with the vendor itself.
- Separation of duties assumes people have one account each. An admin can create a second account, disable the other reviewers, or raise the approval threshold; each of these is in the activity log, which is where an auditor should look. Companies that need more should have a second admin review that log.
- The account-wide sign-in limit means someone who knows a username can keep it locked by guessing wrong 20 times an hour. It locks for 15 minutes, single sign-on is unaffected, and an admin sees the failed attempts in the log.

## Re-running it

`pytest tests/test_attacks.py -v` runs the attacks; they also run on every push, alongside
the secret scan and the dependency audit.
