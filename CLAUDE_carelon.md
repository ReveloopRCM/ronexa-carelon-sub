# Ronexa — Intelligent Browser Automation Platform
# Reveloop Tech Systems
# Claude Code Implementation Plan — March 2026

---

## What Ronexa Does

Ronexa automates prior authorization submissions to payer portals. The
architecture is not RPA and not an AI agent navigating a browser. It is
a compiler-driven platform where:

- **Playwright** owns all browser interaction — login, WebForms DOM steps,
  and all ClinicalFacade API calls via in-browser fetch()
- **Intelligence** fires at three precise seams: READ (parse question from
  API response), DECIDE (LLM evaluates against clinical notes), HANDLE
  (recover from unexpected portal state)
- **PortalDNA** is a JSON descriptor that defines a portal's state machine.
  The compiler reads it and drives the submission. No portal-specific logic
  lives in the engine.
- **Restate** provides durable execution — every step journaled, crashes
  recover from last checkpoint, human review gates via awakeables
- **Reps work inside Ronexa's UI** — never the portal directly. Every
  question arrives pre-answered with evidence and confidence. Phase 1
  requires rep approval on every case. Phase 2 is a boolean switch.

---

## Clean Build — Everything From Scratch

This is a greenfield build. No code is carried over from the previous
prototype. Every module is written fresh with the correct architecture
from day one. No inherited bugs, no legacy patterns, no bias from
prior implementation choices.

### Prior Prototype — Reference Only

A previous prototype exists in apps/worker/. It validated key concepts
and produced HAR recordings that inform this build. The prototype code
is READ-ONLY REFERENCE — useful for understanding DOM selectors that
work, API call sequences that are proven, and Okta login flow details.
Do not copy-paste from it. Do not import from it. Build clean.

Reference knowledge extracted from the prototype:
```
REF  PlaywrightPortalSession     — proved in-page fetch() pattern works for API calls
REF  playwright_webforms_client  — validated DOM selectors for WebForms pages
REF  playwright_clinical_client  — confirmed 40+ ClinicalFacade endpoints + Akamai WAF retry
REF  submission_flow.py          — documented 17-step submission sequence
REF  portal/models.py            — confirmed Pydantic shapes for portal payloads
REF  portal/endpoints.py         — captured endpoint names + default request bodies
REF  config.py                   — proved multi-LLM failover (Anthropic → Gemini)
REF  mfa_resolver.py             — validated Graph API OTP retrieval for Carelon MFA
REF  playwright_login.py         — mapped Okta IDX login with PKCE OAuth2 + MFA flow
```

What gets built fresh in this project:
```
NEW  PlaywrightPortalSession     — clean session class, in-page fetch() API calls
NEW  WebForms client             — DOM automation with Behavior Engine wrapping every action
NEW  ClinicalFacade client       — all 40+ endpoints, Akamai WAF retry, 1s pacing
NEW  PortalDNA schema + compiler — declarative portal descriptor, no hardcoded logic
NEW  AnswerAccumulator           — full array management + backtrack protocol
NEW  Intelligence layer          — READ/DECIDE/HANDLE seams
NEW  PDF parser + Haiku vision   — pymupdf → PNG → Claude vision extraction
NEW  Sonnet evaluator            — question → TypedDecision with RAG context
NEW  RAG retrieval               — pgvector similarity search on outcome patterns
NEW  Restate durable workflow    — per-case workflow with awakeable rep review gates
NEW  Behavior Engine             — gaussian timing, Bezier mouse, per-char typing
NEW  Session pool                — per-NPI browser management with Redis
NEW  MFA resolver                — Graph API OTP retrieval (clean implementation)
NEW  Okta login                  — PKCE OAuth2 + MFA flow
NEW  Multi-LLM config            — Anthropic primary, Gemini failover
NEW  FastAPI backend             — all routes, DB layer, Alembic migrations
NEW  Next.js frontend            — upload, queue, case pages with shadcn/ui
NEW  Database schema             — PostgreSQL + pgvector, SQLAlchemy async
NEW  Excel ingest pipeline       — parse, filter, dedup
NEW  Outcome database            — RAG indexing + compounding data moat
```

---

## Local-First Development Strategy

Build everything local. Prove it works. Then deploy to Azure.

### Local Environment — docker-compose.yml

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg16
    ports: ["5432:5432"]
    environment:
      POSTGRES_DB: ronexa
      POSTGRES_USER: ronexa
      POSTGRES_PASSWORD: dev_password
    volumes:
      - pgdata:/var/lib/postgresql/data

  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]

  restate:
    image: docker.io/restatedev/restate:latest
    ports:
      - "8080:8080"   # ingress
      - "9070:9070"   # admin
    depends_on: [postgres]

volumes:
  pgdata:
```

### Local → Azure Migration Map

| Local                          | Azure Production                     |
|-------------------------------|--------------------------------------|
| docker postgres (pgvector)     | Azure PostgreSQL Flexible + pgvector |
| docker redis                   | Azure Redis Cache                    |
| docker restate                 | Restate on ACA                       |
| local filesystem profiles/     | Azure File Share /mnt/profiles/      |
| uvicorn (FastAPI)              | ACA worker container                 |
| next dev (frontend)            | ACA frontend container               |
| direct Playwright launch       | Playwright in ACA with Xvfb          |
| .env file                      | Azure Key Vault                      |
| direct function call           | Azure Service Bus queue              |

### Local Dev Startup Sequence

```bash
# 1. Infrastructure
docker compose up -d

# 2. Database
cd backend && alembic upgrade head

# 3. Register Restate service
cd backend && restate dep register http://localhost:9080

# 4. Backend API
cd backend && uvicorn main:app --reload --port 8000

# 5. Frontend
cd frontend && npm run dev

# 6. Browser profiles (local)
mkdir -p profiles/  # Chromium profiles stored here locally
```

### Environment Variables — Local Dev (.env)

```
# Database (local docker)
DATABASE_URL=postgresql+asyncpg://ronexa:dev_password@localhost:5432/ronexa

# Redis (local docker)
REDIS_URL=redis://localhost:6379

# Restate (local docker)
RESTATE_URL=http://localhost:8080
RESTATE_ADMIN_URL=http://localhost:9070

# Portal credentials
CARELON_USERNAME=envisionradiology.tracy@gmail.com
CARELON_PASSWORD=<rotated>

# Okta (static from HAR)
OKTA_CLIENT_ID=0oa97idukvkwwIVul4h7
OKTA_DOMAIN=login.mbm.partners.carelon.com

# MFA — Graph API
GRAPH_TENANT_ID=<existing>
GRAPH_CLIENT_ID=<existing>
GRAPH_CLIENT_SECRET=<existing>
GRAPH_MAILBOX=<OTP inbox>

# LLM
ANTHROPIC_API_KEY=<key>
OPENAI_API_KEY=<key — for text-embedding-3-small>
GOOGLE_API_KEY=<key — Gemini failover>

# Local paths
BROWSER_PROFILES_DIR=./profiles
PORTALS_DIR=./portals

# Worker (local: single NPI for testing)
CENTER_NPI=<test center npi>
PORTAL_ID=carelon_provider_portal

# Environment
ENVIRONMENT=local

# Phase 1 flag
REQUIRE_REP_REVIEW=true
```

---

## Architecture — Read Before Writing Any Code

### The Execution Model

```
Excel upload → cases created → PDFs uploaded → Haiku extracts notes
     ↓
For each case:
  Restate workflow starts
  Worker acquires Playwright session (keyed by CenterNPI)
  PortalCompiler reads carelon_provider_portal.json
  Compiler executes phases:
    EXAM_SETUP → EXAM_PROCESSING → DIAGNOSIS → PATHWAY → CLINICAL_TREE → SUBMISSION

  In CLINICAL_TREE (the question loop):
    loop:
      Call GetPathwayAssetsWithValidation with full accumulated answer array
      Parse returned questions (READ seam — from API response, not DOM)
      For each new question group:
        Sonnet evaluates against clinical notes + RAG retrieval (DECIDE seam)
        Restate awakeable suspends workflow
        Rep sees question in Ronexa UI — approves or edits
        Awakeable resolves — loop continues
    until no new question groups returned

  Compiler executes SUBMISSION phase
  Auth number captured → written to DB → indexed to OutcomePatterns (RAG)
```

### The In-Browser Fetch Pattern — Critical

ALL ClinicalFacade API calls run via fetch() inside the live Playwright page.
There is NO httpx client. NO external HTTP from Python to the portal.

```python
# This is how session.api() works — do not change this pattern
result = await self.page.evaluate(f"""
    async () => {{
        const resp = await fetch('/ClinicalFacade.aspx/{method}', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json',
                       'X-Requested-With': 'XMLHttpRequest'}},
            body: JSON.stringify({payload_json}),
        }});
        return {{ ok: resp.ok, status: resp.status, body: await resp.text() }};
    }}
""")
```

Akamai never sees the API traffic because fetch() runs inside the trusted
browser context that Akamai already fingerprinted during login.

### HAR-Confirmed Question Loop Rules — Never Violate

```
RULE 1: Send FULL accumulated answer array on every call — not just new answer
RULE 2: Loop ends when response returns no new GroupIds not in accumulator
        DoneWithExam is called AFTER the loop ends, not as its signal
RULE 3: Type 2=date, Type 3=single-select, Type 4=multi-select (array of UUIDs)
        Type unknown until server returns question
        HAR2: 3 of 5 questions are Type 4 for shoulder pathway
RULE 4: Backtrack = call DeleteAssetsByGroupId for every GroupId > changed GroupId
        then remove downstream from AnswerAccumulator, resume from changed GroupId
        HAR2: 12 DeleteAssetsByGroupId calls in one submission — backtracking is normal
RULE 5: NO sleep() in question loop. LLM latency IS the authentic think time.
        HAR human averages: 24.5s/question (HAR1), 47.3s/question (HAR2)
RULE 6: QuestionIds NEVER hardcoded. Zero overlap between CPT/ICD combos.
RULE 7: ProviderID and ClientID from portal session context — differ per provider
        HAR1: 106818278/200   HAR2: 103564762/55
```

### Behavior Engine Rules — Never Violate

```
Timing: GAUSSIAN distribution (not uniform random):
  pageLoad:         mu=2000ms sigma=400ms
  formField:        mu=550ms  sigma=125ms
  buttonClick:      mu=400ms  sigma=100ms
  searchResult:     mu=1400ms sigma=300ms
  clinicalQuestion: LLM_LATENCY — no artificial delay

Mouse:  Bezier curve paths, 15-25 steps, randomized control points
        Click target offset [30%-70%] x/y — never dead-center
Typing: Per-character, 60-100 WPM, gaussian inter-keystroke (mu=130ms sigma=30ms)
Profile: Local: ./profiles/{centerNpi}
         Azure: Azure File Share /mnt/profiles/{centerNpi}
         Persistent Chromium profile per NPI — same Akamai identity always
```

---

## Tech Stack

```
Language:        Python 3.12 (backend), TypeScript (frontend)
Frontend:        Next.js 14 App Router, Tailwind CSS, shadcn/ui
Backend:         FastAPI
Workflow:        Restate (Python SDK)
LLM Extraction:  claude-haiku-4-5-20251001  (PDF → structured JSON, once per upload)
LLM Evaluation:  claude-sonnet-4-6  (question → TypedDecision, per question group)
LLM Backup:      gemini-2.5-flash (Google) — failover when Anthropic unavailable
                 Build multi-provider config with automatic failover
Embeddings:      text-embedding-3-small (1536 dim, for pgvector RAG)
Database:        PostgreSQL 16 + pgvector extension
ORM:             SQLAlchemy async + Alembic migrations
PDF:             pymupdf (fitz) — PDF → PNG image extraction
                 NO pdfplumber. Every clinical PDF from this RIS is
                 image-only (iTextSharp wrapper). pdfplumber returns
                 zero text on all documents.
Browser:         Playwright Python async — ALL portal interaction
MFA:             Microsoft Graph API — OTP retrieval for Carelon MFA
Session cache:   Redis (local docker / Azure Redis Cache)
Queue:           Direct function call local / Azure Service Bus production
Infra:           docker-compose local / Azure Container Apps + KEDA production
Secrets:         .env local / Azure Key Vault production
Monitoring:      console logging local / Azure Monitor + App Insights production
Portal config:   /portals/carelon_provider_portal.json (PortalDNA descriptor)
```

---

## Input File — Excel Schema

File: Carelon_YYYY-MM-DD.xlsx, Sheet: Sheet1, 50 columns.
Real sample: 115 rows → 66 unique ExamIds after dedup (49 exact duplicate rows).
Client has 104 processing locations (CenterNPIs). Sample shows 20 NPIs.

### Filtering — only process rows where ALL are true:
  AuthProvider == "Carelon"
  PortalMatch  == "Carelon"
  AuthIsResolved == 0         (integer, not string)
  authstatedesc == "Needs Auth"
  ExamId not already in DB with terminal state

### Dedup
ExamId is the unique key. The RIS sends exact duplicate rows (identical
across all 50 columns). First occurrence wins. In sample: 115 rows → 66
unique after dedup. This is normal — not a bug, just how the RIS exports.

### NULL Handling
NULL_VALUES = {"NULL", "null", "", None, 0} — context-dependent:
  String "NULL" from Excel → None (ICD codes, names)
  0 for AttachmentId → treated as missing (no clinical notes attached)
  NaN/blank for ReferringNPI → validation flag

### Processing Readiness — Classification on Ingest

Important: The portal decides the pathway, not us. Some CPT/carrier combos
auto-approve without requiring ICD codes or a clinical pathway at all.
Missing ICD1 is a WARNING, not a blocker — the case may still process fine.
Never hard-block a case from processing based on missing clinical fields.

After parse + dedup, cases get one of three initial states:

  PENDING_STAT:   IsStat=YES — urgent cases, processing immediately.
                  3/66 in sample (5%). Sort priority 1.

  PENDING_NOTES:  Default state for all non-urgent parsed cases.
                  Clinical notes may or may not be required — depends on
                  whether the portal routes to a clinical pathway or
                  auto-approves. The rep decides when to process.
                  Upload UI (Task 14) handles per-case PDF upload.
                  Sort priority 2 (has RIS doc) or 3 (no RIS doc).

  HOLD:           Missing fields required for member search — cannot
                  search for the patient on the portal at all.
                  Missing: FirstName, LastName, dob, policynum, CenterNPI.
                  Sort priority 10. Rare — 0 cases in sample hit this.

Data quality flags (informational, stored in raw_data['_flags']):
  - no_icd:       Missing ICD1 (4/66) — portal may still auto-approve
  - no_referring:  Missing ReferringNPI (4/66) — rep may handle manually
  These are surfaced in the UI but NEVER block processing.

### Name Casing
Patient names arrive in inconsistent casing from the RIS:
  ALL CAPS:   "JOSE HERNANDEZ" (13 cases in sample)
  Title Case: "Amanda Smith" (53 cases in sample)
Normalize to Title Case for portal member search: "Jose Hernandez"

### Key Columns (50 total, these are the ones that matter)

Portal member search requires:
  FirstName         Patient first name (normalize casing)
  LastName          Patient last name (normalize casing)
  policynum         Insurance member/policy ID
  dob               Patient DOB (YYYY-MM-DD string)
  ReferringNPI      Referring provider NPI (float64 — cast to int, 4 nulls in sample)

Session routing:
  CenterNPI         Session pool key — one Playwright browser per NPI (int64)
  CenterAbbr        e.g. RKW, LAC, SNY, FLM (human-readable center code)

Exam setup:
  ExamId            Dedup key (unique=True in DB, int64)
  cptcode           CPT code (int64, e.g. 73721, 74177, 72148)
  CPTDesc           Human-readable CPT description
  ICD1–ICD5         Primary + secondary diagnoses (ICD5 always empty in sample)
                    ICD1 missing = warning, not blocker — portal may auto-approve
  CarrierId         Payer carrier ID (int64)
  CarrierDesc       Payer name (e.g. "Blue Cross Blue Shield")
  FinancialClass    COMMERCIAL or MEDICARE (3 Medicare Advantage cases in sample)

Clinical notes:
  AttachmentId      Attachment identifier (float64 — 0 or null = no notes yet)
  FileKey           Clinical note file key for retrieval

Metadata (stored but not used in portal):
  EncounterId       Encounter ID from RIS
  OrderRequestID    Order request ID from RIS
  CreatedDate       When the auth request was created
  ScheduledDateTime Scheduled exam date (only 33/115 populated)
  CenterDesc        Full center name
  CenterAddress     Center street address
  CenterState       Always "TEXAS" in current client
  CenterTaxId       Center tax ID
  RowType           Always "Single-Code" in sample
  Environment       Always "PRODUCTION"
  PortalHitCount    Always 0 (unused)
  AuthExamId        Secondary exam ID
  PatientZipCode    Patient zip code

Columns always empty in sample (store as nullable):
  ICD5, AuthStateSubDesc, LastAuthUserName, LastAuthNote, AllAuthNotes,
  PrimaryInsuranceAuth, PrimaryAuthTrackingNum, AuthExpirationDate,
  CancellationReasonId

### Batch Sorting

Ingest sort (what the rep sees in upload preview):
  Priority 1: PENDING_STAT (IsStat=YES) — urgent
  Priority 2: PENDING_NOTES + has RIS attachment
  Priority 3: PENDING_NOTES + no RIS attachment
  Priority 10: HOLD
  Within each: ScheduledDateTime ASC → CreatedDate ASC

Processing sort (when worker picks up cases):
  Cases grouped by CenterNPI for session efficiency —
  all cases for NPI X run through the same browser session.
  Within each NPI: sort_priority ASC → CreatedDate ASC.
  This minimizes session creation — one login per NPI per batch.

---

## Directory Structure

```
ronexa/
├── portals/
│   ├── carelon_provider_portal.json     HAR-validated Carelon PortalDNA
│   └── PORTAL_SCHEMA.md                Onboarding guide for new portals
│
├── backend/
│   ├── main.py                          FastAPI app + Restate service registration
│   ├── requirements.txt
│   ├── app/
│   │   ├── api/routes/
│   │   │   ├── uploads.py               Excel + PDF upload endpoints
│   │   │   ├── cases.py                 Case CRUD + status
│   │   │   ├── queue.py                 Rep review queue
│   │   │   └── sessions.py              Session pool status
│   │   │
│   │   ├── compiler/
│   │   │   ├── portal_dna.py            Pydantic models for PortalDNA schema
│   │   │   └── portal_compiler.py       PortalCompiler — reads DNA, drives submission
│   │   │
│   │   ├── auth/
│   │   │   ├── mfa_resolver.py          Graph API OTP retrieval for Carelon MFA
│   │   │   ├── okta_login.py            Okta IDX login with PKCE OAuth2 + MFA
│   │   │   └── config.py               Multi-LLM provider config + failover
│   │   │
│   │   ├── portal/
│   │   │   ├── session.py               PlaywrightPortalSession + in-page fetch()
│   │   │   ├── webforms_client.py       WebForms DOM automation
│   │   │   ├── clinical_client.py       ClinicalFacade API calls (40+ endpoints)
│   │   │   ├── session_pool.py          Per-NPI session management
│   │   │   └── behavior_engine.py       Gaussian timing, Bezier mouse, char typing
│   │   │
│   │   ├── intelligence/
│   │   │   ├── extractor.py             Haiku vision: PNG images → ClinicalContext
│   │   │   ├── evaluator.py             Sonnet: PortalObservation → TypedDecision
│   │   │   ├── rag.py                   pgvector similarity retrieval
│   │   │   ├── prompts.py               All prompt templates
│   │   │   └── models.py                PortalObservation, TypedDecision, ClinicalContext
│   │   │
│   │   ├── accumulator/
│   │   │   └── answer_accumulator.py    AnswerAccumulator + backtrack protocol
│   │   │
│   │   ├── workflow/
│   │   │   └── prior_auth_workflow.py   Restate durable workflow (one per case)
│   │   │
│   │   ├── ingest/
│   │   │   ├── excel_parser.py          Parse + dedup + filter Excel
│   │   │   └── pdf_parser.py            pymupdf: PDF → PNG images → LLM vision
│   │   │
│   │   └── db/
│   │       ├── database.py              Async engine + session factory
│   │       ├── models.py                SQLAlchemy models
│   │       ├── repositories.py          DB access layer
│   │       └── outcome_db.py            OutcomePattern indexing + pgvector queries
│   │
│   └── alembic/                         DB migrations
│
├── frontend/                            Next.js 14 App Router (fresh build)
│   ├── app/
│   │   ├── upload/page.tsx              3-step: Excel → PDFs → Process
│   │   ├── cases/page.tsx               All cases + state
│   │   ├── cases/[caseId]/page.tsx      Case detail + audit trail
│   │   └── queue/
│   │       ├── page.tsx                 Rep review queue list
│   │       └── [caseId]/page.tsx        Inline review UI
│   └── components/
│       ├── upload/
│       │   ├── ExcelDropzone.tsx
│       │   ├── CasePreviewTable.tsx
│       │   └── PdfUploader.tsx
│       └── queue/
│           ├── QuestionAnswerCard.tsx
│           ├── ConfidenceBar.tsx
│           └── AnswerEditor.tsx
│
├── profiles/                            Local Chromium profiles (gitignored)
│
├── infrastructure/
│   ├── container-app.yaml              ACA worker + KEDA scaling
│   └── docker-compose.yml              Local dev (postgres + redis + restate)
│
└── scripts/
    ├── har_analyzer.py                 HAR analysis tool for new portal onboarding
    └── seed_test_case.py               Create a test case for local E2E testing
```

---

## Build Order

```
WEEK 1 — Foundation (Local)
  Task 1   docker-compose + database schema (SQLAlchemy + pgvector)
  Task 2   Excel ingest pipeline
  Task 3   PortalDNA Pydantic models
  Task 4   AnswerAccumulator

WEEK 2 — Portal Layer (Local)
  Task 5   Behavior Engine
  Task 6   Session pool (per-NPI, Redis)
  Task 7   PortalCompiler (phase executor + seam injector)

WEEK 3 — Intelligence Layer (Local)
  Task 8   PDF parser + Haiku vision extractor
  Task 9   Sonnet evaluator (question → TypedDecision)
  Task 10  RAG retrieval (pgvector)

WEEK 4 — Workflow + API (Local)
  Task 11  Restate durable workflow
  Task 12  FastAPI routes

WEEK 5 — Frontend (Local)
  Task 13  Rep review queue UI
  Task 14  Excel upload + case preview UI
  Task 15  Case list + detail pages

WEEK 6 — Local End-to-End Testing
  Task 16  Single case E2E (local Playwright → live Carelon → rep review → submit)
  Task 17  Outcome database + RAG indexing

WEEK 7 — Azure Deployment
  Task 18  ACA worker container + Service Bus
  Task 19  Azure PostgreSQL + Redis + File Share + Key Vault
  Task 20  Production hardening + monitoring

WEEK 8 — Production validation on real batch
```

---

## TASK 1 — Docker Compose + Database Schema

Files:
  infrastructure/docker-compose.yml
  backend/app/db/database.py
  backend/app/db/models.py
  alembic migrations

Enable pgvector: `op.execute("CREATE EXTENSION IF NOT EXISTS vector")`

SQLAlchemy async only. No Prisma.

Models:
  Batch         — one per Excel upload
  Case          — one per unique ExamId (single model through full lifecycle)
  ClinicalNote  — one or more per case (PDFs)
  Question      — one per question group in CLINICAL_TREE loop
  AuditEvent    — every state change + rep action
  OutcomePattern — RAG retrieval table (grows with every submission)

### CaseState enum (full lifecycle — ingest through outcome)

```
  PENDING_NOTES   Ingested, waiting for clinical upload via UI
  PENDING_STAT    IsStat=YES — expedite (same as PENDING_NOTES but priority)
  HOLD            Missing required member search field — cannot process
  NOTES_UPLOADED  PDF attached, ready to process
  PROCESSING      Restate workflow running
  IN_REVIEW       Waiting for rep to approve question (awakeable suspended)
  SUBMITTING      Portal submission in progress
  APPROVED        Auth number captured
  DENIED          Denial received
  PENDED          Portal pended — needs more info
  FAILED          Submission error — retryable
```

No READY_TO_SUBMIT or SUBMITTED — unnecessary intermediate states.
Case goes directly from IN_REVIEW (last question approved) → SUBMITTING → terminal.

### Case model — Single Model, Full Lifecycle, Hybrid Schema

One record per ExamId. Created at ingest, updated through processing, carries
outcome. Structured columns for fields the code touches. JSONB for everything else.

```
Batch:
  id               String PK
  filename         String
  uploaded_at      DateTime
  uploaded_by      String
  total_rows       Integer         raw row count before dedup
  duplicate_rows   Integer         rows removed
  unique_cases     Integer         final count
  stat_count       Integer         IsStat=YES cases
  hold_count       Integer         cases missing member search fields
  source           String          "excel" | "api" (future-proofing)

Case:
  id               String PK
  batch_id         FK → Batch

  # ── Dedup key ─────────────────────────────────────────────────
  exam_id          String unique indexed    dedup key (from ExamId)

  # ── Member search (used directly in portal lookup) ────────────
  first_name       String not null          normalized Title Case
  last_name        String not null          normalized Title Case
  dob              String not null          YYYY-MM-DD (portal expects string)
  policy_num       String not null          insurance member/policy ID
  patient_zip      String nullable          fallback search field

  # ── Session pool key ──────────────────────────────────────────
  center_npi       String not null indexed  one Playwright browser per NPI
  center_abbr      String nullable          for display (e.g. RKW, LAC, FLM)

  # ── Portal submission fields ──────────────────────────────────
  cpt_code         String not null          for exam setup
  icd1             String nullable          portal handles missing ICD
  icd2             String nullable
  icd3             String nullable
  icd4             String nullable
  icd5             String nullable
  referring_npi    String nullable          flagged but not blocking
  carrier_id       String indexed           RAG pre-filter + routing

  # ── Clinical attachment ───────────────────────────────────────
  file_key         String nullable          RIS attachment slot (always present)
  attachment_id    String nullable          >0 means doc already in RIS

  # ── Priority / routing ────────────────────────────────────────
  is_stat          Boolean default=False    IsStat=YES from Excel
  scheduled_dt     DateTime nullable        for secondary sort (29% populated)

  # ── RIS back-reference ────────────────────────────────────────
  auth_exam_id     String nullable          write auth result back to RIS
  order_request_id String nullable

  # ── State + classification ────────────────────────────────────
  state            CaseState indexed        full lifecycle state
  sort_priority    Integer                  1=STAT, 2=has RIS doc, 3=no doc, 10=HOLD
  hold_reason      String nullable          human-readable if HOLD

  # ── Portal session context (populated at processing time) ─────
  portal_provider_id  String nullable       from session — differs per provider
  portal_client_id    String nullable       from session — differs per provider

  # ── Processing results ────────────────────────────────────────
  portal_case_id   String nullable          portal's internal case reference
  auth_number      String nullable          captured on APPROVED
  denial_reason    Text nullable            captured on DENIED
  pend_reason      Text nullable            captured on PENDED

  # ── EVERYTHING ELSE from the Excel row ────────────────────────
  raw_data         JSONB default={}         full original row — ALL columns
                                            Future columns land here automatically.
                                            Parser stores _flags here too.
                                            Access: case.raw_data.get('CarrierDesc')
                                            Queryable via JSONB operators.

  # ── Timestamps ────────────────────────────────────────────────
  ingested_at      DateTime default=now
  updated_at       DateTime default=now onupdate=now
  submitted_at     DateTime nullable        when portal submission completed
```

### Why String types for IDs

All ID fields (exam_id, center_npi, carrier_id, referring_npi) are String,
not Integer. Reasons:
  - NPI "0123456789" won't lose leading zero
  - No float-to-int casting surprises from pandas (ReferringNPI comes as float64)
  - Future portals may use non-numeric IDs
  - PostgreSQL indexes String columns efficiently

### JSONB column — dynamic schema for everything else

The Excel file has 50 columns today. Carelon will add more. A second client's
file will have different columns. We cannot maintain a rigid model with 50
named columns.

Pattern: structured columns ONLY for fields actively used in code (~22).
Everything else goes into raw_data JSONB (~28+ columns).

When code needs to access a raw_data field: `case.raw_data.get('CarrierDesc')`
If a field graduates to being used in code, promote it to a structured column
via Alembic migration + backfill from raw_data. Until then, it lives in JSONB.
Nothing from the Excel row is ever lost.

### Supporting models

ReviewState enum (per Question):
  PENDING, AI_SUGGESTED, REP_APPROVED, REP_EDITED, FLAGGED

Question:
  id                 String PK
  case_id            FK → Case
  portal_question_id String           from portal response — never hardcoded
  group_id           Integer          GroupId from portal
  sequence           Integer          order within submission
  question_type      Integer          2=date, 3=single, 4=multi
  question_text      Text
  options_json       JSON             [{id, text}] — portal options
  ai_answer          JSON             LLM proposed answer
  ai_confidence      Float            0-100 confidence score
  ai_evidence        Text nullable    quote from clinical notes
  ai_reasoning       Text nullable    one-sentence explanation
  ai_gap             Text nullable    description of missing documentation
  review_state       ReviewState
  rep_answer         JSON nullable    rep's answer if they edited
  reviewed_by        String nullable  rep identifier
  reviewed_at        DateTime nullable
  awakeable_id       String nullable  Restate awakeable ID for this question's gate
  created_at         DateTime

ClinicalNote:
  id                 String PK
  case_id            FK → Case
  filename           String           original upload filename
  page_count         Integer          number of pages extracted
  document_type      String           EHR_PRINTOUT | FAX | SCAN | UNKNOWN
  document_quality   String           CLEAN | DEGRADED | PARTIAL
  extraction_method  String           always "haiku_vision" in this pipeline
  structured         JSON             output from Haiku vision extractor
  uploaded_at        DateTime

AuditEvent:
  id                 String PK
  case_id            FK → Case
  actor              String           "system" | "rep:{id}"
  action             String           state change, rep action, etc.
  data               JSON             action-specific payload
  timestamp          DateTime

OutcomePattern (RAG retrieval — grows with every submission):
  id                 String PK
  case_id            FK → Case
  portal_id          String indexed   which portal
  cpt_code           String indexed   pre-filter before vector search
  icd1               String indexed   pre-filter before vector search
  carrier_id         String indexed   pre-filter before vector search
  question_text      Text
  question_type      Integer
  options_json       JSON
  question_embedding Vector(1536)     pgvector column — semantic RAG
  answer_value       JSON             the answer that was submitted
  answer_text        String           human-readable answer
  evidence_text      Text nullable    clinical evidence used
  was_rep_override   Boolean          True if rep changed AI answer
  outcome            String           APPROVED/DENIED/PENDED
  denial_reason      Text nullable    most valuable signal for denial prediction
  created_at         DateTime

---

## TASK 2 — Excel Ingest Pipeline

Files:
  backend/app/ingest/excel_parser.py
  backend/app/db/models.py (Case model from Task 1)

### What this pipeline does

One job: take a raw Carelon Excel file, clean it, classify it, sort it,
and return structured data so the API route can persist it to the DB.
Reps then see it in the UI and can upload clinical notes per case.

No portal interaction. No LLM calls. No queue enqueuing.
Clinical documents are NOT part of this pipeline.

### What the real data revealed

Analyzed from Carelon_2026-03-13 (115 rows, 50 columns, 20 NPIs):
```
115 rows → 66 unique cases
49 rows are exact byte-for-byte duplicates — RIS export artifact
All 115 rows are already Carelon/Needs Auth — file comes pre-filtered
Field completeness on 66 unique cases:
  FirstName, LastName, dob, policynum:  100% present — always
  FileKey:                              100% present — always
  CenterNPI:                            100% present — always
  ReferringNPI:                         94%  (4 cases missing)
  ICD codes:                            94%  (4 cases have no ICD at all)
  AttachmentId > 0:                     50%  (33 cases have RIS clinical doc)
  ScheduledDateTime:                    29%  (19 of 66 cases)
  IsStat=YES:                            5%  (3 urgent cases)
Client has 104 locations. This file had 20 NPIs.
Production batches will regularly span 40-60+ NPIs.
Carelon will add/change columns over time. DB handles this via JSONB.
```

### On missing ICD codes — NOT a blocker

Missing ICD does NOT mean the case cannot be submitted. Some CPTs
auto-approve on Carelon without clinical questions (e.g. CT Lung Cancer
Screening — screening protocol, no pathway questions). Others go through
a pathway that the portal initiates based on CPT alone. The portal
decides what happens — not us. Every case with the required member
search fields gets processed. Missing ICD is flagged as a data quality
note, not a hold.

### Pipeline Steps

1. LOAD:     Read Excel Sheet1 with openpyxl
2. DEDUP:    Drop exact duplicate rows on ExamId (first wins)
3. MAP:      Split each row into structured fields + raw_data JSONB
4. CLASSIFY: Assign state + sort_priority based on field completeness
5. SORT:     Priority → ScheduledDateTime → CreatedDate
6. RETURN:   ParsedBatch (no DB write — route handles persistence)

### Structured/JSONB Split

The file has 50 columns today. Carelon will add more. A second client's
file will have different columns.

```
Structured fields (~22):  Fields the code actively touches —
                          member search, session routing, portal
                          submission, priority logic, RIS back-ref.
                          These become typed columns on the Case model.

raw_data JSONB (~28+):    Every other column from the Excel row,
                          stored verbatim. Future columns land here
                          automatically. Parser never needs updating
                          for new columns. Nothing is ever lost.
```

### Data Cleaning Rules

```python
NULL_VALUES = {"NULL", "null", "", None}

# All IDs stored as String — no int casting, no float surprises
# ExamId, CenterNPI, CarrierId, ReferringNPI, AttachmentId → str

# Name normalization
FirstName:     str.strip().title()   "JOSE" → "Jose", "amanda" → "Amanda"
LastName:      str.strip().title()   "HERNANDEZ" → "Hernandez"

# Date fields kept as string — portal expects string
dob:           str (YYYY-MM-DD)

# ICD codes: string "NULL" → None, strip whitespace
ICD1–ICD5:     str or None

# Boolean fields
IsStat:        str.upper() == "YES" → True
```

### Classification Logic

```python
def _classify(case):
    """
    Assign state and sort_priority.
    Missing ICD is NOT a hold — portal handles it.
    Missing ReferringNPI is NOT a hold — flag it, rep handles in portal.
    HOLD only when we literally cannot search for the patient.
    """
    has_member_search = all([
        case.first_name, case.last_name, case.dob,
        case.policy_num, case.center_npi
    ])

    if not has_member_search:
        → state = HOLD, sort_priority = 10
        → hold_reason = "Missing required fields: ..."

    elif case.is_stat:
        → state = PENDING_STAT, sort_priority = 1

    else:
        → state = PENDING_NOTES
        → sort_priority = 2 if has_ris_attachment else 3

    # Data quality flags stored in raw_data['_flags']
    # Prepend _ to mark as system-generated
    flags = []
    if not icd1:  flags.append("no_icd")
    if not referring_npi:  flags.append("no_referring")
```

### Sort Order

```
Priority 1:  PENDING_STAT (IsStat=YES) — urgent by definition
Priority 2:  PENDING_NOTES + has RIS attachment — clinical already available
Priority 3:  PENDING_NOTES + no RIS attachment — needs clinical upload first
Priority 10: HOLD — cannot process until missing fields resolved

Within each priority:
  Secondary:  ScheduledDateTime ASC (soonest appointment first)
  Tertiary:   CreatedDate ASC (oldest order first — FIFO)
```

### Return Object

```python
@dataclass
class ParsedBatch:
    filename: str
    total_rows: int           # raw Excel rows (115 in sample)
    duplicate_rows: int       # exact dupes removed (49 in sample)
    unique_cases: int         # final case count (66 in sample)
    stat_count: int           # IsStat=YES cases
    hold_count: int           # cases missing member search fields
    cases: list[dict]         # sorted Case field dicts, ready for DB insert
```

Parser returns list of dicts — each dict maps directly to Case model columns.
The API route bulk-inserts Batch + Case records. No intermediate ParsedCase
object — the dict IS the Case record, structured fields + raw_data included.

### Key Rules
- Parser is a pure function: Excel bytes in → ParsedBatch out
- Do not write to DB in the parser — return data, route handles persistence
- All ID fields are String (no int casting)
- raw_data captures EVERY column from the row — nothing is discarded
- raw_data['_flags'] stores data quality flags (system-generated)
- Sort by priority first, then scheduled date, then created date
- Same _map() + _classify() logic works for future API input (Phase 2)

### What the rep sees after upload

```
Batch: Carelon_2026-03-13.xlsx
  115 rows → 66 unique cases (49 duplicates removed)
  ⚡  3  STAT  — processing immediately
  ✅ 63  Ready — waiting for clinical documents
  ⚠️   0  On hold

Cases (sorted):
  [STAT]  17073678  Juan Catano     74178 CT Urography    SAR  📅 scheduled
  [STAT]  17074309  ...
  [READY] 16593446  Alfredo Espinosa 71271 CT Lung LDCT   LAC  📎 RIS doc  ⚠️ no ICD
  [READY] 17065853  ...                                        📎 RIS doc
  [READY] 16962789  Ondria Wells    70486 CT Sinus WO     LAC
  ...

⚠️ flags (informational — not blocking):
  7 cases have no ICD code  → portal may auto-approve or use CPT pathway
  4 cases missing ReferringNPI → rep may need to handle provider lookup
```

### Transition to API input (Phase 2)

When the client moves from Excel uploads to API calls, the same
Case model and classification logic applies. The API endpoint receives
a list of case records in JSON, maps them through _map() and _classify(),
and writes to the same DB table. raw_data JSONB handles the field
flexibility — API payload can include any fields, structured ones get
mapped, everything else lands in raw_data. No schema migration needed.

---

## TASK 3 — PortalDNA Pydantic Models

File: backend/app/compiler/portal_dna.py

Models: MFAConfig, PostLoginStep, AuthProfile, PhaseStep,
        StateMachine, NavigationPhase, RAGConfig, DecideSeam,
        IntelligenceSeams, BotDetectionProfile, PortalDNA

PortalDNA.load(path) classmethod loads from JSON file.
PortalDNA.get_phase(phase_id) returns NavigationPhase by id.

NavigationPhase.type literals:
  "API_SEQUENCE"           — ordered API calls
  "WEBFORM"                — Playwright DOM interactions
  "RECURSIVE_STATE_MACHINE" — CLINICAL_TREE question loop

---

## TASK 4 — AnswerAccumulator

File: backend/app/accumulator/answer_accumulator.py

Methods:
  add(answer)                      Add or replace answer for this GroupId
  change(answer, session, endpoint) Backtrack: DeleteAssetsByGroupId for
                                    all downstream groups, then add new answer
  get_new_groups(response_questions) Returns questions not yet in accumulator
  has_group(group_id)              Check if GroupId already answered
  payload (property)               Full sorted array for API submission
  count (property)                 Number of answered groups

The payload property is critical — returns FULL array every time.
Never return just the new answer. Always the full accumulated history.

Serializable: must be JSON-serializable for Restate journal persistence.

---

## TASK 5 — Behavior Engine

File: backend/app/portal/behavior_engine.py

Methods:
  think(action_type)               Async gaussian delay for nav actions
  click(page, selector, action)    Think + bezier move + click
  type_text(page, selector, text)  Click field + per-character typing
  _bezier_move(page, tx, ty)       Cubic bezier mouse path to target

THINK_TIME_MS dict maps action_type → (mu, sigma).
clinicalQuestion deliberately ABSENT — LLM provides authentic latency.
Clamp gaussian to 2σ range to avoid extreme outliers.

---

## TASK 6 — Session Pool

File: backend/app/portal/session_pool.py

Keyed by CenterNPI. One live Playwright browser per NPI.
Redis stores session metadata (created_at, case_count).
Local dev: profiles/ directory. Azure: File Share /mnt/profiles/{centerNpi}.

Session lifecycle:
  get_session(center_npi, credentials)
    → check Redis for existing session
    → if valid (age < 8hr AND case_count < 80): restore and validate
    → if invalid: create fresh (full Playwright login + Graph API MFA)
  increment(center_npi)   called after each case completes
  invalidate(center_npi)  called on SessionExpiredException

Validation: call GetClientMessages — if response is not None, session is live.

Per-NPI asyncio.Lock prevents concurrent Playwright launches for same NPI.

Local dev note: Single NPI, single browser. No complex pool needed —
use local Redis from docker-compose. Profile directory is ./profiles/{npi}/.

---

## TASK 7 — PortalCompiler

File: backend/app/compiler/portal_compiler.py

PortalCompiler(dna: PortalDNA)
  execute(case, session, clinical_context, restate_ctx) → dict
    Iterates navigationPhases, dispatches by type
  _run_api_sequence(phase, case, session) → dict
    Executes ordered API calls via session.api()
    Handles onEmpty conditions (HANDLE seam)
    Captures output fields (auth number, denial reason)
  _run_webform(phase, case, session) → dict
    Playwright DOM interactions for WebForms pages
    Uses behavior_engine for all clicks and typing
  _run_question_loop(phase, case, session, clinical_context, restate_ctx) → dict
    CLINICAL_TREE execution — follows all 7 HAR-confirmed rules
    Uses AnswerAccumulator
    Fires DECIDE seam (Sonnet) per question group
    Creates Restate awakeable per question — suspends until rep resolves

load_compiler(portal_id) → PortalCompiler
  Loads from PORTAL_REGISTRY dict

PORTAL_REGISTRY:
  "carelon_provider_portal": "portals/carelon_provider_portal.json"

No Carelon-specific logic in PortalCompiler. If you find yourself writing
Carelon logic here, it belongs in the PortalDNA descriptor instead.

### Building From HAR Ground Truth

The PortalDNA descriptor and all client code are built from HAR recordings
and portal analysis — not from the previous prototype. The HAR files are
the single source of truth for:
  1. carelon_provider_portal.json (PortalDNA descriptor) — selectors, endpoints, phases
  2. webforms_client.py — DOM selectors validated against live portal
  3. clinical_client.py — API call sequences confirmed by HAR traffic analysis
  4. behavior_engine.py wraps ALL Playwright interactions from the start

The previous prototype can be consulted as READ-ONLY REFERENCE if a
specific DOM selector or endpoint name needs confirmation, but all code
is written fresh.

---

## TASK 8 — PDF Parser + Haiku Vision Extractor

Files:
  backend/app/ingest/pdf_parser.py       PDF → PNG images (pymupdf)
  backend/app/intelligence/extractor.py  PNG images → ClinicalContext (Haiku vision)

### CRITICAL FINDING FROM DOCUMENT ANALYSIS

Both clinical PDFs analyzed (Zamora and Magness) are 100% image-only.
Zero text layer. pdfplumber returns empty string on both.

Root cause: Envision's RIS uses iTextSharp 5.5.4 to wrap ALL incoming
clinical documents — regardless of source (EHR printout, fax, scan) —
as grayscale PNG images inside a PDF container.

pdfplumber is NEVER useful in this pipeline. Do not install or use it.
The correct pipeline is: pymupdf → PNG images → Claude vision.

### Two Document Patterns Observed

ZAMORA (DrChrono EHR faxed to Envision):
  DPI: 204 x 196 (near-square — high quality scan)
  Pages: 6 clinical pages

MAGNESS (direct fax from Central Park ENT):
  DPI: 200 x 100 (NON-SQUARE — standard fax resolution)
  Must normalize: stretch vertically so text readable for LLM
  Pages: 5 clinical pages

### pdf_parser.py

```python
import fitz  # pymupdf
import io
from PIL import Image  # for fax DPI normalization

def extract_page_images(pdf_bytes: bytes) -> list[dict]:
    """
    Extract PNG image from each page of an iTextSharp-wrapped clinical PDF.
    Returns list of {image_bytes, page_num, width, height, dpi_h, dpi_v, normalized}.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = []

    for i, page in enumerate(doc):
        imgs = page.get_images(full=True)
        if not imgs:
            continue

        xref = imgs[0][0]
        info = doc.extract_image(xref)
        img_bytes = info["image"]
        w, h = info["width"], info["height"]

        # Calculate effective DPI from page dimensions
        page_w_in = page.rect.width / 72
        page_h_in = page.rect.height / 72
        dpi_h = w / page_w_in
        dpi_v = h / page_h_in

        # Normalize non-square fax pixels (200x100 DPI standard fax)
        normalized = False
        if dpi_h / dpi_v > 1.3:
            pil_img = Image.open(io.BytesIO(img_bytes))
            new_h = int(pil_img.height * (dpi_h / dpi_v))
            pil_img = pil_img.resize((pil_img.width, new_h), Image.LANCZOS)
            buf = io.BytesIO()
            pil_img.save(buf, format="PNG")
            img_bytes = buf.getvalue()
            normalized = True

        pages.append({
            "image_bytes": img_bytes,
            "page_num": i + 1,
            "total_pages_in_pdf": len(doc),
            "width": w,
            "height": h,
            "dpi_h": dpi_h,
            "dpi_v": dpi_v,
            "normalized": normalized,
        })

    doc.close()
    return pages
```

### extractor.py

Model: claude-haiku-4-5-20251001 (vision-capable, fast, economical)
Fallback: gemini-2.0-flash via config.py routing
Called: ONCE per PDF upload — not at question evaluation time
Output: stored in ClinicalNote.structured column

```python
import base64
from app.core.settings import settings

EXTRACTION_SYSTEM = """
You are a clinical data extractor for prior authorization.
You receive images of clinical documents — EHR printouts, faxes, scans.
Read all pages regardless of image quality or format.
Ignore fax transmission headers (date/time/phone lines at top of page).
Ignore page footers (e-signature lines, "Powered by" credits).
Extract only what is clearly present. Use null for absent fields.
Return ONLY valid JSON. No prose. No markdown fences.
"""

async def extract_clinical_context(
    pdf_bytes: bytes,
    case_context: dict
) -> dict:
    from app.ingest.pdf_parser import extract_page_images
    pages = extract_page_images(pdf_bytes)

    content = []
    for page in pages:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(page["image_bytes"]).decode()
            }
        })

    content.append({
        "type": "text",
        "text": build_extraction_prompt(len(pages), case_context)
    })

    # Use config.py provider routing — NOT hardcoded model string
    client = settings.get_extraction_client()
    model = settings.get_extraction_model()

    response = await client.messages.create(
        model=model,
        max_tokens=2000,
        system=EXTRACTION_SYSTEM,
        messages=[{"role": "user", "content": content}]
    )

    import json
    return json.loads(response.content[0].text.strip())
```

### Dependencies

  pymupdf (fitz):  pip install pymupdf
  Pillow:          pip install Pillow  (for fax DPI normalization)
  anthropic:       pip install anthropic  (vision support built in)

  Remove from requirements: pdfplumber (not used anywhere)

---

## TASK 9 — Sonnet Evaluator

File: backend/app/intelligence/evaluator.py

Called once per question group during CLINICAL_TREE loop.
Total prompt size per call: ~3,400 tokens. Context window: 200K. No issue.

decide_answer(observation, context, model, rag_config, multi_select) → TypedDecision

Model selection follows the provider routing in config.py:
  Primary:   claude-sonnet-4-6  (Anthropic)
  Fallback:  gemini-2.5-flash   (Google) — used when Anthropic returns 5xx or timeout
  Extractor: claude-haiku-4-5-20251001 primary / gemini-2.0-flash fallback

Build multi-provider config with these methods — do not hardcode model strings.
Load via: settings.get_evaluation_model() and settings.get_extraction_model()
Failover triggers automatically on anthropic.APIStatusError or timeout.

Prompt includes:
  - Case context (CPT, ICD, carrier) — ~300 tokens
  - RAG examples from pgvector (top 5 similar approved cases) — ~1,000 tokens
  - Structured clinical extraction (Haiku output) — ~800 tokens
  - Relevant note excerpts — ~600 tokens
  - Question + options — ~200 tokens

For multi_select=True (Type 4 questions):
  Explicitly instruct: return ALL applicable option UUIDs as array.
  HAR2 shoulder pathway has 3 of 5 questions as Type 4.

TypedDecision fields:
  question_id, question_type, group_id,
  answer_value (str or list[str]),
  measure_unit_id (nullable), measure_unit_values (nullable),
  confidence (0-100), evidence (quote or null),
  gap (missing doc description or null), reasoning (one sentence)

---

## TASK 10 — RAG Retrieval

File: backend/app/intelligence/rag.py

retrieve_similar_cases(question_text, cpt_code, carrier_id, top_k, min_similarity, db)
  → list[dict]

Single PostgreSQL query: WHERE filter + pgvector ANN search combined.
Pre-filter by cpt_code + carrier_id reduces search space before vector math.
Query latency: 2-10ms. Never a bottleneck.

get_embedding(text) → list[float]
  Uses text-embedding-3-small (1536 dimensions).
  Called at:
    1. OutcomePattern insertion (indexing)
    2. RAG retrieval time (query)

format_rag_examples(cases) → str
  Formats top-K cases as readable text for prompt injection.
  Each case: outcome, similarity %, question snippet, answer, evidence quote.

Day 1 behavior: When OutcomePattern table is empty, RAG returns nothing.
Evaluator prompt works without RAG examples — just clinical notes + case context.
RAG enriches over time, never blocks.

---

## TASK 11 — Restate Durable Workflow

File: backend/app/workflow/prior_auth_workflow.py

```python
from restate import Service, Context
prior_auth_service = Service("PriorAuth")
```

@prior_auth_service.handler()
async def process_case(ctx: Context, case_id: str) → dict:

Steps (all wrapped in ctx.run for durability):
  1. Load case + notes from DB
  2. Build ClinicalContext (structured extraction already done at upload time)
  3. Get Playwright session from pool
  4. Load PortalCompiler from registry
  5. compiler.execute() — drives all phases including CLINICAL_TREE
     (CLINICAL_TREE creates awakeables internally, workflow suspends per question)
  6. On completion: save result to DB + index to OutcomePatterns
  7. Update case state to APPROVED/DENIED/PENDED

### Awakeable Flow (inside _run_question_loop)

```python
# Per question group:
awakeable_id, awakeable_promise = ctx.awakeable()

# Save to DB so rep UI can find it
await ctx.run("save_question", lambda: save_question_to_db(
    case_id=case.id,
    question=observation,
    decision=decision,
    awakeable_id=awakeable_id
))

# Workflow suspends here — zero cost
rep_response = await awakeable_promise

# Rep approved or edited — continue
if rep_response.get("changed_prior_group_id"):
    # Backtrack: wipe downstream, resume from changed group
    await accumulator.change(rep_response, session, endpoint)
else:
    accumulator.add(rep_response["answer"])
```

### Session Recovery

If Playwright session dies mid-question-loop:
  1. Restate replays from last checkpoint
  2. AnswerAccumulator is restored from journal (full array)
  3. New session acquired from pool (fresh login)
  4. Deterministic replay: member search → exam setup → pathway selection (~30s)
  5. Resume question loop: send full accumulated array → portal picks up at next question

---

## TASK 12 — FastAPI Routes

File: backend/main.py + backend/app/api/routes/

```
POST   /api/upload/excel           Parse Excel → return preview (no DB write)
                                   Response: {total_rows, duplicate_rows, unique_cases,
                                   stat_count, hold_count, cases_preview (first 50, sorted)}
POST   /api/upload/confirm         Bulk insert Batch + all Case records
                                   Response: {batch_id, unique_cases, stat_count, hold_count}
POST   /api/cases/{id}/notes       Upload PDFs → pymupdf images → Haiku vision → save
POST   /api/cases/{id}/process     Start Restate workflow (local) or enqueue (Azure)
GET    /api/cases                  List cases (paginated, filterable by state/center/batch)
GET    /api/cases/{id}             Case detail + all questions + audit trail
GET    /api/batches                List batches with summary stats
GET    /api/batches/{id}/cases     List cases for batch (filterable by state, center_npi)
                                   Sort by: sort_priority (default), then scheduled_dt
GET    /api/queue                  Cases in IN_REVIEW state (rep queue list)
GET    /api/queue/{id}             Current pending question + AI answer
POST   /api/queue/{id}/resolve     Rep submits answer → resolves Restate awakeable
POST   /api/queue/{id}/flag        Flag case (missing docs)
GET    /api/sessions               Session pool status per NPI
GET    /api/health                 Health check
```

POST /api/queue/{id}/resolve body:
```json
{
  "rep_id": "string",
  "group_id": 3,
  "answer_value": "uuid-or-[uuid1,uuid2]",
  "changed_prior_group_id": null,
  "note": "optional"
}
```
changed_prior_group_id non-null → backtrack triggered in workflow.

Local dev: POST /api/cases/{id}/process calls Restate directly (no Service Bus).
Azure: POST /api/cases/{id}/process enqueues to Azure Service Bus.
The route checks settings.ENVIRONMENT to choose the dispatch method.

---

## TASK 13 — Rep Review Queue UI

File: frontend/app/queue/[caseId]/page.tsx

Rep never touches the Carelon portal. All work happens here.

Three-panel layout:

LEFT — Case summary (fixed, always visible):
  Patient name, DOB, policy, center, CPT+desc, ICD codes, carrier.

CENTER — Active question (one at a time):
  Question text
  AI proposed answer (highlighted)
  Confidence bar: green >=90, amber 70-89, red <70
  Evidence blockquote (exact clinical note quote)
  Gap warning if evidence missing
  Edit control by question type:
    Type 2 (date):        DatePicker
    Type 3 (single):      shadcn Select
    Type 4 (multi):       Checkbox group — multiple selections allowed
  "Approve" button  (sends AI answer as-is)
  "Edit & Approve" button (sends rep's edited answer)
  "Flag" button (missing documentation)

RIGHT — Clinical notes (scrollable):
  Structured extraction (Haiku output) — formatted for quick scanning
  Evidence phrase highlighted when question is active

Bottom status bar: Question N of ~total, case state, elapsed time.

Real-time: new question appears immediately after rep approves previous
(SSE or websocket — workflow resumes and creates next awakeable instantly)

Backtrack UX: if rep wants to change an earlier answer, UI must set
changed_prior_group_id in the resolve payload so workflow handles
DeleteAssetsByGroupId cleanup correctly.

---

## TASK 14 — Excel Upload + Case Preview UI

File: frontend/app/upload/page.tsx

Step 1 — Upload Excel:
  Drag-drop → POST /api/upload/excel → preview table
  Table: Patient | Center | CPT | Primary ICD | ExamId
  Dedup banner: "20 rows → 12 unique cases (8 duplicates removed)"
  "Confirm & Create Cases" button

Step 2 — Attach Clinical Notes:
  Per-case PDF upload (multiple files allowed)
  "Extracting..." → "Ready" (Haiku runs on upload, progress shown)
  Skip allowed — notes uploadable later from case detail

Step 3 — Process:
  "Process All Ready Cases" or per-case "Process" button
  Redirect to /queue when first IN_REVIEW cases appear

---

## TASK 15 — Case List + Detail Pages

File: frontend/app/cases/

Case list:
  State badges (color-coded), CPT, patient name, center, auth number
  Filter by state, center, date. Live state via SSE.

Case detail:
  Full case info + all questions (AI answers, rep decisions, evidence)
  Full audit trail (state changes + rep actions + timestamps)
  Auth number or denial reason prominently shown when terminal
  Retry button for FAILED cases

---

## TASK 16 — Local End-to-End Testing

Single case flow on local machine:

```
1. docker compose up (postgres, redis, restate)
2. alembic upgrade head
3. Upload test Excel (1 row) → verify parsing + case creation
4. Upload test PDF → verify pymupdf extraction + Haiku vision
5. Process case → verify:
   a. Playwright launches, logs into Carelon portal
   b. Member search, exam setup, pathway selection all work
   c. First question appears → Sonnet evaluates → question saved to DB
   d. Rep review UI shows question with AI answer + evidence
   e. Rep approves → awakeable resolves → next question appears
   f. All questions answered → submission completes
   g. Auth number captured → case state APPROVED
   h. OutcomePattern indexed
6. Verify backtrack: change answer mid-flow, confirm DeleteAssetsByGroupId fires
7. Verify session recovery: kill Playwright mid-flow, confirm Restate resumes
```

This is the gate for moving to Azure. Every step must work locally first.

---

## TASK 17 — Outcome Database + RAG Indexing

File: backend/app/db/outcome_db.py

index_outcome_pattern(case_id, result, db):
  Called after every submission completes.
  For each Question in the case:
    Generate embedding for question_text
    Write OutcomePattern record with all fields
    pgvector index updates automatically

This is the compounding RAG moat:
  Day 1:   0 outcome records — LLM uses clinical notes only
  Day 90:  ~270K records — LLM sees verified examples for common combos
  Year 1:  ~750K records — near-deterministic for high-volume CPT/ICD pairs

---

## TASK 18 — Azure Container App Worker

File: infrastructure/container-app.yaml

Per-NPI ACA worker that pulls from Azure Service Bus queue.
KEDA autoscaling: 1 replica per 10 queued messages, 0-5 replicas.
Scale to zero overnight — zero compute cost when no cases queued.
Mounts:
  /mnt/profiles/{npi}  — Azure File Share — persistent Chromium profile
  /mnt/portals         — Azure File Share — PortalDNA JSON files

Morning batch design:
  500 cases, 5am MST start → done by ~5:25am
  12 NPIs x avg 3 replicas = 36 parallel browsers
  36 workers x ~110s/case = 500 cases in ~25 minutes

---

## TASK 19 — Azure Infrastructure

- Azure PostgreSQL Flexible Server + pgvector extension
- Azure Redis Cache (Standard C1)
- Azure File Share for browser profiles + PortalDNA files
- Azure Key Vault for all secrets (replace .env)
- Restate deployed as ACA container
- Azure Service Bus namespace with per-NPI queues

---

## TASK 20 — Production Hardening

- Azure Monitor + Application Insights integration
- Structured logging (JSON) for all worker events
- Alert rules: case failure rate > 5%, session pool exhaustion, LLM timeout rate
- Dashboard: cases/hour, approval rate, avg review time, queue depth
- Graceful shutdown: complete current case before container restart

---

## Phase 1 User Flow (Every Case Human-Gated)

```
Staff uploads Carelon_YYYY-MM-DD.xlsx
  → "20 rows → 12 unique cases" — staff confirms

Staff uploads clinical PDFs per case
  → Haiku extracts structured data immediately

Staff clicks "Process"
  → Restate workflow starts (local direct / Azure via Service Bus)
  → PortalCompiler executes: EXAM_SETUP → EXAM_PROCESSING → DIAGNOSIS → PATHWAY

CLINICAL_TREE begins:
  Portal returns Q1 → Sonnet evaluates → workflow suspends
  Case appears in rep review queue
  Rep sees: question + AI answer + evidence + confidence bar
  Rep approves (or edits) → awakeable resolved → portal receives answer
  Portal returns Q2 → Sonnet evaluates → workflow suspends
  Rep approves Q2 → Q3 → Q4 → Q5

Loop complete → SUBMISSION executes
Auth number captured → case APPROVED → visible in case list
OutcomePattern indexed → RAG database grows
```

---

## HAR Reference — Ground Truth

```
HAR1: CPT:73722 / ICD:M70.62 (Trochanteric bursitis, left hip)
  Algorithm:   "Lower extremity MRI other diagnosis or reasons for imaging Carelon"
  AlgorithmId: 635ae8b8-fa32-4a97-af50-f9bb315a3ed8
  WorkspaceId: a7b582ca-7782-4d00-9159-9a47d1145a7c
  PermanentId: MSK | CPTGroupID: 46
  ProviderID:  106818278 | ClientID: 200
  Questions:   5 | Types: 2, 3, 3, 3, 3
  API calls:   81 | Loop calls: 9
  Session:     1070s | Human think avg: 24.5s/question
  Akamai:      115 hits — all on .aspx page loads, zero on API calls

HAR2: CPT:73221 / ICD:M25.512 (Pain in left shoulder)
  Algorithm:   "Upper extremity joint MRI rotator cuff Carelon"
  AlgorithmId: be6ff2b1-1fda-4ba6-8c00-ca297c7f8ec0
  WorkspaceId: a7b582ca-7782-4d00-9159-9a47d1145a7c  ← same workspace
  PermanentId: MSK | CPTGroupID: 47
  ProviderID:  103564762 | ClientID: 55
  Questions:   5 | Types: 4, 3, 4, 4, 3  ← multi-select!
  API calls:   94 | Loop calls: 7
  Session:     753s | Human think avg: 47.3s/question
  Backtrack:   12 DeleteAssetsByGroupId calls

Critical finding: HAR1 and HAR2 share ZERO QuestionIds.
Every CPT/ICD produces a completely different question tree.
Never hardcode any QuestionId UUID.
```

---

## Non-Negotiable Rules for Claude Code

When writing any code in this project:

```
1. NO httpx for portal API calls.
   All calls via session.api() which runs fetch() inside Playwright browser.

2. NO hardcoded QuestionIds, option UUIDs, ProviderID, or ClientID.
   All come from portal responses or session context at runtime.

3. NO sleep() in the question loop.
   LLM latency = authentic think time. Do not add delays.

4. NO two-pass question scraping.
   Questions are a live server-side decision tree.
   You cannot see Q3 without committing an answer to Q2 first.

5. AnswerAccumulator.payload sends the FULL array every call.
   Never send just the new answer.

6. Backtrack MUST call DeleteAssetsByGroupId for all downstream groups
   before removing them from the accumulator.

7. Behavior Engine wraps ALL Playwright interactions.
   Timing uses gaussian, not random.uniform().

8. One Playwright browser per CenterNPI.
   Persistent Chromium profile — local ./profiles/ or Azure File Share.

9. MFA resolver uses Microsoft Graph API to poll for Carelon OTP emails.
   Build fresh — the prior prototype validates the approach works.

10. PortalCompiler is portal-agnostic.
    Carelon-specific logic belongs in the PortalDNA descriptor,
    not in portal_compiler.py.

11. Never hardcode model strings (e.g. "claude-sonnet-4-6") in evaluator or
    extractor code. Build config.py with provider routing and failover.
    Primary: Anthropic (Sonnet for eval, Haiku for extraction).
    Fallback: Google Gemini (gemini-2.5-flash eval, gemini-2.0-flash extraction).
    settings.get_evaluation_model() and settings.get_extraction_model().

12. Local-first: every feature must work with docker-compose before Azure.
    No Azure-only code paths until Task 18+. Use settings.ENVIRONMENT
    to switch between local and production dispatch.

13. Clean build — everything written fresh from HAR ground truth.
    The prior prototype in apps/worker/ is READ-ONLY REFERENCE for
    confirming DOM selectors and API sequences. Do not copy-paste.
    Do not import. Build clean with no inherited patterns or bugs.
```
