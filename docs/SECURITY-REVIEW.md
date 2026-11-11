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

- The sign-in rate limiter lives in each server's memory: run one web server, or move it to a shared store first.
- On SQLite (laptop use) company separation relies on the application code alone; PostgreSQL adds the database-level guarantee.
- A company admin can tell that a username is taken by another company (usernames are global).
- Signing out ends all of that person's sessions, not just one device.

## Re-running it

`pytest tests/test_attacks.py -v` runs the attacks; they also run on every push, alongside
the secret scan and the dependency audit.
