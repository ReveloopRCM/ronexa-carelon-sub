# Handoff — March 22, 2026

## What was accomplished today

### 1. Restate Journal Replay Fix — 5 Logins → 2

**The problem:** Restate's journal replay caused `compiler.execute()` to re-run on every handler re-invocation because it wasn't wrapped in `ctx.run()`. This produced 5 login cycles per case (3 wasted on replays).

**The fix:** Created `_run_compiler_pass()` helper function in `prior_auth_workflow.py` that wraps the full browser lifecycle (acquire → compile → close) in a single callable. The workflow now calls:
- `ctx.run("first_pass", _run_compiler_pass, ...)` — journaled, skipped on replay
- `ctx.run("finalize_pass", _run_compiler_pass, ...)` — journaled, only runs after rep approval

**Result:** Exactly 2 logins per case (first pass + finalize), down from 5. ~6 minutes saved per case.

**Files modified:**
| File | Change |
|------|--------|
| `backend/app/workflow/prior_auth_workflow.py` | Added `_run_compiler_pass()` helper; refactored `process_case` to wrap both compiler passes in `ctx.run()` |

### 2. Duplicate Auth Check — Fixed Broken Matching + Early Abort

**The problem (matching):** `check_duplicate_auth()` did `cpt_code in exam_description` (e.g., `"70551" in "Brain (Includes IACs, Pituitary) - MRI"`) which **never matched**. The portal grid shows body-part descriptions, not CPT codes. The duplicate check was effectively non-functional — this is why Robin Stuart's existing brain MRI auth (283083690) wasn't caught.

**The problem (timing):** Even if matching worked, the check only ran in the workflow AFTER the full compiler pass (login + all phases + LLM questions). A duplicate would waste an entire portal session.

**The fix (matching):** Description-based matching using the CPT catalog's SearchText:
- Catalog: `"70551 MRI - Brain (Includes IACs, Pituitary) - MRI"`
- Grid: `"Brain (Includes IACs, Pituitary) - MRI"`
- Parse body-part from SearchText (split on ` - `, take everything after first separator)
- Compare against grid's `exam_description` (case-insensitive)
- Added `"authorized"` to active status set (portal uses "Authorized" not "Approved")
- Keeps CPT-code-in-description as fallback

**The fix (timing):** Moved check into compiler, right after `clinical_flow.initialize()` (which loads the CPT catalog). Duplicate detected → HOLD returned immediately, before exam setup / diagnosis / pathway / LLM questions.

**Verified with real data:**
```
Old check (no search text): duplicate=False  ← BROKEN, missed Robin Stuart
New check (with search text): duplicate=True  ← FIXED, catches auth 283083690
Knee MRI against brain/abdomen: duplicate=False  ← Correct, no false positive
Abdomen MRI: duplicate=True  ← Correct, catches auth 272613813
```

**Files modified:**
| File | Change |
|------|--------|
| `backend/app/portal/clinical_flow.py` | Fixed `check_duplicate_auth()` — added `cpt_search_text` param, description-based matching |
| `backend/app/compiler/portal_compiler.py` | Added early duplicate check after `clinical_flow.initialize()`, before exam setup |
| `backend/app/workflow/prior_auth_workflow.py` | Pass `cpt_search_text` to workflow-level safety-net check |

### 3. HAR Analysis — Existing Auths Grid Structure Confirmed

Analyzed 3 HAR files to understand the existing authorizations page:
- **Order IDs are NOT clickable** — plain `<span>` elements, no View hyperlink
- **Grid is read-only** — only actions are column sorting and "Start Order Request" / Next
- **7 columns:** Order ID, Order Status, Date of Service, Exam Description, Ordering Provider, Outcome, Reason
- **exam_description maps to CPT catalog SearchText** — this is how matching works

### 4. Frontend — HOLD Reason Display

**The problem:** When a case was put on HOLD (member not found, duplicate auth, etc.), the frontend showed an amber "HOLD" badge but didn't display the reason. Reps had to check audit events to understand why.

**The fix:**
- **Case detail page:** Added amber banner showing `hold_reason` when `state === "HOLD"` (same pattern as WAITING_CLINICALS and denial_reason banners)
- **Cases list (Active tab):** Shows `hold_reason` text inline next to the HOLD badge for at-a-glance triage

**Files modified:**
| File | Change |
|------|--------|
| `frontend/app/cases/[caseId]/page.tsx` | Added HOLD banner with hold_reason |
| `frontend/app/cases/page.tsx` | Show hold_reason inline on Active tab for HOLD cases |

## Current State

- **Restate journal fix:** Code complete, not yet tested live (needs E2E run)
- **Duplicate auth fix:** Code complete, verified with unit test using real Robin Stuart data
- **Frontend HOLD display:** Code complete, not yet visually verified (needs dev server)
- **All Python files pass syntax check**

## Files Modified This Session

| File | Change |
|------|--------|
| `backend/app/workflow/prior_auth_workflow.py` | `_run_compiler_pass()` helper + `ctx.run()` wrapping + `cpt_search_text` passthrough |
| `backend/app/portal/clinical_flow.py` | `check_duplicate_auth()` — description-based matching with `cpt_search_text` |
| `backend/app/compiler/portal_compiler.py` | Early duplicate check after `clinical_flow.initialize()` |
| `frontend/app/cases/[caseId]/page.tsx` | HOLD banner with hold_reason |
| `frontend/app/cases/page.tsx` | hold_reason inline on Active tab |

## Next Steps

1. **Test Restate E2E with journal fix** — Run on a clean case (no existing auths for the CPT). Should see exactly 2 logins instead of 5. Verify suspend → approve → resume → finalize still works.
2. **Test duplicate auth early abort** — Run on Robin Stuart (CPT 70551, existing brain MRI auth). Should HOLD at clinical_init phase, not proceed to questions.
3. **Verify frontend** — Start Next.js dev server, check HOLD cases display the hold_reason banner.
4. **Handle "member not found" production flow** — Backend already handles it (HOLD + reason). Frontend now shows it. May want a more specific state (`MEMBER_NOT_FOUND`) vs generic HOLD — decide later based on volume.
5. **Test actual submission** — Click "Submit This Request" on a controlled case.
6. **Purge stale Restate invocations** from previous failed runs.

## Test Cases Summary

| Patient | CPT | Test Type | Result |
|---------|-----|-----------|--------|
| Winona Sandlin | 75574 (Cardiac CT) | Live test Steps 1-19 | ✅ Full pass (March 19) |
| Jonathan Horne | 73721 (Knee MRI) | Live test Steps 1-19 | ✅ Full pass (March 19) |
| Robin Stuart | 70551 (Brain MRI) | Live test Steps 1-19 | ✅ Full pass (March 21) |
| Robin Stuart | 70551 (Brain MRI) | Restate E2E workflow | ✅ Through hdnAction=20, ❌ hdnAction=6 (duplicate auth) |
| Robin Stuart | 70551 (Brain MRI) | Duplicate auth unit test | ✅ New matching correctly detects auth 283083690 |
| Kristal Shackelford | 72148 (Lumbar MRI) | Live test | ❌ Member not found |
| Oshodi Olumide | 73721 (Knee MRI) | Live test | ❌ Existing auth blocks CPT |
| Elliot Pershing | 73721 (Knee MRI) | Live test | ❌ Existing auth blocks CPT |

## Key Technical Details

### Description-Based Auth Matching
```
CPT Catalog SearchText format: "{CPT} {modality} - {body_part_description}"
  e.g. "70551 MRI - Brain (Includes IACs, Pituitary) - MRI"

Portal Grid exam_description: "{body_part_description}"
  e.g. "Brain (Includes IACs, Pituitary) - MRI"

Match: split SearchText on " - " (first occurrence), take remainder, compare case-insensitive
```

### Compiler Duplicate Check Location
```
Phase order: member_search → eligibility → start_order → check_existing_auths → provider_search → clinical_exam_setup → ...

Duplicate check runs INSIDE clinical_exam_setup, right after initialize() loads the CPT catalog.
This is BEFORE: exam setup, diagnosis, pathway, questions, finalize — saving all that work on duplicates.
```

### _run_compiler_pass() Helper
```python
async def _run_compiler_pass(center_npi, case_data, clinical_context, dry_run, resume_answers):
    # Acquire browser → login → compile → close browser
    # Returns serializable dict (journaled by ctx.run)
    # Browser always closed in finally block (no session leak during suspension)
```

---

## Session 2 — Speed Optimization + Production Scale Strategy

### 3. Portal Speed Optimization — Senior Rep Timing

**Problem:** Timing was calibrated for caution (bot detection avoidance) rather than matching a senior auth rep who knows exactly where every field is.

**Changes:**

| File | Change | Savings |
|------|--------|---------|
| `backend/app/auth/okta_login.py` | Replaced 8 fixed `sleep()` calls with selector-gated `wait_for_selector()`. Removed `sleep(2)` after page load, `sleep(3)` after username, `sleep(2)+sleep(4)` around Okta Next. Password retry uses `wait_for_selector` instead of `sleep(5)+sleep(3)`. Post-login sleeps reduced from 2-3s → 0.5-1s | **~15-18s per login** |
| `backend/app/portal/behavior_engine.py` | `searchResult` think time: `(300, 70)` → `(180, 40)` | **~120ms per search click** |
| `backend/app/portal/clinical_client.py` | `API_PACE_MS`: `1000` → `400` | **~15s across ~25 clinical API calls** |
| `backend/app/portal/webforms_client.py` | Removed extra `think("formField")` after provider search radio click | **~150ms** |

**Estimated total savings: ~30-35s per case**, two logins per E2E = ~60-70s faster overall.

### 4. Production Throughput Strategy — 500-1000 Submissions/Day

**The winning combination: Restate + Multiple Logins + Containers**

| Component | Role |
|-----------|------|
| **Restate** | Durable fan-out, automatic retry, journal replay (no lost work on crash) |
| **Multiple logins (5-20)** | Each user = independent browser session + own MFA mailbox, no contention |
| **Worker containers** | Isolate browsers, scale horizontally, fail independently |

**Why each piece is needed:**

| Without | Problem |
|---------|---------|
| No Restate | Crash mid-case = lost work, manual restart |
| 1 login | Serial only — 500 × 3 min = 25 hours |
| 1 container | OOM kills all sessions, can't scale past 1 machine |

**Architecture:**
```
┌──────────┐
│ Restate  │──── routes by VO key (user_id)
│ (router) │
└──────────┘
     │
     ├──► Worker-1 (4 users, 4 browsers, 2GB/2CPU)
     ├──► Worker-2 (4 users, 4 browsers, 2GB/2CPU)
     ├──► Worker-3 (4 users, 4 browsers, 2GB/2CPU)
     ├──► Worker-4 (4 users, 4 browsers, 2GB/2CPU)
     └──► Worker-5 (4 users, 4 browsers, 2GB/2CPU)
```

**Key architectural shift:** VO key changes from NPI → user_id. One user processes cases across multiple NPIs. NPI is a data field, not a session boundary.

**Throughput projections:**

| Config | Users | Containers | Cases/user | Time/case | Wall clock |
|--------|-------|-----------|-----------|-----------|------------|
| Conservative | 5 | 2 | 100 | 5 min | ~8.5 hrs |
| **Target** | **10** | **3** | **50** | **3 min** | **~2.5 hrs** |
| Aggressive | 20 | 5 | 25 | 2.5 min | **~65 min** |

**Infrastructure: Azure Container Apps** (already on Azure for Cosmos DB, Blob, Graph API)
- Scale to zero when no submissions queued
- Cron-based scale: spin up at 2 AM, scale down at 8 AM
- Each container: 2GB RAM, 2 vCPU, Playwright + Chrome pre-installed

**Multi-user MFA:** Each user gets their own shared mailbox + Graph API app registration. User onboards via frontend signup → backend script provisions mailbox automatically. No MFA cross-contamination since each user polls their own inbox.

**ViewState constraint (from old codebase analysis):** ASP.NET WebForms `__VIEWSTATE` is per-tab. Can't run 2 cases simultaneously on the same login. The old `carelon-workflow-automation` used multi-tab for read-only extraction, but submission is write-heavy — must be serial per user.

### Reference: Old Codebase Analysis

Reviewed `/Users/andrewntuyo/Desktop/carelon-workflow-automation/` for patterns to borrow:

| Pattern | Old Way | New Way (Restate) |
|---------|---------|-------------------|
| Parallelism | `asyncio.Semaphore(2)` + `asyncio.gather()` multi-tab | Restate fan-out across UserWorker VOs |
| Progress tracking | File-based JSON + locks | Restate journal (durable, automatic) |
| Session reuse | Login decorator restores context state | VO `_ensure_session()` validates + re-logs |
| Retry | Failed cases re-batched, configurable attempts | Restate automatic retry with backoff |
| Resume after crash | Checkpoint files, manual `resume_workflow.py` | Restate journal replay (automatic) |

**Key takeaway from old code:** The extraction step (`carelon_case_extraction_step.py`, ~3400 lines) proves multi-tab works for **reading** portal data. But our submission flow (member search → eligibility → start order → clinical → facility → submit) mutates server-side ViewState, so it must be **serial per login**. Scale comes from more logins, not more tabs.

---

## Session 3 — Production Strategy Finalized + Dashboard Design

### 5. Production Strategy — Finalized on Plane

**Three principles:**
1. **Stream, don't batch** — Cases arrive from RIS every 15 min, process 24/7. Overnight cases already submitted by morning.
2. **Simulate a real auth team** — 6 Carelon accounts on 12hr shift rotations (day/night), persistent browser profiles, jittered shift transitions. Indistinguishable from 6 real reps.
3. **Automate straight-through, empower humans on exceptions** — 80%+ cases end-to-end. 20% exceptions handed off with full context pre-loaded.

**Infrastructure: 4 Azure Containers × 2 accounts each = 8 accounts**
```
Container 1  →  STAT/Teal lane (priority)
Container 2  →  Standard lane
Container 3  →  Standard lane
Container 4  →  Standard lane (headroom)

Each container:
  Account A  →  Day shift   (~6am – 6pm, ±15min jitter)
  Account B  →  Night shift (~6pm – 6am, ±15min jitter)
```

**Corrected throughput math** (ViewState = serial per login):
```
4 active accounts × 15 cases/hr = 60 cases/hr
60 × 24 hours = 1,440 cases/day capacity
Headroom over 1,000/day target: 1.44×
```

**Priority scoring (Postgres queue, SELECT FOR UPDATE SKIP LOCKED):**
```
STAT/Teal                →  1000  (immediate)
Standard, same-day DOS   →  500
Standard, next-day DOS   →  200
Pending follow-up        →  100   (Day 2 carrier checks, 7am)
Standard, future DOS     →  50    (fill overnight gaps)
```

**Exception taxonomy (each tied to Restate awakeable):**

| Exception | Automation Does | Rep Does |
|-----------|----------------|----------|
| STAT pended | Submits, captures case#, fires awakeable | Calls Carelon nurse reviewer, enters outcome (2 min) |
| RPO not found | Attempts expanded search, fires awakeable | Manually enters provider info (1 min) |
| Med necessity popup | Pastes LLM narrative, continues | Reviews/edits narrative if needed |
| Member not found | Flags case, fires awakeable | Updates member info or routes to manual |
| Duplicate auth | Detects early, HOLD state | Skip or override |

**LLM pipeline:**
- LLM1: Clinical document extraction (Haiku vision) — runs before submission
- LLM2: Clinical pathway answer generation (Anthropic + Gemini) — consumed at Step 6
- RAG (pgvector): Every submission feeds back, similar cases inform future extractions

### 6. Auth Operations Dashboard Design

**5-tab layout:** Dashboard | Queues | Worklist | Cases | Review

- **Dashboard** — Today's throughput (submitted/target), queue depths, worker status, auto-approval rate
- **Queues** — STAT and Standard sub-tabs, priority-sorted, age column (STAT >30min = red)
- **Worklist** — Left sidebar: exception categories with count badges. Right panel: case detail + action button. Each action resolves a Restate awakeable → workflow resumes automatically
- **Cases** — Existing case list + detail (HOLD banner, flow check cards)
- **Review** — Existing LLM answer review queue (moved to own tab)

**Key principle:** Every worklist item is a Restate awakeable. Rep clicks action → API resolves awakeable → case moves. No polling, no manual retry.

## Build Order (when resuming)

**Backend first, then dashboard.**

1. **Priority queue table + scoring** — `submission_jobs` with `SELECT FOR UPDATE SKIP LOCKED`, priority enum
2. **UserWorker VO** — Replace NPI-keyed `BrowserSession` with user_id-keyed VO (owns credentials, mailbox, browser profile)
3. **Batch dispatcher** — Restate fan-out: query queue → assign to available workers → execute
4. **Shift manager** — Account A/B rotation, clean logout on transition, jittered start
5. **Auth Operations Dashboard** — Dashboard tab, Queues tab, Worklist tab, wire to backend APIs
6. **Dockerfile + Container Apps** — Playwright base image, persistent profile volumes, scaling config

**What's already built and proven:**
- Login + MFA (okta_login.py + mfa_resolver.py) ✅
- All 19 WebForms steps (webforms_client.py + clinical_flow.py) ✅
- Compiler (portal_compiler.py) ✅
- Restate workflow with awakeables (prior_auth_workflow.py) ✅
- LLM1 extraction (extractor.py) ✅
- LLM2 pathway answers (evaluator.py) ✅
- Mongo sync + blob fetch ✅
- Duplicate auth check (just fixed) ✅
- Speed optimization (just done) ✅

**Start with:** Priority queue table + scoring → then UserWorker VO.

---

## Session 4 — Settings Page, LLM Pipeline, Sync Engine, Frontend Integration

### 7. Settings Page — Complete

Built full settings page at `/settings` with live backend connection:
- **Mongo Polling** — Enable/disable toggle, interval (min), extract toggle, limit, Sync Now button
- **Database Flush** — Preview dialog showing state breakdown, protected states (IN_REVIEW, PROCESSING, SUBMITTING), confirmation before delete
- **Sync History** — Last 10 sync runs with fetched/new/dupes/extracted/enqueued/errors
- **Tested live:** Flushed 177 old test cases (2 IN_REVIEW protected), all working

### 8. LLM Pipeline — Workflow Node View

Redesigned LLM config as visual workflow pipeline with connected nodes:
```
PDF Upload → Clinical Extraction (LLM1) → Pathway Q&A (LLM2) → RAG Embeddings
```
Each node: provider dropdown (Anthropic/Google), dynamic model dropdown per provider, System/User Prompt buttons. Prompt editor hidden by default, opens on click with Jinja2 variable hints.

API Keys moved to bottom as vertical list with status dots (green=set, red=missing) and descriptions.

### 9. Shared Sync Engine

Extracted sync logic from `sync.py` route into `app/ingest/sync_engine.py` — shared by:
- `POST /api/sync` (manual endpoint)
- `POST /api/settings/sync-now` (settings page)
- `poll_scheduler.py` (background interval)

Updated `sync_engine.py` to enqueue ALL new cases (NOTES_UPLOADED + PENDING_NOTES) — cases without attachments enter Restate workflow which handles retry + extraction.

### 10. Frontend Integration Decision — Reverse Proxy (Option 2)

**Decision:** Keep both frontends (Remix + Next.js) behind the same domain via reverse proxy. Zero rewrites. Deploy both to Azure Container Apps.

**Why not port to Remix:** Too risky before production. Working UI code, different frameworks (Tailwind vs custom CSS, Next.js vs Remix data loading). Consolidate after production is stable.

**Routing:**
```
ronexa.domain.com/
├── /auth-ops/*     → Next.js (auth operations dashboard)
├── /api/*          → FastAPI backend
└── /*              → Remix (main ronexa app — LLM workflows, admin)
```

**Shared auth:** Remix login sets session cookie on domain. Next.js reads same cookie. One login, both apps.

**Navigation:** "Auth Operations" link in Remix nav, "Admin" link in Next.js nav. Regular `<a>` tags (cross-app, not SPA).

### Files Created/Modified This Session

| File | Action | Purpose |
|------|--------|---------|
| `backend/app/db/models.py` | Modified | Added `SystemSetting` model |
| `backend/alembic/versions/003_system_settings.py` | Created | Migration + seed 11 defaults |
| `backend/app/api/routes/settings.py` | Created | Settings CRUD + flush + sync-now + prompt-reset |
| `backend/app/ingest/poll_scheduler.py` | Created | Background asyncio polling loop |
| `backend/app/ingest/sync_engine.py` | Created | Shared sync logic (used by route + scheduler + settings) |
| `backend/app/api/routes/sync.py` | Modified | Simplified to use sync_engine |
| `backend/main.py` | Modified | Registered settings router + poll scheduler in lifespan |
| `frontend/app/settings/page.tsx` | Created | Settings page — polling, flush, LLM pipeline, prompts, API keys |
| `frontend/app/layout.tsx` | Modified | Added Settings nav link |
| `frontend/lib/api.ts` | Modified | Added settings + flush + sync API functions |
| `.claude/launch.json` | Modified | Added backend server config |

---

## Azure Deployment Plan

### Container Topology

```
Azure Container Apps Environment
├── Container: nginx-proxy          (Nginx, routes by path prefix)
│   └── Port 80/443 — public-facing
├── Container: ronexa-frontend      (Remix, port 5173)
│   └── Main ronexa app — LLM workflows, admin, login
├── Container: auth-ops-frontend    (Next.js, port 3000)
│   └── Auth operations — queues, worklist, cases, settings
├── Container: backend-api          (FastAPI/Hypercorn, port 8000)
│   └── All /api/* routes, poll scheduler
├── Container: worker-1             (Playwright + Chrome, port 9080)
│   └── Restate worker — 2 Carelon accounts (day/night shift)
├── Container: worker-2             (Playwright + Chrome, port 9081)
│   └── Restate worker — 2 Carelon accounts
├── Container: restate-server       (Restate, port 8080/9070)
│   └── Workflow orchestration, journal storage
└── Managed Services:
    ├── Azure PostgreSQL Flexible Server
    ├── Azure Cache for Redis
    ├── Azure Cosmos DB (existing — Envision source)
    └── Azure Blob Storage (existing — clinical PDFs)
```

### Dockerfiles Needed

1. `backend/Dockerfile` — Python 3.12 + FastAPI + dependencies
2. `backend/Dockerfile.worker` — Python 3.12 + Playwright + Chrome + Restate SDK
3. `frontend/Dockerfile` — Node 18 + Next.js production build
4. `infra/nginx/Dockerfile` — Nginx + routing config
5. `infra/restate/Dockerfile` — Restate server (or use official image)

### Dockerfiles Created ✅

1. `backend/Dockerfile` — Python 3.12 + FastAPI + auto-migrations
2. `backend/Dockerfile.worker` — Python 3.12 + Playwright + Chrome + persistent profiles
3. `frontend/Dockerfile` — Node 18 + Next.js standalone build
4. `infra/nginx/Dockerfile` — Nginx Alpine + routing config
5. `docker-compose.yml` — Full local stack (Postgres, Redis, Restate, backend, 2 workers, frontend, nginx)
6. `infra/deploy.sh` — One-shot Azure CLI deployment script
7. `.env.example` — Template for all required env vars

### 11. Clinical OCR — Azure Document Intelligence Integration

**Problem:** Haiku Vision doing OCR + interpretation in one shot is mediocre on fax-quality scans, handwriting, tables.

**Solution:** Two-stage pipeline:
```
Stage 1: Azure AI Document Intelligence (prebuilt-read model)
   → Clean text, tables, structure, confidence scores
   → $1.50 per 1000 pages — purpose-built for degraded documents

Stage 2: Gemini 2.5 Flash (interpretation)
   → Takes clean text → structured clinical JSON
   → Prompt-tunable per provider format
```

**Fallback:** If Azure Doc Intelligence not configured (no endpoint/key), falls back to legacy single-stage vision extraction (Haiku/Gemini).

**Default LLM routing updated:**
- Eval (pathway Q&A): **Gemini 2.5 Pro** (primary)
- Extract (clinical interpretation): **Gemini 2.5 Flash** (cost-efficient)
- Anthropic: available as fallback in settings

**Files modified:**
| File | Change |
|------|--------|
| `backend/app/intelligence/extractor.py` | Two-stage pipeline: `_extract_with_document_intelligence()` → `_interpret_with_llm()`, with vision fallback |
| `backend/app/core/settings.py` | Added `AZURE_DOC_INTELLIGENCE_ENDPOINT` + `AZURE_DOC_INTELLIGENCE_KEY` |
| `backend/app/intelligence/llm_config.py` | Defaults changed to Gemini Pro/Flash |
| `backend/requirements.txt` | Added `azure-ai-documentintelligence`, `cryptography`, `jinja2` |
| `.env.example` | Added Doc Intelligence vars |
| `docker-compose.yml` | Added Doc Intelligence env vars to backend-api |

---

## Azure Portal — Step-by-Step Deployment Guide

### Step 1: Resource Group
1. Azure Portal → "Resource groups" → Create
2. Name: `rg-ronexa-prod`, Region: **Central US**

### Step 2: Container Registry (ACR)
1. "Container registries" → Create
2. Name: `ronexaacr`, SKU: Basic ($5/mo)
3. After created → Settings → Access keys → Enable Admin user
4. Copy: Login server, Username, Password

### Step 3: Azure AI Document Intelligence
1. Search **"Document Intelligence"** (or "Form Recognizer") → Create
2. Resource group: `rg-ronexa-prod`
3. Name: `ronexa-doc-intel`
4. Region: **Central US**
5. Pricing tier: **S0** (Standard — $1.50/1000 pages for Read model)
6. **Create**
7. After created → **Keys and Endpoint** → Copy:
   - `AZURE_DOC_INTELLIGENCE_ENDPOINT` = the endpoint URL
   - `AZURE_DOC_INTELLIGENCE_KEY` = Key 1

### Step 4: PostgreSQL
1. "Azure Database for PostgreSQL" → Create → Flexible Server
2. Name: `ronexa-pg`, Admin: `ronexa`, Password: generate + save
3. Compute: Burstable B1ms ($13/mo), Storage: 32GB, Version: 16
4. Networking: Allow public access + Azure services
5. After created → Databases → Add → `ronexa`
6. Server parameters → `azure.extensions` → add `vector` → Save

### Step 5: Redis
1. "Azure Cache for Redis" → Create
2. Name: `ronexa-redis`, SKU: Basic C0 ($16/mo)
3. After created → Access keys → Copy Primary connection string

### Step 6: Container Apps Environment
1. "Container Apps Environments" → Create
2. Name: `ronexa-env`, Region: Central US

### Step 7: Push Docker Images (from terminal)
```bash
docker login ronexaacr.azurecr.io -u ronexaacr -p {ACR_PASSWORD}

docker build -t ronexaacr.azurecr.io/backend-api:v1 -f backend/Dockerfile backend/
docker build -t ronexaacr.azurecr.io/worker:v1 -f backend/Dockerfile.worker backend/
docker build -t ronexaacr.azurecr.io/auth-ops-frontend:v1 -f frontend/Dockerfile frontend/
docker build -t ronexaacr.azurecr.io/nginx-proxy:v1 -f infra/nginx/Dockerfile infra/nginx/

docker push ronexaacr.azurecr.io/backend-api:v1
docker push ronexaacr.azurecr.io/worker:v1
docker push ronexaacr.azurecr.io/auth-ops-frontend:v1
docker push ronexaacr.azurecr.io/nginx-proxy:v1
```

### Step 8: Create Container Apps (in portal)

**App 1: backend-api** — 1 CPU / 2 GiB, Internal ingress :8000
**App 2: restate** — `restatedev/restate:1.1`, 1 CPU / 2 GiB, Internal :8080
**App 3: worker-1** — 2 CPU / 4 GiB, Internal :9080 (start with 1 worker for testing)
**App 4: auth-ops-frontend** — 0.5 CPU / 1 GiB, Internal :3000
**App 5: nginx-proxy** — 0.25 CPU / 0.5 GiB, **External** :80

### Step 9: Verify
1. Get nginx-proxy FQDN from Overview page
2. `https://{FQDN}/api/health` → `{"status": "ok"}`
3. `https://{FQDN}/auth-ops/settings` → Settings page loads

### Cost Estimate (testing — 1 worker)

| Resource | SKU | Monthly |
|----------|-----|---------|
| PostgreSQL | B1ms | ~$13 |
| Redis | Basic C0 | ~$16 |
| ACR | Basic | ~$5 |
| Document Intelligence | S0 | ~$1.50/1K pages |
| backend-api | 1 CPU / 2GB | ~$36 |
| restate | 1 CPU / 2GB | ~$36 |
| worker-1 | 2 CPU / 4GB | ~$73 |
| auth-ops-frontend | 0.5 CPU / 1GB | ~$18 |
| nginx-proxy | 0.25 CPU / 0.5GB | ~$9 |
| **Total** | | **~$210/mo** |

---

## Session 5 — Azure Deployment In Progress

### What's Been Created in Azure Portal

| Resource | Status | Notes |
|----------|--------|-------|
| Resource Group (`rg-ronexa-prod`) | ✅ Created | Central US |
| Container Registry (`ronexaacr`) | ✅ Created | Basic SKU, admin enabled |
| Azure AI Document Intelligence (`ronexa-doc-intel`) | ✅ Created | S0, Central US, endpoint + key copied |
| PostgreSQL Flexible Server (`ronexa-pg`) | ✅ Created | B1ms, v16, `ronexa` DB + pgvector enabled |
| Azure Managed Redis (`ronexa-redis`) | ✅ Created | Access keys available (used Managed Redis, not deprecated Cache) |
| Container Apps Environment (`ronexa-env`) | ✅ Created | Central US, Workload profiles |
| ACR Images pushed | ✅ All 4 | `backend-api:v1`, `worker:v1`, `auth-ops-frontend:v1`, `nginx-proxy:v1` |
| `backend-api` Container App | ⚠️ Shell created | No image/env vars configured yet — needs Edit and deploy or delete + recreate |
| `restate` Container App | ❌ Not created | |
| `worker-1` Container App | ❌ Not created | |
| `auth-ops-frontend` Container App | ❌ Not created | |
| `nginx-proxy` Container App | ❌ Not created | |

### Docker Build Notes

- **Must use `--platform linux/amd64`** for all builds (Mac M-series builds ARM by default, Azure needs AMD64)
- Docker Desktop is the build tool (not CLI-only Docker)
- All 4 images built and pushed successfully to ACR after platform fix
- Frontend needed `public/` directory created (was missing, caused COPY error)

### CLI Session Variables

Environment variables are lost between terminal sessions. Must re-export before running `az containerapp create`:

```bash
export RG="rg-ronexa-prod"
export ACR="ronexaacr"
export ENV_NAME="ronexa-env"
export ACR_SERVER=$(az acr show --name $ACR --query loginServer -o tsv)
export ACR_PASS=$(az acr credential show --name $ACR --query passwords[0].value -o tsv)

# Fill in YOUR values:
export DATABASE_URL="postgresql+asyncpg://ronexa:{PG_PASS}@ronexa-pg.postgres.database.azure.com:5432/ronexa?ssl=require"
export REDIS_URL="rediss://:{REDIS_KEY}@ronexa-redis.redis.cache.windows.net:6380/0"
export DOC_INTEL_ENDPOINT="https://ronexa-doc-intel.cognitiveservices.azure.com/"
export DOC_INTEL_KEY="{from portal}"
export MONGO_URI="{your cosmos connection string}"
export AZURE_BLOB_CONN="{your blob connection string}"
export GOOGLE_API_KEY="{your gemini key}"
export CARELON_USERNAME="{your carelon login}"
export CARELON_PASSWORD="{your carelon password}"
export GRAPH_TENANT_ID="{your tenant id}"
export GRAPH_CLIENT_ID="{your client id}"
export GRAPH_CLIENT_SECRET="{your client secret}"
export GRAPH_MAILBOX="{your mfa mailbox}"
```

### Next Steps When Resuming

1. **Fix `backend-api`** — Either:
   - Go to Containers → Edit and deploy → set image + env vars + save
   - Or delete the app and recreate via `az containerapp create` CLI (paste the full command from the deployment guide above)

2. **Create remaining 4 container apps** — restate, worker-1, auth-ops-frontend, nginx-proxy (CLI commands in the deployment guide above)

3. **Register Restate worker** — `curl` from inside the backend-api container to register worker-1 with Restate

4. **Verify** — Hit the nginx-proxy public URL: `/api/health`, `/auth-ops/settings`

5. **Run migration** — The backend Dockerfile runs `alembic upgrade head` on startup, but verify the DB tables were created

6. **Test Sync Now** — From the settings page, click Sync Now to pull cases from Cosmos DB

### Frontend Changes This Session

- Settings page: Prompt editor hidden by default (opens on click with Close button)
- API Keys moved to bottom as vertical list with status dots and descriptions
- AI Pipeline: Removed clinical interpretation node (2-stage → OCR text goes directly to pathway LLM)
- Default LLM: Gemini Pro for pathway, not Anthropic
