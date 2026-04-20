# Ronexa

**Automated Prior Authorization System for the Carelon Provider Portal**

Ronexa automates the submission of diagnostic imaging prior authorization requests to Carelon's provider portal on behalf of Envision Imaging. It uses browser automation (Playwright) to navigate the portal, LLMs (Claude + Gemini) to answer clinical questions, and a human review dashboard (Next.js) for quality control before final submission.

## The Business Problem

Authorization reps manually log into the Carelon portal, search for members, enter procedure codes, answer clinical pathway questions, and submit requests — a process that takes 10-20 minutes per case. Ronexa automates this entire flow:

1. Cases arrive from the RIS (Radiology Information System) via Cosmos DB
2. A browser automation worker logs into the portal and navigates the submission forms
3. An LLM evaluates each clinical question using the patient's clinical documentation
4. A rep reviews the AI's answers in a dashboard before submission
5. The system submits the approved answers and captures the determination

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Directory Structure](#directory-structure)
- [System Components](#system-components)
  - [Backend (FastAPI)](#backend-fastapi)
  - [Workflow Engine (Restate)](#workflow-engine-restate)
  - [Portal Automation](#portal-automation)
  - [Authentication Flow](#authentication-flow)
  - [LLM Intelligence](#llm-intelligence)
  - [Frontend (Next.js)](#frontend-nextjs)
  - [Data Ingestion](#data-ingestion)
- [Case Lifecycle](#case-lifecycle)
- [Portal Navigation Phases](#portal-navigation-phases)
- [Database Schema](#database-schema)
- [API Reference](#api-reference)
- [Environment Variables](#environment-variables)
- [Development Setup](#development-setup)
- [Deployment](#deployment)
- [Common Operations](#common-operations)
- [Troubleshooting](#troubleshooting)
- [Key Design Decisions](#key-design-decisions)

---

## Architecture Overview

### Data Flow

```
                    ┌──────────────┐
                    │  Cosmos DB   │ (RIS cases arrive here)
                    │  (MongoDB)   │
                    └──────┬───────┘
                           │ Poll every 60s
                           ▼
                    ┌──────────────┐     ┌──────────────┐
                    │  PostgreSQL  │◄────│  Azure Blob  │ (clinical PDFs)
                    │  (pgvector)  │     │   Storage    │
                    └──────┬───────┘     └──────────────┘
                           │
                           ▼
                    ┌──────────────┐
                    │  WorkerLoop  │ (Restate Virtual Object)
                    │  Claims next │ (priority: STAT > standard)
                    │  case from   │
                    │  job queue   │
                    └──────┬───────┘
                           │
              ┌────────────┴────────────┐
              ▼                         ▼
     ┌────────────────┐       ┌────────────────┐
     │ CaseWorkflow   │       │ OrderWorkflow  │
     │ (Job 1: First  │       │ (no clinical   │
     │  Pass w/ docs) │       │  notes variant)│
     └───────┬────────┘       └───────┬────────┘
             │                        │
             ▼                        ▼
     ┌────────────────────────────────────┐
     │        Worker VM (Playwright)      │
     │  1. Login to Carelon portal        │
     │  2. Navigate 11 form phases        │
     │  3. LLM answers clinical questions │
     │  4. Capture determination          │
     └───────────────┬────────────────────┘
                     │
                     ▼
     ┌────────────────────────────────────┐
     │  Questions saved to PostgreSQL     │
     │  Case moves to L1_REVIEW state     │
     └───────────────┬────────────────────┘
                     │
                     ▼
     ┌────────────────────────────────────┐
     │   Next.js Dashboard (Rep Review)   │
     │  - Review AI answers               │
     │  - Edit if needed                  │
     │  - Approve → L2 → Submit          │
     └───────────────┬────────────────────┘
                     │
                     ▼
     ┌────────────────────────────────────┐
     │  SubmitWorkflow (Job 2)            │
     │  - Replay portal with approved     │
     │    answers                         │
     │  - Capture auth # or denial        │
     │  - Fax clinical docs if pended     │
     └────────────────────────────────────┘
```

### Infrastructure

```
┌─────────────────────────────────────────────────────────────────┐
│                 ORCHESTRATOR VM (20.29.73.195)                   │
│                                                                  │
│  ┌─────────┐  ┌──────────┐  ┌──────────┐  ┌───────────────┐   │
│  │  nginx  │  │ frontend │  │ backend  │  │   restate     │   │
│  │  :80    │  │  :3000   │  │  -api    │  │   :8080       │   │
│  │         │  │ (Next.js)│  │  :8000   │  │   admin:9071  │   │
│  └────┬────┘  └──────────┘  │ (FastAPI)│  └───────────────┘   │
│       │                     └──────────┘                       │
│       │                     ┌──────────────────┐               │
│       └─────────────────────│ restate-handler  │               │
│                             │  :9080           │               │
│                             │  (workflow code) │               │
│                             └──────────────────┘               │
│  ┌──────────┐  ┌───────┐                                      │
│  │PostgreSQL│  │ Redis │                                      │
│  │  :5432   │  │ :6379 │                                      │
│  └──────────┘  └───────┘                                      │
└─────────────────────────────────────────────────────────────────┘
         │
         │ HTTP calls to worker VMs
         │
    ┌────┴────────────────┬──────────────────┐
    ▼                     ▼                  ▼
┌──────────┐       ┌──────────┐       ┌──────────┐
│Worker-A  │       │Worker-B  │       │Worker-C  │
│172.202.  │       │74.249.   │       │20.106.   │
│22.112    │       │202.85    │       │37.51     │
│:9081     │       │:9081     │       │:9081     │
│          │       │          │       │          │
│Playwright│       │Playwright│       │Playwright│
│ browser  │       │ browser  │       │ browser  │
└──────────┘       └──────────┘       └──────────┘
```

**Key principle:** Workers run plain HTTP servers — they are NOT Restate services. The Restate handler on the orchestrator dispatches work to workers via HTTP. This means workers can be scaled independently and don't need Restate installed.

---

## Directory Structure

```
ronexa-sub/
├── backend/                              # Python backend
│   ├── main.py                           # FastAPI app entry point
│   ├── restate_worker.py                 # Restate handler registration
│   ├── requirements.txt                  # Python dependencies
│   ├── Dockerfile                        # Backend API container
│   ├── Dockerfile.worker                 # Worker VM container (includes Playwright)
│   ├── alembic/                          # Database migrations
│   │   └── versions/                     # 27 migration files
│   ├── app/
│   │   ├── api/routes/                   # FastAPI route handlers
│   │   │   ├── queue.py                  # Rep review queue (resolve, rerun, flag)
│   │   │   ├── cases.py                  # Case CRUD + state transitions
│   │   │   ├── analytics.py             # Outcome patterns, bypass rules
│   │   │   ├── settings.py              # System config, prompt templates
│   │   │   ├── auth.py                  # Login/logout/session
│   │   │   ├── jobs.py                  # Submission job lifecycle
│   │   │   ├── signatures.py           # Algorithm signature replay
│   │   │   ├── executions.py           # Billing event logs
│   │   │   ├── uploads.py              # File upload handling
│   │   │   └── sync.py                 # Mongo→Postgres sync trigger
│   │   ├── auth/                        # Portal authentication
│   │   │   ├── okta_login.py            # Full Carelon + Okta login flow
│   │   │   └── mfa_resolver.py          # Graph API OTP email polling
│   │   ├── compiler/                    # Portal navigation engine
│   │   │   ├── portal_compiler.py       # Phase-driven case execution
│   │   │   └── portal_dna.py            # Portal descriptor model
│   │   ├── portal/                      # Browser + API interactions
│   │   │   ├── session.py               # PlaywrightPortalSession (in-page fetch)
│   │   │   ├── clinical_flow.py         # ClinicalExamFlow (SPA API calls)
│   │   │   ├── clinical_client.py       # ClinicalFacade low-level API
│   │   │   ├── webforms_client.py       # HTML form interactions
│   │   │   ├── page_reader.py           # DOM scraping utilities
│   │   │   ├── session_pool.py          # Browser session pool (per NPI)
│   │   │   └── behavior_engine.py       # Human-like timing + mouse movement
│   │   ├── intelligence/                # LLM + extraction
│   │   │   ├── evaluator.py             # Question answering (Claude + Gemini)
│   │   │   ├── extractor.py             # Clinical PDF → structured data (OCR)
│   │   │   ├── prompts.py               # Prompt template builder
│   │   │   ├── llm_config.py            # Default prompt templates
│   │   │   ├── rag.py                   # pgvector similarity search
│   │   │   ├── models.py                # PortalObservation, TypedDecision
│   │   │   └── answer_bridge.py         # Portal↔DB answer format bridge
│   │   ├── workflow/                    # Restate durable workflows
│   │   │   ├── case_workflow.py         # Job 1: first pass + question discovery
│   │   │   ├── submit_workflow.py       # Job 2: submit with approved answers
│   │   │   ├── order_workflow.py        # Order-only variant (no clinical docs)
│   │   │   ├── awaiting_clinical_workflow.py  # Signature replay
│   │   │   ├── worker_loop.py           # Pull-based queue polling + dispatch
│   │   │   ├── worker_session.py        # Thin HTTP caller to worker VMs
│   │   │   └── extraction_service.py    # Parallel OCR extraction
│   │   ├── worker/                      # Worker VM code
│   │   │   ├── http_server.py           # FastAPI on worker (port 9081)
│   │   │   ├── browser_manager.py       # Playwright browser lifecycle
│   │   │   └── helpers.py               # Portal operations (login, mark done)
│   │   ├── ingest/                      # Data ingestion
│   │   │   ├── mongo_poller.py          # Cosmos DB → Case records
│   │   │   ├── blob_fetcher.py          # Azure Blob Storage downloads
│   │   │   ├── excel_parser.py          # Excel batch upload parsing
│   │   │   ├── poll_scheduler.py        # Background polling scheduler
│   │   │   └── sync_engine.py           # Mongo fetch + dedup + insert
│   │   ├── db/                          # Database layer
│   │   │   ├── models.py                # SQLAlchemy models + enums
│   │   │   ├── repositories.py          # CRUD operations
│   │   │   ├── database.py              # Async session factory
│   │   │   ├── queue_manager.py         # Priority queue (SELECT FOR UPDATE)
│   │   │   └── outcome_db.py            # Outcome pattern + signature queries
│   │   ├── services/                    # External integrations
│   │   │   └── ringcentral.py           # Fax sending via RingCentral
│   │   └── core/
│   │       └── settings.py              # Pydantic settings (.env loading)
│   ├── portals/
│   │   └── carelon_provider_portal.json # Portal DNA (phases, selectors, APIs)
│   └── tests/                           # Integration + unit tests
│
├── frontend/                            # Next.js 14 dashboard
│   ├── app/                             # App Router pages
│   │   ├── page.tsx                     # Dashboard (metrics overview)
│   │   ├── queue/page.tsx               # Review queue (L1/L2/Fax tabs)
│   │   ├── queue/[caseId]/page.tsx      # Case review (questions + actions)
│   │   ├── cases/page.tsx               # Cases list (Active/Submission/Hold/Done)
│   │   ├── cases/[caseId]/page.tsx      # Case detail (upload, cure, retry)
│   │   ├── analytics/page.tsx           # 7-tab analytics dashboard
│   │   ├── worklist/page.tsx            # Exception resolution
│   │   ├── queues/page.tsx              # Submission job queues
│   │   ├── awaiting-clinicals/page.tsx  # Order-only + signature replay
│   │   ├── executions/page.tsx          # Daily event tracking
│   │   ├── upload/page.tsx              # 3-step Excel upload wizard
│   │   ├── settings/page.tsx            # Admin config (4 tabs)
│   │   └── login/page.tsx               # Authentication
│   ├── lib/api.ts                       # 40+ API client functions
│   ├── components/NavBar.tsx            # Top navigation
│   ├── middleware.ts                    # Session auth guard
│   ├── package.json                     # Dependencies
│   ├── Dockerfile                       # Multi-stage Next.js build
│   └── tailwind.config.ts              # Tailwind CSS config
│
├── infra/
│   └── nginx/nginx.conf                # Reverse proxy config
│
├── deploy.sh                           # CI/CD deployment script
├── docker-compose.yml                  # Local development stack
├── docker-compose.prod.yml             # Production orchestrator stack
└── .env.example                        # Environment variable template
```

---

## System Components

### Backend (FastAPI)

**Entry point:** `backend/main.py`

The backend is a FastAPI application that serves the REST API and registers Restate workflow handlers on startup. It runs on port 8000 behind nginx.

**On startup (`lifespan`):**
1. Registers the Restate handler deployment at `localhost:9080`
2. Starts the background poll scheduler (Cosmos DB → PostgreSQL sync)

**API Routes:**

| File | Prefix | Purpose |
|------|--------|---------|
| `queue.py` | `/api/queue` | Rep review queue — resolve L1/L2, rerun, flag, on-hold, pathway change, fax validation |
| `cases.py` | `/api/cases` | Case CRUD, state transitions, clinical note upload, auth PDF download |
| `analytics.py` | `/api/analytics` | Outcome patterns, approval rates, bypass/automation rules |
| `settings.py` | `/api/settings` | System settings, LLM prompt templates, flush/sync controls |
| `auth.py` | `/api/auth` | Login, logout, session check |
| `jobs.py` | `/api/jobs` | Submission job queue status, retry, workers |
| `signatures.py` | `/api/signatures` | Algorithm signature CRUD, replay trigger |
| `executions.py` | `/api/executions` | Billing event log queries |
| `uploads.py` | `/api/uploads` | Excel and PDF file uploads |
| `sync.py` | `/api/sync` | Manual Mongo→Postgres sync trigger |

---

### Workflow Engine (Restate)

Ronexa uses [Restate](https://restate.dev) for durable workflow orchestration. Restate provides exactly-once execution guarantees, automatic retries, and state persistence — without needing an external message broker like Kafka or RabbitMQ.

#### Two-Job Architecture

Portal submission is split into two independent jobs:

```
┌─────────────────────────────────────────────────────┐
│                    JOB 1: First Pass                 │
│                                                      │
│  CaseWorkflow / OrderWorkflow                        │
│  1. Login to portal (if not already)                │
│  2. Navigate all 11 portal phases                   │
│  3. LLM answers each clinical question              │
│  4. Save questions + AI answers to PostgreSQL       │
│  5. Case → L1_REVIEW state                          │
│                                                      │
│  Output: Questions with AI answers for rep review   │
└─────────────────────────────────────────────────────┘
                         │
                    Rep reviews in
                    Next.js dashboard
                    (approves/edits)
                         │
┌─────────────────────────────────────────────────────┐
│                    JOB 2: Submit                     │
│                                                      │
│  SubmitWorkflow                                      │
│  1. Load approved answers from PostgreSQL           │
│  2. Login to portal (fresh session)                 │
│  3. Replay all 11 phases with rep-approved answers  │
│  4. Submit and capture determination                │
│  5. If pended: send fax with clinical docs          │
│  6. Case → APPROVED / PENDED / DENIED              │
│                                                      │
│  Output: Auth number, determination, denial reason  │
└─────────────────────────────────────────────────────┘
```

**Why two jobs?** Separation of concerns. Job 1 discovers questions and gets AI answers. The rep reviews between jobs. Job 2 submits with final answers. No workflow suspension or awakeables needed during review — the rep's approval triggers a new job.

#### Workflow Services

| Service | Type | Key | Purpose |
|---------|------|-----|---------|
| `CaseWorkflow` | Workflow | `case_id` | Job 1 with clinical documents |
| `OrderWorkflow` | Workflow | `case_id` | Job 1 without clinical documents |
| `SubmitWorkflow` | Workflow | `case_id` | Job 2: submit approved answers |
| `AwaitingClinicalWorkflow` | Workflow | `case_id` | Signature replay for awaiting-clinicals |
| `WorkerLoop` | VirtualObject | `worker_id` | Pull-based queue polling + dispatch |
| `WorkerSession` | VirtualObject | `worker_id` | Thin HTTP caller to worker VMs |
| `ExtractionService` | Service | (stateless) | Parallel OCR extraction |

#### WorkerLoop — How Cases Get Processed

WorkerLoop is a Restate Virtual Object (one per worker). It continuously:

1. Queries the `SubmissionJob` priority queue (Postgres `SELECT FOR UPDATE SKIP LOCKED`)
2. Claims the highest-priority job
3. Dispatches the appropriate workflow (CaseWorkflow, SubmitWorkflow, etc.)
4. On completion, loops back to step 1
5. If queue is empty, sleeps via Restate awakeable (woken by poll scheduler when new cases arrive)

**Priority order:** SUBMIT (highest) > FIRST_PASS > ORDER > SIGNATURE_REPLAY

**Within each type:** STAT cases (priority 1000) before standard (priority 50-200)

---

### Portal Automation

#### PortalCompiler (`backend/app/compiler/portal_compiler.py`)

The compiler is the brain of portal navigation. It reads a "Portal DNA" JSON file that describes the portal's structure (phases, selectors, API endpoints) and executes each phase sequentially.

**Three phase types:**

| Type | Description | Example Phases |
|------|-------------|---------------|
| `WEBFORM` | Direct HTML interaction via Playwright (fill forms, click buttons, read DOM) | Member search, provider search, facility search |
| `API_SEQUENCE` | JSON API calls via ClinicalFacade SPA (run inside the browser page) | Exam setup, diagnosis entry, pathway selection |
| `RECURSIVE_STATE_MACHINE` | Iterative question-answer loop with LLM evaluation | Clinical questions |

**Key method:** `compile(case, session, clinical_context, resume_answers, changed_group_id, dry_run, order_mode)`

- `case`: Dict with patient info (name, DOB, CPT, ICD, etc.)
- `session`: Playwright browser session (already logged in)
- `clinical_context`: Extracted clinical notes (OCR output)
- `resume_answers`: Pre-approved answers for Job 2 submission
- `changed_group_id`: For reruns — backtrack from this question group
- `dry_run`: Stop before submitting (for testing)
- `order_mode`: Use order-form-specific LLM prompts

#### PlaywrightPortalSession (`backend/app/portal/session.py`)

**Critical concept:** ALL ClinicalFacade API calls run via `page.evaluate(fetch())` INSIDE the browser — not as external HTTP requests. This means Carelon's portal sees organic browser traffic, defeating Akamai WAF protection.

```python
# How portal API calls work (simplified):
async def api(self, endpoint: str, payload: dict) -> dict:
    return await self.page.evaluate("""
        async ([url, body]) => {
            const resp = await fetch(url, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body)
            });
            return await resp.json();
        }
    """, [endpoint_url, payload])
```

#### ClinicalExamFlow (`backend/app/portal/clinical_flow.py`)

Orchestrates the ClinicalFacade SPA's API sequence:

1. `GetCase` — Initialize exam (returns available CPT codes)
2. `AddExam` — Add the CPT code to the case
3. `SetSelectedDiagnosis` — Set ICD-10 diagnosis
4. `GetPathwayOptions` → `SetPathway` — Select clinical scenario
5. `GetPathwayAssetsWithValidation` — Get questions (iterative loop)
6. `ProcessAccepted` → `IsExamAutoApproved` — Check determination
7. `DoneWithExam` → `FindNextExam` — Finalize

#### WebFormsClient (`backend/app/portal/webforms_client.py`)

Handles traditional HTML form pages (member search, provider search, facility search). Uses Playwright locators for clicking, filling, and reading DOM elements.

**Key pattern — `postback_hdnaction(action_value)`:** The portal uses a hidden `hdnAction` field to transition between the clinical SPA and traditional WebForms pages:
- `hdnAction=20`: Clinical SPA → Exam Summary
- `hdnAction=6`: Exam Summary → Facility Search
- `hdnAction=17`: Review → Order Summary

#### BehaviorEngine (`backend/app/portal/behavior_engine.py`)

Adds human-like timing to all interactions:
- Page load pause: ~400ms
- Form field focus: ~150ms
- Button click delay: ~120ms
- Typing speed: ~50ms/char (~200 WPM)
- Mouse movement: Bezier curves (8-12 steps)

---

### Authentication Flow

```
┌──────────────────────────────────────────────────────────────┐
│  Step 1: Carelon Login Page                                  │
│  URL: providerportal.com/Default.aspx                       │
│  Enter username → Click "Next"                              │
├──────────────────────────────────────────────────────────────┤
│  Step 2: Okta Sign-In Widget                                │
│  Username pre-filled (readonly) → Click "Next"              │
├──────────────────────────────────────────────────────────────┤
│  Step 3: Password                                           │
│  Enter password → Click "Login"                             │
├──────────────────────────────────────────────────────────────┤
│  Step 4: MFA (Carelon's own — NOT Okta MFA)                │
│  Click "Send Email Verification"                            │
│  Poll Microsoft Graph API for OTP email                     │
│  Enter 6-digit code → Click "Verify"                       │
├──────────────────────────────────────────────────────────────┤
│  Step 5: HIPAA Disclaimer                                   │
│  Click "I Agree"                                            │
├──────────────────────────────────────────────────────────────┤
│  Step 6: Dashboard                                          │
│  Verify: "Start Your Order", "Find This Member", "Logout"  │
└──────────────────────────────────────────────────────────────┘
```

**MFA Resolver (`backend/app/auth/mfa_resolver.py`):**
- Uses Microsoft Graph API to read OTP emails from a shared mailbox
- **Prepare/wait pattern:** `resolver.prepare()` flushes stale OTP emails and sets a timestamp cutoff BEFORE clicking "Send Email" — prevents picking up old codes from previous login attempts
- Polls every 3 seconds for up to 90 seconds

**Files:**
- `backend/app/auth/okta_login.py` — Full login orchestration
- `backend/app/auth/mfa_resolver.py` — Graph API email polling

---

### LLM Intelligence

#### Question Evaluator (`backend/app/intelligence/evaluator.py`)

The evaluator answers each portal clinical question using an LLM. It receives:
- The question text and available options
- Clinical documentation (OCR-extracted from patient's PDF)
- RAG examples (historically approved answers for similar questions)
- Algorithm signatures (proven Q&A sequences)
- Previous answers in the current session (for consistency)

**LLM chain:** Anthropic Claude (primary) → Google Gemini (fallback)

**Dual-Answer System:**
Every question gets TWO answers:
1. **Approval Path** (`answer_value`): The answer most likely to satisfy the portal's approval algorithm. This is what gets submitted.
2. **Notes Path** (`notes_answer_value`): The most documentation-honest answer based purely on clinical evidence.

When both match, the case is strong. When they differ, an `approval_gap` explains what documentation would bridge the gap. This helps reps understand the AI's reasoning.

**Output (`TypedDecision`):**
```python
class TypedDecision:
    answer_value: str | list[str]    # Portal answer (approval path)
    confidence: float                 # 0-100%
    reasoning: str                    # Why this answer
    evidence: dict                    # Explicit + inferred evidence
    gap: str                          # What's missing
    notes_answer_value: str           # Documentation-honest answer
    notes_confidence: float
    notes_reasoning: str
    approval_gap: str                 # Bridge between paths
```

#### RAG System (`backend/app/intelligence/rag.py`)

Uses pgvector embeddings to find historically approved answers for similar questions. The `OutcomePattern` table stores question-answer pairs from successful submissions with their outcomes (APPROVED, DENIED, PENDED).

These are injected into the LLM prompt as "Approval Patterns" — strong signals of what the portal accepts.

#### Algorithm Signatures (`backend/app/db/outcome_db.py`)

When a case is approved, its complete Q&A sequence is stored as an `AlgorithmSignature` (keyed by CPT + ICD + pathway). Future cases with the same CPT/ICD can replay this signature instead of running the LLM — faster and more reliable.

#### Clinical Extractor (`backend/app/intelligence/extractor.py`)

Converts clinical PDF documents to structured data using Azure Document Intelligence (OCR). Extracts:
- Clinical indication
- Chief complaint
- Physical exam findings
- Prior treatments
- Medications
- Symptoms and body parts
- Dates and provider info

**File:** `backend/app/intelligence/extractor.py`

---

### Frontend (Next.js)

The frontend is a Next.js 14 application (App Router) with Tailwind CSS. It serves as the rep review dashboard.

#### Key Pages

| Page | Path | What Reps Do Here |
|------|------|-------------------|
| **Dashboard** | `/` | View today's metrics: submitted, STAT queue, exceptions, worker status |
| **Review Queue** | `/queue` | Pick cases to review (L1/L2/Fax tabs, auto-refreshes every 5s) |
| **Case Review** | `/queue/[caseId]` | Review AI answers, edit if needed, approve/reject/flag/hold |
| **Cases List** | `/cases` | Browse all cases by state (Active, Submission, Hold, Completed) |
| **Case Detail** | `/cases/[caseId]` | Upload clinical PDFs, check auth result, cure hold cases |
| **Analytics** | `/analytics` | 7-tab analytics: approval rates, pathway intelligence, signatures |
| **Worklist** | `/worklist` | Resolve submission exceptions (member not found, duplicate auth, etc.) |
| **Queues** | `/queues` | Monitor STAT and standard submission job queues |
| **Settings** | `/settings` | Admin controls: review toggles, bypass rules, prompts, workers |

#### Case Review Page Layout

```
┌─────────────────────────────────────────────────────────┐
│  ◄ Back to Queue                                        │
├───────────────┬─────────────────────────────────────────┤
│               │                                         │
│  LEFT PANEL   │           RIGHT PANEL                   │
│  (1/3 width)  │           (2/3 width)                   │
│               │                                         │
│  Patient Info │  Clinical Questions (N)     [L1 Review] │
│  - Name       │  ┌──────────────────────────────────┐  │
│  - DOB        │  │ Q1: Has the patient had a        │  │
│  - Policy #   │  │     history and physical exam?   │  │
│  - CPT / ICD  │  │                                   │  │
│               │  │  AI Answer: ● Yes                │  │
│  Verdict      │  │  Confidence: ████████░░ 95%      │  │
│  [APPROVED]   │  │  Evidence: "Clinical note from   │  │
│               │  │   ortho specialist dated..."     │  │
│  Clinical PDF │  │                                   │  │
│  [View Doc]   │  │  Rep Answer: [● Yes ○ No]        │  │
│               │  └──────────────────────────────────┘  │
│  Clinical     │  ┌──────────────────────────────────┐  │
│  Scenario     │  │ Q2: Has a recent x-ray been     │  │
│  ○ Knee:      │  │     performed?                   │  │
│    Ligament   │  │     ...                          │  │
│  ○ Knee:      │  └──────────────────────────────────┘  │
│    Arthritis  │                                         │
│               │  ┌──────────────────────────────────┐  │
│  AI Reasoning │  │ Already Worked │ Flag │ On Hold  │  │
│  Confidence:  │  │         [Approve & Send to L2]    │  │
│  Evidence:    │  └──────────────────────────────────┘  │
│               │                                         │
└───────────────┴─────────────────────────────────────────┘
```

**Action buttons:**
- **Already Worked** — Case was handled in legacy system
- **Flag** — Flag for supervisor attention
- **On Hold** — Clear questions, move to HOLD (rep fixes patient data, then requeues)
- **Re-Run** (L2 only) — Send back to L1 with new questions
- **Approve & Send to L2** (L1) / **Submit to Portal** (L2)

#### Badges in Queue List

| Badge | Color | Meaning |
|-------|-------|---------|
| STAT | Red | Urgent case |
| Auto Approve | Purple | Gold card / auto-approved (0 questions, order only) |
| Algorithm Approved | Green | Algorithm recommended approval (has questions) |
| GC:1 / GC:2 | Yellow | Gold card level |
| Signature Replay | Cyan | Using stored Q&A sequence |
| Pass #2+ | Gray | Re-run (rep changed answers) |
| L1 Reviewed | Blue | Passed L1, in L2 now |

---

### Data Ingestion

#### Cosmos DB Poller (`backend/app/ingest/mongo_poller.py`)

Polls Azure Cosmos DB (MongoDB API) every 60 seconds for new cases:

```
Cosmos DB: workflowdb.auth-submissions
Filter: PortalMatch = "Carelon", status = "Submitted"
```

**Field mapping (Mongo → PostgreSQL):**

| Cosmos DB Field | Case Column | Notes |
|----------------|-------------|-------|
| `ExamId` | `exam_id` | Unique key for deduplication |
| `FirstName` | `first_name` | |
| `LastName` | `last_name` | |
| `dob` | `dob` | |
| `CenterNPI` | `center_npi` | Facility NPI |
| `cptcode` | `cpt_code` | Procedure code |
| `ICD1`-`ICD5` | `icd1`-`icd5` | Diagnosis codes |
| `FileKey` | `file_key` | Order form PDF in Azure Blob |
| `ClinicalAttachments` | `clinical_blob_key` | Clinical notes PDF in Azure Blob |
| `IsStat` | `is_stat` | Priority flag |

#### Azure Blob Storage (`backend/app/ingest/blob_fetcher.py`)

Downloads clinical PDFs from the `carelon-attachments` container. Blob keys always have `.pdf` suffix. Used by:
- Clinical extractor (OCR → structured data)
- Rep review (PDF viewer link)
- Fax sending (clinical docs attached to pended cases)

#### Excel Upload (`backend/app/ingest/excel_parser.py`)

Manual batch upload via the `/upload` page. Parses Excel rows into Case records with deduplication by `exam_id`.

---

## Case Lifecycle

A case goes through these states (in order):

```
1. PENDING_NOTES          Case created, waiting for clinical docs
   │
   ├─→ WAITING_CLINICALS  No clinical docs available (order-only path)
   │
   ▼
2. NOTES_UPLOADED         Clinical PDF extracted via OCR
   │
   ▼
3. PROCESSING             WorkerLoop claimed the case, browser automating
   │
   ├─→ HOLD               Portal error, missing data, or rep-requested hold
   │
   ├─→ NO_AUTH_REQUIRED    Portal says DI doesn't require pre-auth
   │
   ▼
4. L1_REVIEW              Questions + AI answers ready for L1 rep review
   │
   ├─→ HOLD               Rep moved to hold (fix ICD, missing info)
   │
   ▼
5. L2_REVIEW              L1 approved, waiting for L2 senior review
   │
   ├─→ L1_REVIEW          L2 triggered re-run (back to L1 with new questions)
   │
   ▼
6. SUBMITTING             SubmitWorkflow running, browser submitting to portal
   │
   ├─→ APPROVED           Auth number issued
   ├─→ PENDED             Additional review needed (fax clinical docs)
   │   └─→ PENDED_FAX_REVIEW  Fax ready for rep validation
   ├─→ DENIED             Authorization denied
   └─→ FAILED             Portal error during submission
```

**Special states:**
- `ALREADY_WORKED` — Case was already handled in legacy system
- `ORDER_READY` — Order-only case ready for processing
- `CLINICAL_REVIEW` — Signature replay awaiting rep verification

---

## Portal Navigation Phases

The PortalCompiler executes these 11 phases sequentially. Each phase corresponds to a page or step in the Carelon portal:

| # | Phase | Type | What Happens |
|---|-------|------|-------------|
| 1 | `member_search` | WEBFORM | Enter patient name + DOB → find member in portal |
| 2 | `start_order` | WEBFORM | Select "Diagnostic Imaging" category → start new order |
| 3 | `check_existing_auths` | WEBFORM | Check for duplicate authorizations → extract existing auths |
| 4 | `provider_search` | WEBFORM | Search ordering provider by NPI → match by address → set fax |
| 5 | `clinical_exam_setup` | API_SEQUENCE | GetCase → enter CPT code → validate → add exam |
| 6 | `clinical_diagnosis` | API_SEQUENCE | Enter ICD-10 code → set diagnosis |
| 7 | `clinical_pathway` | API_SEQUENCE | Get pathway options → LLM or ICD match → set clinical scenario |
| 8 | `clinical_questions` | RECURSIVE_STATE_MACHINE | Loop: get questions → LLM answers → submit → repeat until done |
| 9 | `clinical_complete` | API_SEQUENCE | ProcessAccepted → check auto-approval → finalize exam |
| 10 | `facility_search` | WEBFORM | Advanced search by NPI → select facility → set fax |
| 11 | `submit_and_extract` | WEBFORM | "Submit This Request" → capture auth # and determination |

**Phase 8 (clinical questions) is where the LLM does its work.** Each question is:
1. Parsed from the portal's `GetPathwayAssetsWithValidation` response
2. Sent to the LLM with clinical context, RAG examples, and prior answers
3. The LLM returns a confidence-scored answer with evidence
4. The answer is submitted to the portal
5. The portal returns the next batch of questions (branching tree)
6. Repeat until done

---

## Database Schema

### Core Tables

#### `cases` — Every prior auth request

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID | Primary key |
| `exam_id` | String (unique) | RIS exam ID (deduplication key) |
| `state` | Enum (CaseState) | Current lifecycle state |
| `first_name`, `last_name` | String | Patient name |
| `dob` | String | Date of birth |
| `policy_num` | String | Insurance policy number |
| `center_npi` | String | Facility NPI |
| `cpt_code` | String | Procedure code (e.g., 73721) |
| `icd1`-`icd5` | String | Diagnosis codes |
| `file_key` | String | Azure Blob key for order form PDF |
| `clinical_blob_key` | String | Azure Blob key for clinical notes PDF |
| `portal_case_id` | String | Carelon's internal case ID |
| `auth_number` | String | Authorization number (if approved) |
| `determination_status` | String | APPROVED / PENDED / DENIED |
| `pathway_id` | String | Selected clinical scenario ID |
| `pathway_name` | String | Selected clinical scenario name |
| `auto_approved` | Boolean | Portal auto-approved (no questions or algorithm) |
| `approval_type` | String | gold_card / auto_approved / algorithm / manual |
| `gold_card_level` | Integer | Gold card level (0, 1, 2) |
| `hold_reason` | String | Why case is on hold |
| `is_stat` | Boolean | Urgent case flag |
| `auth_pdf_url` | String | Azure Blob key for auth result/screenshot |
| `signature_replay` | Boolean | Using stored Q&A sequence |
| `signature_id` | UUID | Reference to AlgorithmSignature |
| `rerun_count` | Integer | Number of re-runs |

#### `questions` — Portal questions with AI + rep answers

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID | Primary key |
| `case_id` | UUID (FK) | Reference to case |
| `portal_question_id` | String | Portal's question ID |
| `group_id` | Integer | Question group (0 = pathway selection) |
| `sequence` | Integer | Display order |
| `question_type` | Integer | 2=numeric, 3=single-select, 4=multi-select |
| `question_text` | String | Question text |
| `options_json` | JSON | Available answer options |
| `ai_answer` | JSON | AI's portal-format answer |
| `ai_confidence` | Float | AI confidence (0-100) |
| `ai_evidence` | String | Evidence citations from clinical docs |
| `ai_reasoning` | String | AI's reasoning for the answer |
| `ai_gap` | String | What documentation is missing |
| `ai_notes_answer` | String | Documentation-honest answer (notes path) |
| `ai_notes_confidence` | Float | Notes path confidence |
| `ai_notes_reasoning` | String | Notes path reasoning |
| `ai_approval_gap` | String | Bridge between approval and notes paths |
| `review_state` | Enum | AI_SUGGESTED / REP_APPROVED / REP_EDITED / FLAGGED |
| `review_level` | Integer | 1 = L1 review, 2 = L2 review |
| `rep_answer` | JSON | Rep's answer (if edited) |
| `l1_reviewed_by` | String | L1 rep username |
| `l1_reviewed_at` | DateTime | L1 review timestamp |
| `l1_note` | String | L1 rep's note |
| `l2_reviewed_by` | String | L2 rep username |
| `l2_reviewed_at` | DateTime | L2 review timestamp |

#### `submission_jobs` — Priority queue for portal work

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID | Primary key |
| `case_id` | UUID (FK, unique) | One job per case |
| `priority` | Integer | 1000=STAT, 500=same-day, 200=next-day, 50=standard |
| `job_type` | Enum | FIRST_PASS / SUBMIT / ORDER / SIGNATURE_REPLAY |
| `status` | Enum | QUEUED / CLAIMED / RUNNING / COMPLETED / FAILED |
| `claimed_by` | String | Worker ID that claimed this job |
| `attempt` | Integer | Current attempt number |
| `max_attempts` | Integer | Max retries (default 3) |
| `exception_type` | Enum | STAT_PENDED / RPO_NOT_FOUND / MEMBER_NOT_FOUND / etc. |
| `exception_detail` | String | Error details for rep resolution |

#### `worker_accounts` — Carelon portal login credentials

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID | Primary key |
| `username` | String | Carelon login email |
| `password` | String (encrypted) | Portal password |
| `shift` | Enum | DAY / NIGHT |
| `worker_url` | String | Worker VM URL (e.g., http://172.202.22.112:9081) |
| `mailbox_address` | String | MFA email mailbox |
| `is_active` | Boolean | Account enabled |
| `is_logged_in` | Boolean | Currently authenticated |
| `current_case_id` | UUID | Case being processed |
| `job_type` | String | Current job type |
| `cases_today` | Integer | Cases processed today |

### Supporting Tables

| Table | Purpose |
|-------|---------|
| `clinical_notes` | OCR-extracted clinical document data |
| `algorithm_signatures` | Stored Q&A sequences from approved cases (keyed by CPT+ICD+pathway) |
| `outcome_patterns` | Historical Q&A with outcomes + pgvector embeddings for RAG |
| `bypass_rules` | CPT+ICD patterns that auto-skip L1/L2 review |
| `automation_rules` | CPT+ICD patterns that skip all review (full auto-submit) |
| `system_settings` | Runtime config (key-value, JSONB) |
| `audit_events` | Full audit trail (actor, action, data, timestamp) |
| `execution_logs` | Billing-oriented pipeline events (never deleted) |
| `batches` | Excel upload batch metadata |

### Key Enums

**CaseState:**
`PENDING_NOTES` → `PENDING_STAT` → `WAITING_CLINICALS` → `HOLD` → `NOTES_UPLOADED` → `PROCESSING` → `L1_REVIEW` → `L2_REVIEW` → `SUBMITTING` → `APPROVED` → `DENIED` → `PENDED` → `PENDED_FAX_REVIEW` → `NO_AUTH_REQUIRED` → `CLINICAL_REVIEW` → `ALREADY_WORKED` → `ORDER_READY` → `FAILED`

**ReviewState:** `PENDING` → `AI_SUGGESTED` → `REP_APPROVED` / `REP_EDITED` / `FLAGGED`

**JobStatus:** `QUEUED` → `CLAIMED` → `RUNNING` → `COMPLETED` / `FAILED` / `CANCELLED`

**JobType:** `FIRST_PASS` | `SUBMIT` | `SIGNATURE_REPLAY` | `ORDER`

---

## API Reference

### Queue (Rep Review)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/queue?level=1&source=clinical` | List cases in review |
| GET | `/api/queue/{case_id}` | Get case with questions for review |
| POST | `/api/queue/{case_id}/resolve-l1` | Submit L1 review (approve/edit) |
| POST | `/api/queue/{case_id}/resolve-l2` | Submit L2 review (approve/edit) |
| POST | `/api/queue/{case_id}/rerun` | Trigger re-run (L2 sends back to L1) |
| POST | `/api/queue/{case_id}/flag` | Flag case for attention |
| POST | `/api/queue/{case_id}/send-to-hold` | Move to hold (clear questions) |
| POST | `/api/queue/{case_id}/validate-fax` | Approve/reject fax sending |
| PATCH | `/api/queue/{case_id}/pathway` | Change clinical scenario |
| POST | `/api/queue/{case_id}/already-worked` | Mark as worked in legacy system |
| POST | `/api/queue/{case_id}/confirm-clinical` | Confirm signature replay |
| POST | `/api/queue/{case_id}/confirm-no-auth` | Confirm no-auth determination |

### Cases

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/cases` | List cases (filter by state, center, date) |
| GET | `/api/cases/{case_id}` | Get case detail |
| POST | `/api/cases` | Create case manually |
| DELETE | `/api/cases/{case_id}` | Delete case |
| PATCH | `/api/cases/{case_id}/state` | Update case state |
| POST | `/api/cases/{case_id}/notes` | Upload clinical PDF |
| GET | `/api/cases/{case_id}/auth-pdf` | Download auth result PDF/screenshot |

### Jobs

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/jobs` | List jobs (filter by status, type) |
| GET | `/api/jobs/stats` | Job queue statistics |
| GET | `/api/jobs/workers` | Worker status |
| POST | `/api/jobs/{job_id}/retry` | Retry failed job |

### Analytics

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/analytics/overview` | Dataset stats |
| GET | `/api/analytics/approval-breakdown` | By approval type and CPT |
| GET | `/api/analytics/pathway-intelligence` | Pathway success rates |
| GET | `/api/analytics/outcomes` | Outcome patterns |
| GET | `/api/analytics/overrides` | Rep override stats |
| GET | `/api/analytics/coverage` | CPT x ICD combo depth |
| GET | `/api/analytics/submission-signatures` | Signature replay metrics |

### Settings

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/settings` | All settings |
| PUT | `/api/settings/{key}` | Update a setting |
| GET | `/api/settings/prompts` | LLM prompt templates |
| POST | `/api/settings/prompts/{key}` | Update prompt template |
| POST | `/api/settings/flush` | Preview or execute case flush |
| POST | `/api/settings/sync` | Trigger Mongo→Postgres sync |

---

## Environment Variables

Create a `.env` file at the project root (see `.env.example` for template).

### Database & Cache
```bash
DATABASE_URL=postgresql+asyncpg://ronexa:password@postgres:5432/ronexa
REDIS_URL=redis://redis:6379/0
```

### Restate
```bash
RESTATE_URL=http://restate:8080              # Ingress (worker registration)
RESTATE_ADMIN_URL=http://restate:9071        # Admin API (prod: 9071, dev: 9070)
RESTATE_SELF_URI=http://restate-handler:9080 # Handler callback URI
```

### Azure (Clinical Documents)
```bash
AZURE_BLOB_CONNECTION_STRING=DefaultEndpointsProtocol=https;AccountName=...
AZURE_BLOB_CONTAINER=carelon-attachments
AZURE_DOC_INTELLIGENCE_ENDPOINT=https://centralus.api.cognitive.microsoft.com/
AZURE_DOC_INTELLIGENCE_KEY=your-key
```

### MongoDB (Cosmos DB)
```bash
MONGO_URI=mongodb+srv://user:pass@host/?tls=true&tlsAllowInvalidCertificates=true
MONGO_DB=workflowdb
MONGO_COLLECTION=auth-submissions
```

### LLM API Keys
```bash
ANTHROPIC_API_KEY=sk-ant-api03-...    # Primary (Claude)
GOOGLE_API_KEY=AIzaSy...              # Fallback (Gemini)
OPENAI_API_KEY=sk-proj-...            # RAG embeddings
```

### Carelon Portal
```bash
CARELON_BASE_URL=https://www.providerportal.com
CARELON_REDIRECT_URI=https://www.providerportal.com/interaction-code/callback
CARELON_AUTHENTICATOR_ID=aut5t66pqwcf91LGc4h7
```

### MFA (Microsoft Graph API)
```bash
MFA_TENANT_ID=your-tenant-id
MFA_CLIENT_ID=your-client-id
MFA_CLIENT_SECRET=your-client-secret
MFA_MAILBOX_1=carelon-mfa@yourdomain.com
```

### Okta
```bash
OKTA_CLIENT_ID=0oa97idukvkwwIVul4h7
OKTA_DOMAIN=login.mbm.partners.carelon.com
```

### Portal Worker Accounts
```bash
CARELON_USERNAME_1=user1@envision.com
CARELON_PASSWORD_1=password
CARELON_USERNAME_2=user2@envision.com
CARELON_PASSWORD_2=password
```

### Feature Flags
```bash
REQUIRE_REP_REVIEW=true    # Require human review before submission
HEADLESS=true              # Run Playwright in headless mode
ENVIRONMENT=production     # production | docker | local
```

---

## Development Setup

### Prerequisites

- **Python 3.12+**
- **Node.js 18+**
- **Docker + Docker Compose**
- **Playwright** (installed via pip, includes Chromium)

### 1. Clone and install

```bash
git clone <repo-url>
cd ronexa-sub

# Backend
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# Frontend
cd ../frontend
npm install
```

### 2. Environment

```bash
cp .env.example .env
# Fill in all required values (see Environment Variables section)
```

### 3. Start infrastructure

```bash
docker compose up -d postgres redis restate
```

### 4. Run migrations

```bash
cd backend
alembic upgrade head
```

### 5. Start backend

```bash
# Terminal 1: FastAPI server
cd backend
uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# Terminal 2: Restate handler
cd backend
python restate_worker.py
```

### 6. Start frontend

```bash
cd frontend
npm run dev
# Opens at http://localhost:3000
```

### 7. Register Restate handler

```bash
curl -X POST http://localhost:9070/deployments \
  -H 'Content-Type: application/json' \
  -d '{"uri": "http://host.docker.internal:9080", "force": true}'
```

---

## Deployment

### deploy.sh — The Single Deploy Command

**Always use `deploy.sh` for deployment.** Never manually SCP files as a permanent fix.

```bash
# Full deploy (builds images, deploys orchestrator + workers, re-registers Restate)
./deploy.sh all

# Backend only (image rebuild + workers + Restate cleanup)
./deploy.sh backend

# Frontend only (image rebuild + orchestrator)
./deploy.sh frontend

# Workers only (SCP code, no image rebuild — emergency hotfix only)
./deploy.sh workers

# Check current production status
./deploy.sh status
```

**What `deploy.sh all` does:**
1. Checks for uncommitted changes (warns if found)
2. Auto-increments version tags from production
3. Builds Docker images (backend + frontend)
4. Pushes to Azure Container Registry (ACR)
5. Kills in-flight Restate invocations (prevents stale workflow code)
6. Deploys orchestrator (docker-compose pull + restart)
7. SCPs backend code to all 3 worker VMs + restarts services
8. Re-registers Restate handler deployment
9. Runs health checks on all VMs

### Post-Deploy Checklist

```bash
# 1. Run migrations (if any new ones)
ssh ronexa@20.29.73.195 'docker exec backend-api alembic upgrade head'

# 2. Check WorkerLoops are running
./deploy.sh status

# 3. Monitor first few cases
ssh ronexa@20.29.73.195 'docker logs -f restate-handler --tail=100'
```

### Infrastructure

| VM | IP | Role | Services |
|----|-----|------|---------|
| Orchestrator | 20.29.73.195 | API + workflows | nginx, frontend, backend-api, restate, restate-handler, postgres, redis |
| Worker-A | 172.202.22.112 | Browser automation | worker_http.py (port 9081) + Playwright |
| Worker-B | 74.249.202.85 | Browser automation | worker_http.py (port 9081) + Playwright |
| Worker-C | 20.106.37.51 | Browser automation | worker_http.py (port 9081) + Playwright |

**ACR:** `ronexaacr.azurecr.io`

---

## Common Operations

### Check what's running in production

```bash
./deploy.sh status
```

### View worker logs

```bash
# Orchestrator (API + workflows)
ssh ronexa@20.29.73.195 'docker logs -f restate-handler --tail=200'
ssh ronexa@20.29.73.195 'docker logs -f backend-api --tail=200'

# Worker VM
ssh ronexa@172.202.22.112 'journalctl -u ronexa-worker-http -f'
```

### Retry a failed case

Option A: Via the Worklist page in the dashboard

Option B: Via API:
```bash
curl -X POST https://your-domain/api/jobs/{job_id}/retry
```

### Add a worker account

Go to Settings → Workers tab → "Add Worker Account"

Or via API:
```bash
curl -X POST https://your-domain/api/settings/workers \
  -H 'Content-Type: application/json' \
  -d '{
    "username": "user@envision.com",
    "password": "...",
    "shift": "DAY",
    "worker_url": "http://172.202.22.112:9081",
    "mailbox_address": "carelon-mfa@yourdomain.com"
  }'
```

### Change LLM prompts

Go to Settings → General tab → Prompt Management

Prompts are stored in the `system_settings` table and can be edited via the UI. Changes take effect immediately (no deploy needed). Click "Reset to Default" to restore the hardcoded template from `backend/app/intelligence/llm_config.py`.

### Manual Mongo sync

```bash
curl -X POST https://your-domain/api/sync/trigger
```

Or via Settings → General → Cache & Sync → "Sync Now"

---

## Troubleshooting

### Portal timeouts during deploy

**Symptom:** Cases go to HOLD with "Portal timeout" errors clustered at the same timestamp.

**Cause:** `deploy.sh all` kills in-flight Restate invocations and restarts workers. Any case being processed at that moment will timeout.

**Fix:** No action needed. These cases will be retried automatically. Check the timestamps — if they cluster around deploy time, it's deploy collateral, not a code bug.

### "Member not found" on first attempt

**Symptom:** Case goes to HOLD with "Member not found in Carelon portal."

**Current handling:** The system retries once — navigates to the portal homepage and re-searches. If still not found, it goes to HOLD.

**Manual fix:** Check if the patient name/DOB matches what's in Carelon. Go to Worklist → Member Not Found → update info and retry.

### MFA failures

**Symptom:** Login fails with "OTP not received" or "MFA timeout."

**Common causes:**
1. **Stale OTP emails** — The `prepare()` step should flush these, but check the mailbox
2. **Graph API permissions** — Verify the Azure AD app has `Mail.Read` on the shared mailbox
3. **Rate limiting** — Too many MFA requests in a short time. Wait a few minutes.

**Debug:** Check worker logs for `MFAResolver` entries:
```bash
ssh ronexa@172.202.22.112 'journalctl -u ronexa-worker-http | grep MFA'
```

### Fax modal not handled

**Symptom:** Cases go to HOLD with "CPT not found in AvailableCptCodes" after provider selection.

**Cause:** The "Ordering Provider Fax Number" popup appeared but wasn't dismissed. The clinical SPA won't initialize until this popup is closed.

**Fix (already implemented):** `_handle_fax_modal()` in `webforms_client.py` handles this automatically. If you see this error, check that the fax modal handler is detecting the popup correctly.

### SubmitWorkflow "No approved answers found"

**Symptom:** Auto-approved cases (0 questions) fail with "No approved answers found for submission."

**Fix (already implemented):** `submit_workflow.py` checks `case.auto_approved` before rejecting empty answer lists.

### Browser session stuck

**Symptom:** Worker shows `browsers=1` but isn't processing cases.

**Fix:**
```bash
# Close the stuck browser
curl -X POST http://172.202.22.112:9081/close-browser

# Restart the worker service
ssh ronexa@172.202.22.112 'sudo systemctl restart ronexa-worker-http'
```

---

## Key Design Decisions

### Why in-page `fetch()` instead of external HTTP?

Carelon's portal uses Akamai Web Application Firewall (WAF). External HTTP requests to the ClinicalFacade API would be blocked. By running `fetch()` inside the Playwright browser page, all traffic appears as organic browser activity — same cookies, same session, same origin. The WAF never sees external API calls.

### Why two-job architecture?

Separating "discover questions" (Job 1) from "submit answers" (Job 2) creates a clean state machine:
- No workflow suspension during rep review (no awakeables to manage)
- Rep review is a simple CRUD operation (update Question records in Postgres)
- Submitting triggers a new job — the workflow starts fresh with a clean browser
- Reruns just re-enqueue Job 1 with a `changed_group_id` flag

### Why Restate instead of Celery/Kafka?

Restate provides durable execution guarantees without external infrastructure:
- No message broker (Kafka, RabbitMQ) to manage
- Automatic retries with journaled state
- Virtual Objects for per-worker state management
- Built-in admin UI for monitoring invocations
- All state in Postgres (single source of truth)

### Why Postgres-native priority queue?

The `SubmissionJob` table uses `SELECT FOR UPDATE SKIP LOCKED` for exactly-once job claiming:
- No external queue service (SQS, Redis Queue) needed
- Priority is a simple integer column — STAT cases always go first
- Transactional consistency with case state updates
- Easy to query, debug, and audit

### Why dual-answer LLM?

The portal's approval algorithm has specific answer patterns that trigger approval. But the "right" answer for approval isn't always the most honest answer based on clinical documentation. The dual-answer system:
- **Approval path:** Optimizes for portal algorithm satisfaction
- **Notes path:** Gives the documentation-honest answer
- **Gap analysis:** When they differ, explains what clinical evidence would bridge the gap
- This gives reps transparency — they can see when the AI is "stretching" and make informed decisions

### Why human review (L1 + L2)?

Even though the LLM achieves high accuracy, authorization decisions have real clinical and financial consequences. The two-level review provides:
- **L1:** First-pass review by a junior rep (catch obvious errors)
- **L2:** Senior review before portal submission (final quality gate)
- Both levels are toggleable via Settings (can bypass L1, L2, or both for certain CPT+ICD patterns)
- This builds the training data that improves the LLM over time (outcome patterns + algorithm signatures)
