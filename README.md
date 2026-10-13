# GST Practice Automation Platform

A multi-tenant web application for Indian CA firms. It reconciles each
client's purchase register against GSTR-2B to identify eligible, ineligible
and mismatched Input Tax Credit, and manages the firm's review and approval
workflow around the exceptions that result.

Firms sign up, add their clients (each with one or more GSTINs), upload or
load GSTR-2B data, run reconciliation, and work the exception queue through
an associate-to-partner approval chain.

## What's in here

```
gstr2b/
├── backend/                   FastAPI + SQLAlchemy (PostgreSQL via Docker; SQLite fallback)
│   ├── app/
│   │   ├── main.py            FastAPI app + router registration
│   │   ├── models.py          22 tables; Firm is the tenant root
│   │   ├── schemas.py         Pydantic request/response schemas
│   │   ├── database.py        DB session/engine setup
│   │   ├── config.py          Environment-driven settings (.env) + tolerances
│   │   ├── money.py           Decimal helpers + Indian digit grouping
│   │   ├── security.py        Argon2 password hashing, JWT issue/verify
│   │   ├── crypto.py          Fernet encryption at rest, blind index, token hashing
│   │   ├── tenancy.py         *** TenantSession: firm scoping on every query ***
│   │   ├── deps.py            Auth dependencies, role checks
│   │   ├── audit.py           Append-only, hash-chained audit log
│   │   ├── validation.py      GSTIN checksum, PAN, row-level rules
│   │   ├── gsp/               GSP interface, Sandbox adapter, mock, GSTN format parser
│   │   ├── storage/           Encrypted file storage (local disk; S3 adapter later)
│   │   ├── ocr/               Text extraction: PDF text layer, optional Tesseract
│   │   ├── notice_ai/         Notice analyser interface, mock, Claude
│   │   ├── services/          consent, gstr2b_sync (fetch + retry), ingest, reconciliation,
│   │   │                      notices (workflow, drafting, alerts), notice_evidence,
│   │   │                      messaging (Resend / console)
│   │   ├── worker/            Celery app, schedule and tasks
│   │   ├── matching/
│   │   │   ├── engine.py      *** The core reconciliation matching engine ***
│   │   │   └── normalize.py   Invoice number / GSTIN normalization helpers
│   │   ├── ingestion/
│   │   │   ├── purchase_register.py   CSV/XLSX parser (pandas)
│   │   │   └── gstr2b.py              GSTR-2B JSON parser + GSP API stub
│   │   ├── export/
│   │   │   └── excel_export.py        Exception report -> XLSX
│   │   └── routers/           auth / clients / uploads / reconciliation /
│   │                           exceptions / export / gsp / notices / notifications
│   ├── alembic/               Database migrations (schema source of truth)
│   │   └── versions/
│   ├── alembic.ini
│   ├── .env.example           Documented environment variables
│   ├── scripts/
│   │   ├── generate_sample_data.py    Regenerates the sample dataset
│   │   ├── test_matching.py           Standalone proof the engine works (no DB)
│   │   ├── seed.py                    Seeds a demo firm, staff, client and run
│   │   ├── make_demo_notice.py        Sample notice PDFs for the demo client
│   │   ├── add_demo_staff.py          Adds manager/associate/partner without a reset
│   │   └── run_notice_alerts.py       Runs the deadline-alert job for any date
│   ├── tests/
│   │   ├── conftest.py                Fixtures, incl. the two-firm scenario
│   │   ├── test_tenant_isolation.py   *** Cross-firm access must always fail ***
│   │   ├── test_auth.py               Sessions, roles, audit chain
│   │   ├── test_workflow.py           Upload -> reconcile -> approve -> re-run
│   │   ├── test_validation.py         GSTIN checksum and row validation
│   │   └── test_matching_engine.py    Matching engine branches
│   └── sample_data/            Bundled sample purchase register + GSTR-2B JSON
└── frontend/                   React + TypeScript + Tailwind CSS (Vite)
    └── src/
        ├── auth/               AuthContext (in-memory token), ProtectedRoute
        ├── pages/              Login, Signup, Clients, Upload, Dashboard,
        │                       ExceptionQueue, Notices, NoticeDetail
        ├── components/         SummaryCards, ExceptionTable, ExceptionRow, FileUpload
        └── api/client.ts       Typed API client with refresh-on-401
```

## Data model

The tenant root is **Firm**. Every other table carries a `firm_id` and is
filtered by it on every query.

```
Firm
 └── Client                       (a business the firm acts for)
      └── ClientGSTIN             (one registration per state)
           ├── PurchaseRegisterUpload  ──> PurchaseRegisterEntry
           ├── GSTR2BFetch             ──> GSTR2BEntry
           └── ReconciliationRun       ──> ReconciliationResult
                                            └── ReconciliationException
                                                 └── ExceptionComment
```

Alongside these sit `User`, `RefreshToken`, `Invitation`, `Notice`,
`ClientDocument`, `AuditLog`, `ConsentLog`, `Subscription`, `UsageRecord`
and `Notification`.

Three decisions are worth knowing before changing anything:

**Uploads are versioned, never overwritten.** Re-uploading a purchase
register for the same GSTIN and period creates a new version and marks it
current. Earlier versions stay, and `/uploads/{id}/diff` reports what
changed between two of them.

**Human work is stored separately from derived data.** A
`ReconciliationResult` is recomputed and replaced on every run.
A `ReconciliationException` holds the resolution notes and approvals, is
keyed to the invoice by `match_key` (vendor GSTIN plus normalised invoice
number), and is carried across re-runs. In the original single-tenant MVP,
re-running a reconciliation silently destroyed every note the firm had
written; that is the bug this split fixes.

**Money is `NUMERIC(14,2)`, handled as `Decimal`.** See the note further
down on why, and on the one conversion boundary in the matching engine.

## Roles

| Role | Can do |
|---|---|
| Owner | Everything, including billing |
| Partner | Everything except billing; invites staff |
| Manager | Manages clients, approves resolutions |
| Associate | Uploads data, runs reconciliation, proposes resolutions |
| Client viewer | Read-only, restricted to its own client |

Roles are ranked, so a check for Manager also admits Partner and Owner. An
associate's resolution is a proposal: it moves to `PENDING_APPROVAL` and
only a manager or above can approve it. Whoever wrote a resolution cannot
approve it themselves, even if their role would otherwise allow it.

## Tenant isolation

`firm_id` is never accepted from client input. It is derived from the
authenticated session and applied by `app/tenancy.py`, which hands routes a
`TenantSession` rather than a raw database session. A `TenantSession`
cannot express an unscoped query against a tenant-owned table, and fetching
another firm's row by a valid id returns `None`, which surfaces as a 404
rather than a 403 so the id's existence is not confirmed.

`tests/test_tenant_isolation.py` takes real identifiers belonging to a
second firm and presents them while authenticated as the first, at both the
data layer and over HTTP. Those tests are the ones that must never be
allowed to fail.

## The matching engine (`backend/app/matching/engine.py`)

Runs in strict priority order, one-to-one (each GSTR-2B entry can satisfy at
most one purchase entry):

1. **Exact match** — same vendor GSTIN + invoice number equal after
   normalization (strip spaces/slashes/hyphens/dots, case-insensitive) +
   taxable value within ₹1 → `MATCHED`
2. **Formatting mismatch** — same vendor GSTIN, invoice date within ±3 days,
   taxable value within ₹10, but the invoice number genuinely differs after
   normalization (e.g. a dropped leading zero) → `FORMATTING_MISMATCH`
   (auto-resolved, confidence scored by how close the values are)
3. **Value mismatch** — same invoice number but the taxable value differs
   beyond tolerance → `VALUE_MISMATCH` (flagged for review — likely an
   unrecorded credit note, or a data entry error)
4. **Supplier not filed** — exists in the purchase register, no match in
   GSTR-2B at all → `SUPPLIER_NOT_FILED` (ITC not eligible this period)
5. **Missing in books** — exists in GSTR-2B, no match in the purchase
   register → `MISSING_IN_BOOKS` (client may have forgotten to record it)

Every non-`MATCHED` result gets a plain-language explanation, e.g.:

> Vendor ABC Pvt Ltd invoice appears in GSTR-2B with taxable value ₹1,50,000
> but books show ₹2,00,000 — possible unrecorded credit note of ₹50,000, or
> a data entry error. Review before filing.

Confidence is an integer percentage from 0 to 100. Tolerances are passed
into the engine rather than read from globals, so a firm can configure its
own thresholds and a historical run records the exact settings it used.

The engine is a pure function
(`reconcile(purchase_entries, gstr2b_entries, tolerances)`)
decoupled from the database and web framework — `scripts/test_matching.py`
proves it against the sample data with no DB or HTTP server involved, and
`tests/test_matching_engine.py` unit-tests every branch.

## Getting GSTR-2B

There are two routes in, and they end in the same place. A GSTR-2B arrives either
by **live fetch** from the GST portal, or by **file upload** (JSON, or the bundled
sample). Both are parsed by `app/ingestion/gstr2b.py`, stored by
`app/services/ingest.py` as a new version of the period, and reconciled by the
same engine. Manual upload is a permanent path, not a stopgap: firms and clients
that never connect live access lose nothing.

### Live GST portal access

Live fetching goes through a `GSPProvider` interface (`app/gsp/base.py`) with
`verify_gstin`, `initiate_otp_consent`, `verify_otp_consent`, `refresh_consent`,
`fetch_gstr2b` and `fetch_gstr1`. Nothing above that boundary names a concrete
provider, so adding ClearTax or MasterGST means writing one class and one branch
in `app/gsp/__init__.py`.

| Provider | Use |
|---|---|
| `mock` | Default in development. No credentials, returns sample data, accepts OTP `123456`. **Refused when `ENVIRONMENT` is not `development`**, because it fabricates filings. |
| `sandbox` | Sandbox.co.in, written against their published API reference. Needs `SANDBOX_API_KEY` and `SANDBOX_API_SECRET`. |

**How a client is connected.** On the clients page, *Connect live GST access*
walks a person through it: the taxpayer switches on *Manage API Access* on the GST
portal, the platform asks the portal to send an OTP to the taxpayer's registered
contact, and the person types it in. The access token is encrypted at rest and is
never returned by any endpoint or written to a log. The OTP is never stored.

**Consent is a session with two clocks, and neither is assumed.** Every taxpayer
token lasts 6 hours and is renewed silently, without an OTP, for as long as the
consent session lasts. The session itself is set by the taxpayer on the GST portal,
from 6 hours to 30 days, and is read from the provider's response. When it ends,
only a new OTP restores access and the assigned associate is told.

> **For hands-off monthly fetching, the client must set Manage API Access to the
> longest duration offered (30 days)** and someone must reconnect at least monthly,
> before the 14th. A 6-hour setting means the fetch on the 14th finds a dead session.
> The platform flags consents that will lapse before the fetch window opens.

**Guard rails:** OTP requests are limited per GSTIN per hour (each one texts the
client's phone), wrong OTPs are limited per request, a token issued by one provider
is never sent to another, and no call is made to the provider once consent has ended.

**Known limits, stated plainly:**

- GSTR-2B **credit and debit notes** are fetched and stored but **not yet reconciled**;
  the engine matches invoices only. The fetch result says how many were held back.
- The `ITC available` flag from GSTN is stored on each invoice, but the dashboard
  totals do not yet exclude blocked credit.
- Disconnecting ends the platform's use of access; the GST portal session runs until
  it expires, unless the client ends it on the portal.
- Points Sandbox's documentation leaves open are marked `UNVERIFIED` in
  `app/gsp/sandbox.py` and covered by tests that say so. Confirm them against a real
  sandbox account: whether the GSTR-2B `year` is the financial or calendar year
  (a setting, `SANDBOX_GSTR2B_YEAR_BASIS`), where the page count sits in a paginated
  response, the month format for GSTR-1, and that every `TEC`-prefixed error code is
  transient.

### The scheduled fetch

A Celery worker fetches GSTR-2B for every connected GSTIN at 06:00 IST on the 14th,
15th and 16th of each month (GSTN generates it on the 14th). Later days skip anyone
already fetched. Transient failures (GSTN down, rate limited, statement not yet
generated) retry with exponential backoff, up to six attempts. Permanent ones (consent
ended, malformed response) do not retry. **No outcome is silent**: every attempt is
recorded in `gstr2b_fetch_attempts` in words a person can act on, and a failed fetch
notifies the client's assigned associate (or the firm's managers, if nobody is
assigned) with the specific reason. A successful fetch reconciles automatically against
the latest purchase register, if there is one.

```bash
# from backend/, with the virtualenv active and Redis running
celery -A app.worker.celery_app worker --pool=solo -l info    # runs the jobs
celery -A app.worker.celery_app beat -l info                  # fires the schedule
```

`--pool=solo` is required on Windows, where Celery's default pool does not work; on
Linux drop the flag. Redis must be reachable at `CELERY_BROKER_URL`. To confirm a
worker is alive: `python -c "from app.worker.tasks import ping; print(ping.delay().get(timeout=10))"`
should print `pong`.

Besides the fetch, the worker renews tokens every 30 minutes and checks consent health
every 2 hours, warning once per cause before a consent lapses.

## GST notices

Upload a notice from the tax department (PDF, PNG or JPEG) and take it through to a
filed response. Notices are staff-only: client-viewer accounts never see them.

```
Received -> Under review -> Response drafted -> Partner approved -> Filed -> Closed
                 ^                  |
                 +---- sent back ---+
```

What happens, in order:

1. **Stored encrypted.** The file is encrypted with `ENCRYPTION_KEY` before it touches
   the disk, behind a storage interface (`app/storage`). Local disk today; an
   S3-compatible adapter is a second implementation of the same four methods. **Lose
   the key and every stored notice is unreadable**, so back it up off this machine.
2. **Text is read.** A digital PDF has its text layer read directly: instant, free and
   exact. A scan or photo needs OCR, which is optional (see below). When text cannot be
   read the notice is kept, flagged, and the text can be pasted in by hand.
3. **Analysed.** The reader classifies the notice and extracts its reference, section,
   dates, period, amount and what it asks for. Those become editable fields. What was
   originally read is kept, so a correction is visible as a difference.
4. **A person confirms the deadline.** A date the analyser read is marked unconfirmed,
   with the wording it came from, and a response cannot be submitted until someone checks
   it against the original. A wrong deadline is the one error here that cannot be undone.
5. **A first draft is built from real records.** "Generate" hands the drafter the
   client's reconciliation records for the notice's period as numbered evidence. The
   draft cites each fact as `[E3]`, and the server checks every citation against what it
   supplied: a citation to a record that does not exist gets the draft thrown away, and an
   amount that appears nowhere in the records or the notice is flagged for the reviewer.
   Whatever the records cannot answer is left as a placeholder for the practitioner.
6. **Drafter is not approver.** Whoever last edited the response, or submitted it, cannot
   approve it, whatever their role. Approval needs a Partner or Owner. Filing and closing
   need a Manager. The screen shows why a blocked step is blocked.
7. **Alerts.** A daily job (08:00 IST) notifies the assignee 7, 3 and 1 days before the
   deadline, on the day, and once when overdue. At one day and after, managers and
   partners are told too, and if nobody owns the notice it goes to them instead of no
   one. Each alert reaches a person once. Moving the deadline starts a new set.

Views: `/notices` is every open notice across clients, nearest deadline first (undated
last); `/notices?client=<id>` is one client's whole history, closed notices included.

### The analyser

`NOTICE_AI_PROVIDER` chooses what reads notices and drafts responses:

| Value | What it is |
|---|---|
| `mock` (default) | A keyword and pattern reader. No key, no network. Everything it reads is marked low confidence. **Refused unless `ENVIRONMENT=development`.** |
| `claude` | The Claude API. Needs `ANTHROPIC_API_KEY`; the model is `NOTICE_AI_MODEL` (default `claude-opus-5`). |

With `claude`, analysis sends the notice text to Anthropic. Drafting also sends the
client's reconciliation records for the period: vendor names and GSTINs, invoice numbers,
dates and amounts. Nothing else leaves the system. Decide whether that fits your clients'
consent and your engagement terms before switching it on. A refusal, a rate limit or an
outage is reported plainly, never retried silently, and never loses an upload.

### OCR

Optional, and off by default. To read photographed notices: install Tesseract, then
`pip install pytesseract pillow`, and set `TESSERACT_CMD` if it is not on PATH. Scanned
**PDFs** are not rasterised: they are flagged as scans and the text is pasted in by hand.

### Trying it

```bash
cd backend
python scripts/make_demo_notice.py       # writes demo_data/sample_notice_asmt10.pdf (due in 5 days)
python scripts/add_demo_staff.py         # adds a manager, associate and partner to the demo firm
python scripts/run_notice_alerts.py --today 2026-09-25   # run the alert job as if it were that day
```

## Setup & run

Requires **Python 3.11+** and **Node.js 18+**.

### 1. Backend

```bash
cd backend
python -m venv venv

# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate

pip install -r requirements.txt

# Create your local environment file
cp .env.example .env
# Then set DATABASE_URL to the Docker Postgres described under
# "Local database" below (or comment it out to fall back to SQLite).

# Create the database schema (Alembic owns the schema, not the app)
alembic upgrade head

# Generate the sample dataset (already included, but this regenerates it)
python scripts/generate_sample_data.py

# Prove the matching engine works against the sample data (no DB needed)
python scripts/test_matching.py

# Run the unit test suite
pytest tests/ -v

# Seed a demo firm, staff, a client and one reconciled period
python scripts/seed.py

# Start the API
uvicorn app.main:app --reload --port 8000
```

The API is now running at `http://localhost:8000` (docs at
`http://localhost:8000/docs`).

### 2. Frontend

In a second terminal:

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173` and sign in with the seeded demo account:

```
demo@sharma-ca.in / demo-password-123      (Owner)
```

The seeded client, Shree Ganesh Manufacturing Co., appears on the clients
page with one GSTIN. Open its dashboard for period `2026-03` to see the
reconciliation, or upload your own data for a new period.

The one thing a single account cannot demonstrate is the approval chain.
Nobody may approve a resolution they wrote themselves, so the demo user is
refused at that step by design. To try it, either invite a colleague from
the app, or re-seed with a manager and an associate as well:

```bash
SEED_EXTRA_STAFF=1 python scripts/seed.py     # macOS/Linux
$env:SEED_EXTRA_STAFF=1; python scripts/seed.py   # PowerShell
```

Then resolve an exception as `associate@sharma-ca.in` and approve it as
`manager@sharma-ca.in`, both with the same password.

Invitation and password-reset emails are written to the backend log rather
than sent, because no email provider is configured yet. The links in the log
are complete and work.

### Trying it with your own data

On the Upload screen:
1. Set the **period** (e.g. `2026-03`).
2. Drop a CSV or XLSX purchase register with columns: `vendor_name,
   vendor_gstin, invoice_number, invoice_date, taxable_value, cgst, sgst,
   igst, total_value` (case-insensitive; `cgst/sgst/igst/total_value`
   default to 0 if omitted).
3. Either upload a GSTR-2B JSON file (see `backend/sample_data/gstr2b_sample.json`
   for the expected shape) or click **"Load sample GSTR-2B data"**.
4. Click **Run reconciliation** to see the dashboard.

### Exporting

From the dashboard, click **Export exceptions (XLSX)** to download the
exception report (every non-matched row, color-coded by status) with the
same columns shown on screen.

## Local database

Development runs PostgreSQL in Docker, because it is what production runs and
the only place foreign keys, enum CHECK constraints and the append-only audit
trigger are actually enforced. SQLite hides problems in all three.

```bash
# from the project root, with Docker Desktop running
cp .env.example .env            # then set POSTGRES_PASSWORD in it
docker compose up -d postgres
```

Then point the backend at it, using the same password, in `backend/.env`:

```
DATABASE_URL=postgresql+psycopg://gstr2b:<password>@127.0.0.1:5433/gstr2b
```

Two details that are easy to trip over:

- It is published on port **5433**, not 5432, so it coexists with a PostgreSQL
  service installed natively on the machine. It is bound to `127.0.0.1` only.
- It creates a second database, `gstr2b_test`, that the test suite uses. The
  suite empties every table before each test, so it refuses to run against any
  database whose name does not contain `test`.

Redis, needed for the scheduled fetch, is not started by default because many
machines already run one. If yours does not, `docker compose --profile redis up
-d` starts one on port 6380.

If the API starts but reports it cannot reach the database, Docker Desktop has
almost certainly stopped. Start it, then `docker compose up -d postgres`.

To rebuild the demo data on a fresh or existing database:

```bash
cd backend
python scripts/seed.py          # drops everything, rebuilds from migrations
```

The seed refuses to run unless `ENVIRONMENT=development`, since it drops every
table.

### Running the tests against PostgreSQL

The default run uses SQLite and takes about half a minute. To run against the
container, which takes a few minutes but exercises the real constraints:

```bash
cd backend
# PowerShell:  $env:TEST_DATABASE_URL="postgresql+psycopg://gstr2b:<password>@127.0.0.1:5433/gstr2b_test"
TEST_DATABASE_URL=postgresql+psycopg://gstr2b:<password>@127.0.0.1:5433/gstr2b_test pytest
```

That also enables `tests/test_postgres_integrity.py`, which builds a scratch
database from the real migrations and checks the things `create_all()` cannot:
that migrations match the models, that an invalid enum value or a dangling
foreign key is rejected, that the audit log refuses UPDATE, DELETE and
TRUNCATE, and that every revision reverses cleanly.

## Configuration

All backend configuration is environment-driven — there are no hardcoded
secrets or connection strings. Settings are read from real environment
variables first, then `backend/.env`, then the defaults in
`app/config.py`. Copy `backend/.env.example` to `backend/.env` to start;
every variable is documented there.

The database is the one setting that must be chosen deliberately. Local
development uses the Docker PostgreSQL described under "Local database" above.
For staging or production, point `DATABASE_URL` at your managed PostgreSQL and
run the migrations:

```bash
export DATABASE_URL="postgresql+psycopg://user:pass@host:5432/gstr2b"
alembic upgrade head
```

SQLite remains available as a zero-setup fallback (leave `DATABASE_URL` unset),
but treat it as a convenience only: it does not enforce foreign keys, CHECK
constraints or the audit-log trigger.

`backend/.env` is gitignored. `backend/.env.example` is committed and is the
reference for what needs to be set in each environment.

## Database migrations

Alembic owns the schema. The application does **not** create tables at
startup, because auto-creating tables hides schema drift.

```bash
cd backend

alembic upgrade head            # apply all pending migrations
alembic current                 # show the applied revision
alembic revision --autogenerate -m "description"   # create a new migration
alembic downgrade -1            # roll back one revision
```

The migration environment reads `DATABASE_URL` from the same settings the
app uses, so `alembic.ini` holds no credentials. Batch mode is enabled for
SQLite, which cannot alter columns in place.

## A note on money columns

All rupee amounts are stored as `NUMERIC(14, 2)` and handled as
`decimal.Decimal`, not float, so that sums across thousands of invoices stay
exact. `app/money.py` defines the two conversion boundaries: values become
`Decimal` as they enter the database, and are converted to `float` inside the
matching engine, whose tolerance comparisons are against whole-rupee
thresholds. Mixing `Decimal` and `float` in arithmetic raises `TypeError`, so
new code touching amounts should use those helpers.

## Testing

```bash
cd backend
pytest tests/ -q                      # full suite
pytest tests/test_tenant_isolation.py # the non-negotiable ones
```

| Suite | Covers |
|---|---|
| `test_tenant_isolation.py` | Cross-firm access, at the data layer and over HTTP |
| `test_auth.py` | Signup, login, lockout, token rotation, replay detection, roles, audit chain |
| `test_workflow.py` | Upload, reconcile, resolve, approve, re-run carry-forward, export |
| `test_matching_engine.py` | Every match status and tolerance boundary |
| `test_validation.py` | GSTIN checksum, PAN, tax split, dates, row parsing |
| `test_notice_components.py` | Encrypted storage, PDF and OCR extraction, both analysers, grounding check |
| `test_notice_workflow.py` | Upload, drafting from real records, the approval chain, separation of duties |
| `test_notice_alerts.py` | 7/3/1-day, due-today and overdue alerts, escalation, idempotence |
| `test_audit_chain.py` | Several audit entries per request still form one valid chain |

## Demo data

Ready-to-upload files live in `demo_data/`, regenerated with
`python scripts/make_demo_data.py` from `backend`.

| File | What it does |
|---|---|
| `purchase_register_2026-03.csv` | 36 valid invoices |
| `gstr2b_2026-03.json` | The matching GSTR-2B statement |
| `purchase_register_with_errors.csv` | Triggers one validation rule per row |
| `sample_notice_asmt10.pdf` | A digital scrutiny notice for the demo client (`scripts/make_demo_notice.py`) |
| `sample_notice_scanned.pdf` | A page with no text layer, to try the paste-the-text path |

The GSTR-2B file is issued for GSTIN `27AAAAA0000A1Z2`, the seeded demo
client. Uploading it against a different GSTIN is rejected on purpose,
because a statement belonging to another registration would reconcile
against nothing and look like a mass filing failure.

**If your own test file is rejected for invalid GSTINs**, that is expected:
the fifteenth character is a modulus-36 checksum, so invented identifiers
almost never pass. Repair the file rather than hand-editing it:

```bash
cd backend
python scripts/fix_gstin_checksums.py path/to/your.csv
```

That writes a corrected copy beside the original and lists every change.

## Not built yet

These are planned but deliberately absent, in roughly this order:

- Tally and Zoho Books connectors
- Reporting, trend charts and the PDF reconciliation certificate
- Razorpay billing (the data model exists; no payment processing)
- Platform admin panel
- Object storage, CI and deployment documentation

Still open, and blocking the work that depends on it: the SMS provider and the
S3-compatible storage provider. Each sits behind an interface, so choosing one means
writing an adapter rather than changing call sites.
