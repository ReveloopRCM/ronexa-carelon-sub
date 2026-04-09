# Handoff — March 28, 2026

## Session Summary

Major session covering: batch simplification, Group 1 clinical scenario fix, post-review submission redesign, full automation toggle, HOLD cure system, auto-sync pipeline, and multi-worker architecture planning.

---

## 1. Batch Architecture Simplification

### What Changed
- **Removed `FinalizeService`** — was a separate polling service that watched for APPROVED_FOR_SUBMIT cases. Redundant with Restate's awakeable system.
- **Removed `APPROVED_FOR_SUBMIT` state** — L2 Submit now resolves the awakeable directly, triggering portal submission immediately.
- **Added batch counter to WorkerSession** — `set_batch_size` handler stores count in VO state. Each `run_first_pass` decrements via `_decrement_batch_counter`. When it hits 0, browser closes automatically.
- **Updated `restate_worker.py`** — removed FinalizeService registration. Now 6 services: WorkerSession, CaseWorkflow, BatchDispatcher, ExtractionService, PriorAuth (legacy), BrowserSession (legacy).

### Key Restate Lesson
**Journal mismatch on code changes.** When we changed `case_workflow.py` (reordered `ctx.run()` / `ctx.object_call()` steps), all in-flight CaseWorkflows from the old code failed with `[570 Journal mismatch]`. **Rule: after changing any Restate handler's ctx.* call sequence, kill ALL in-flight invocations for that service before deploying.**

---

## 2. Group 1 Clinical Scenario Fix

### The Problem
The first clinical question (clinical scenario selection) was invisible in L1 Review — all cases started at Group 2. The portal returned Group 1 as `ForDisplay=False` (hidden/pre-filled).

### HAR Analysis (5 cases)
Two patterns identified:

**Pattern A — "Other diagnosis" pathway:**
- Group 1 = DOB auto-fill (QType 2, same QuestionId `b39639ad` always) — NOT a clinical question
- Group 2 = Clinical scenario selection (Radio) — THE real question

**Pattern B — Specific pathway (Rotator Cuff, Meniscal Tear, etc.):**
- Group 1 = Clinical sub-scenario selection (Radio/Checkbox) — THE real question
- Group 2+ = Clinical questions

### The Fix (clinical_flow.py)
In `_run_question_loop`, after getting hidden questions from the portal:
- Check if any hidden question has `Type=1` (question) with `Options` (radio/checkbox choices)
- If so, **promote it to display** — send to LLM for answering
- DOB auto-fills (QType 2, no options) stay hidden
- Added `_normalize_prefills()` to ensure hidden answers have proper GroupId format

### Result
Group 1 now appears in L1 Review with AI_SUGGESTED answers. Verified in DB:
```
Group 1: "Are any of the following procedures being considered or planned?" — QType 3 (Radio) — AI_SUGGESTED ✅
Group 2: "Did the most recent pelvic ultrasound..." — QType 3 (Radio) — AI_SUGGESTED ✅
```

---

## 3. Post-Review Submission Redesign

### L2 Review — Two Actions

**Submit to Portal (green button):**
- Resolves awakeable directly with `action: "approved"` + all answers
- CaseWorkflow resumes → `run_finalize` → full portal replay → extract confirmation
- No intermediate APPROVED_FOR_SUBMIT state

**Re-Run (amber button):**
- Available only when reviewer changes an answer at L2
- Resolves awakeable with `action: "edited"` + `changed_group_id` + new answer
- CaseWorkflow resumes → `run_replay` → portal backtracks from changed group → new questions
- Case goes back to `L1_REVIEW` (treated like fresh run, full review cycle again)
- Question tree logic: changing answer at Group N invalidates everything from N+1 onwards

### L1 Review
- Approve only — pushes to L2
- No submit, no re-run

---

## 4. Full Automation Toggle (Settings)

### Design
CPT+ICD10 combo table in Settings → "Full Automation Rules":
- Toggle ON = LLM answers questions → skip L1+L2 review → straight to portal submission
- Toggle OFF = normal review flow
- Stats computed from historical submissions (shown on Analytics page)

### Implementation
- `automation_rules` table: `cpt_code` + `icd_code` (composite PK), `enabled`, `enabled_by`, `enabled_at`
- **Decision at dispatch time, not mid-workflow**: BatchDispatcher queries automation_rules, tags each case with `auto_submit: true/false` in the CaseWorkflow payload
- CaseWorkflow checks the flag — if true, skip awakeable/suspend, go straight to `run_finalize`
- Pros: No extra DB call from inside Restate, deterministic replay, decision auditable in payload
- Con: If toggle changes after dispatch, in-flight cases use stale decision (window is minutes, acceptable)

---

## 5. HOLD Cure & Auto-Requeue System

### Cure Feature
When a case is on HOLD for data issues, rep can:
1. See hold reason + current field values
2. Edit the bad/missing field directly on the case detail page
3. Click "Cure & Requeue"
4. Backend: updates case fields → resets state to `NOTES_UPLOADED` → clears hold_reason → resets job to `QUEUED`

### Curable Fields
All portal-input fields: `first_name`, `last_name`, `dob`, `policy_num`, `patient_zip`, `center_npi`, `cpt_code`, `icd1`-`icd5`, `referring_npi`, `referring_fax`, `patient_phone`

### Phone Number Logic
1. Portal phone (primary) — extracted from portal page during navigation
2. Case phone (fallback) — `PatientPhone` from Mongo payload (newly mapped)
3. Neither available → HOLD for cure

### Auto-Requeue for Transient Errors
Browser crashes, portal timeouts, network failures → auto-requeue with max 2 retries:
- Original attempt (1st) → transient error → auto-requeue
- Retry 1 (2nd) → transient error → auto-requeue
- Retry 2 (3rd) → transient error → **permanent HOLD**

Classification in `_is_transient_error()`: checks for "browser", "timeout", "Target page", "closed", "navigation", "network" in error message.

Permanent HOLDs (need human cure): `MEMBER_NOT_FOUND`, `PHONE_MISSING`, `DUPLICATE_AUTH`, `ELIGIBILITY_EXPIRED`, missing data fields.

---

## 6. Auto-Sync Pipeline

### Current Setup
- **Poll scheduler** — asyncio task in FastAPI lifespan, configurable from Settings UI
- **Sync cycle**: Cosmos DB fetch → dedup → insert → enqueue → mark synced → fire extraction → auto-dispatch
- **Settings**: `polling_enabled`, `polling_interval_minutes` (30), `polling_extract` (true), `auto_process_enabled`

### Fix Applied
`sync_engine.py` auto-process was calling dead `ShiftManager/start_worker_loop` → changed to `BatchDispatcher/dispatch_batch` with worker config.

### Natural Batch Cycle
- Cycle N: syncs cases, fires extraction
- Cycle N+1 (30 min later): extraction done, cases now NOTES_UPLOADED, batch picks them up
- One-cycle lag for new cases — acceptable, operator can manually dispatch if urgent

---

## 7. Multi-Worker Architecture (IN PROGRESS — discussed, not yet implemented)

### Design Decision: Restate + Smart Dispatch (NOT a message broker)

**Why not Redis/RabbitMQ/Celery:**
- Browser session reuse is critical — login once per batch, not per case
- Restate already provides: exclusive handlers (per-worker queue), exactly-once delivery, retry/replay
- Adding a broker would require rebuilding session management

**Round-Robin Dispatch:**
```python
workers = ["worker-a", "worker-b", "worker-c"]
for i, case in enumerate(sorted_cases):  # sorted by priority
    worker = workers[i % len(workers)]
    # STAT cases interleaved across all workers
```

**Scaling math:**
- 3 workers × 50 cases/batch × ~1 batch/hour = ~150 cases/hour
- 8-hour shift = ~1200 cases/day ✅ (covers 1000/day target)

**Infrastructure:**
- Separate VMs per worker (Akamai bot detection requires isolated browser profiles)
- Worker credentials stored in `worker_accounts` DB table (not env vars)
- Adding a worker = INSERT row + deploy VM

**Smart dispatch (not yet built):**
- Dispatcher queries active workers from `worker_accounts`
- Round-robin with priority interleaving (STAT spread across all workers)
- Small batches OK — even 1 case dispatches to a worker (30 sec login overhead is fine for STAT)
- Workers that get 0 cases don't login

### TODO: Implement multi-worker dispatch
- Wire `worker_accounts` into `BatchDispatcher`
- Store Carelon credentials per worker in DB
- Deploy worker-b and worker-c VMs (duplicate worker1 setup)
- Update Settings UI for worker management

---

## Deployment State

| Component | Version | Notes |
|-----------|---------|-------|
| Backend API | v24 | On orchestrator (20.29.73.195) |
| Frontend | v21 | On orchestrator |
| Restate Worker | rev 12 | On worker1 (172.202.22.112 / 10.0.0.5) |
| Restate Server | 1.6.2 | On orchestrator |
| Azure PostgreSQL | ronexa-pg | Password: `doYRYD6DulhnNkAFRW33r66VWgET` |

### DB Migrations
- 008: Added `determination_status`, `valid_from`, `valid_through`, `denial_reason`, `pend_reason` to cases
- 009: Added `automation_rules` table
- 010: Added `patient_phone` to cases

### Infrastructure
- Worker runs under `xvfb-run` (virtual display for headed Playwright)
- xrdp on worker1 sometimes hangs — restart with `sudo systemctl restart xrdp`
- Docker `restart` doesn't re-read env files — must use `docker compose up -d --force-recreate`

---

## Key Files Changed This Session

### Backend
- `app/workflow/case_workflow.py` — awakeable before save, try-except on WorkerSession calls, auto_submit path
- `app/workflow/worker_session.py` — batch counter, auto-requeue for transient errors
- `app/workflow/batch_dispatcher.py` — set_batch_size call, automation_rules lookup, patient_phone in payload
- `app/portal/clinical_flow.py` — Group 1 promotion from hidden to display, _normalize_prefills
- `app/compiler/portal_compiler.py` — phone fallback logic (portal → case → HOLD)
- `app/api/routes/queue.py` — direct submit + rerun endpoints (replaced resolve-l2)
- `app/api/routes/cases.py` — PATCH cure endpoint
- `app/api/routes/settings.py` — automation rules CRUD, analytics endpoint
- `app/ingest/mongo_poller.py` — PatientPhone mapping
- `app/ingest/sync_engine.py` — fixed auto-process to use BatchDispatcher
- `app/db/models.py` — AutomationRule, patient_phone, determination fields, removed APPROVED_FOR_SUBMIT
- `restate_worker.py` — removed FinalizeService

### Frontend
- `app/queue/[caseId]/page.tsx` — Re-Run button at L2, editable cure fields on HOLD cases
- `app/settings/page.tsx` — Full Automation Rules section
- `app/analytics/page.tsx` — CPT+ICD submission stats (populates after submissions complete)
