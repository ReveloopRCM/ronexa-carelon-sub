# Handoff — March 25, 2026

## What Was Accomplished Today

### 1. VM Deployment (Container Apps → VMs)
- Deleted all Container Apps (5 apps + environment + storage)
- Created 2 VMs: Orchestrator (D2s v3) + Worker-A (D4s v3)
- Docker Compose on Orchestrator: Restate + Backend + Frontend + Nginx
- Worker runs natively (Python + Playwright, HEADED browser for RDP monitoring)
- DNS: `ronexa.centralus.cloudapp.azure.com` (orchestrator), `ronexa-worker-a.centralus.cloudapp.azure.com` (worker)
- Restate abort timeout increased to 10 minutes (was 1 min default causing re-login loops)

### 2. Gemini 2.5 Pro Integration Fixed
- `google.genai` SDK: `response.text = None` for thinking models
- `response_mime_type: "application/json"` incompatible with 2.5-pro thinking mode
- Safety filters blocking clinical content (finish_reason=2)
- **Fix:** Switched to `google.generativeai` SDK (deprecated but working), removed `response_mime_type`, added safety settings `BLOCK_NONE`, code fence stripping for markdown-wrapped JSON responses
- Currently using `gemini-2.5-flash` (works) — `gemini-2.5-pro` needs response_mime_type removed

### 3. ViewState Wait Fix
- `wait_for_selector("[name='__VIEWSTATE']")` defaulted to `state="visible"` but __VIEWSTATE is `type="hidden"`
- Changed to `state="attached"` — checks DOM presence, not visibility
- Fixed in both `page_reader.py` and `webforms_client.py`

### 4. Worker Environment Fix
- `.env` not loaded into worker process environment (nohup doesn't source .env)
- Created `start_worker.sh` wrapper script that sources .env before starting worker
- `HEADLESS` setting was hardcoded to `ENVIRONMENT != "local"` — fixed to read from env var

### 5. Flow Checks Display
- Flow checks (eligibility, duplicate auth, provider match, contrast, completeness) were saved AFTER awakeable suspension — reviewers never saw them
- Moved `_save_inline_checks()` to run BEFORE the awakeable suspension
- Now visible on case detail page during L1/L2 review

### 6. L1/L2 Review Fixes
- L2 was showing "No pending questions" — filter only showed AI_SUGGESTED, not REP_APPROVED
- Fixed: both L1 and L2 see all questions
- "L1 Changed" badge only shows on questions L1 actually edited (has rep_answer)
- Added edit reason textarea (appears when rep changes an answer)

### 7. LLM Key Resolution Fix
- Fernet encryption on DB keys uses different key per machine
- Decrypted garbage passed validation check (didn't start with `gAAAAA`)
- Fixed: validate key format (must start with `sk-ant-` or `AIza` and be >20 chars)
- Fallback to `os.environ` directly (not just settings object)

## Current Infrastructure

| Resource | Location | Status |
|----------|----------|--------|
| Orchestrator VM | ronexa.centralus.cloudapp.azure.com (20.29.73.195) | ✅ Running |
| Worker-A VM | ronexa-worker-a.centralus.cloudapp.azure.com (172.202.22.112) | ✅ Running |
| PostgreSQL | ronexa-pg.postgres.database.azure.com | ✅ |
| Redis | ronexa-redis | ✅ |
| ACR | ronexaacr.azurecr.io | ✅ |
| Document Intelligence | ronexa-doc-intel (centralus) | ✅ |
| Restate | Docker on Orchestrator, ports 8080/9070/9071 | ✅ |
| Backend API | Docker on Orchestrator, port 8000 | ✅ v11 |
| Frontend | Docker on Orchestrator, port 3000 | ✅ v10 |

## Approved Plan — Next Implementation

Full plan in `/Users/andrewntuyo/.claude/plans/drifting-sauteeing-thompson.md`

### Three Workflows to Build:

**A) ExtractionService** — Restate fan-out OCR (parallel, 10 concurrent)
- Sync inserts cases → fires ExtractionService → Document Intelligence OCR → NOTES_UPLOADED

**B) Portal Batch Processing** — Serial per login with session reuse
- Worker loop claims jobs → login once → process N cases → navigate dashboard between → L1_REVIEW
- Session reuse: browser stays open between cases (one login per batch)

**C) FinalizeService** — Batch submit approved cases
- L2 approve → APPROVED_FOR_SUBMIT (awakeable NOT resolved)
- FinalizeService: login once → loop through approved → ctx.resolve_awakeable() for each
- PriorAuth resumes → replays portal → fast-forward with approved answers → submit
- If rep changed answer → backtrack → new questions → back to L1_REVIEW → next batch picks up

### Settings Page Additions:
```
Portal Processing
├── Auto-Process: ON/OFF
├── Batch Size (first pass): 50
├── Finalize Batch Size: 10
├── [Start Worker] [Submit Batch]
└── Queue Stats
```

## Files Modified Today

| File | Change |
|------|--------|
| `backend/app/portal/page_reader.py` | ViewState wait: `state="attached"` |
| `backend/app/portal/webforms_client.py` | ViewState wait + fax unavailable fix |
| `backend/app/workflow/browser_session.py` | HEADLESS from env var |
| `backend/app/workflow/prior_auth_workflow.py` | `_save_inline_checks()` before suspension |
| `backend/app/intelligence/evaluator.py` | Gemini SDK fix + safety settings + code fence stripping |
| `backend/app/intelligence/llm_config.py` | Key validation + os.environ fallback |
| `backend/app/core/settings.py` | Added HEADLESS setting |
| `backend/app/api/routes/cases.py` | extraction_method label + process endpoint (removed env check) |
| `backend/app/db/repositories.py` | list_review_queue with states filter |
| `frontend/app/queue/[caseId]/page.tsx` | L2 sees all questions + edit reason + L1 Changed badge |
| `frontend/app/queue/page.tsx` | L1/L2 tabs |
| `frontend/app/cases/[caseId]/page.tsx` | PDF link fix + OCR text display |
| `backend/app/workflow/extraction_service.py` | **Created** — ExtractionService fan-out OCR |
| `backend/app/workflow/finalize_service.py` | **Created** — FinalizeService batch awakeable resolution |
| `backend/restate_worker.py` | Registered ExtractionService + FinalizeService |
| `backend/app/ingest/sync_engine.py` | Removed inline extraction, fire-and-forget to ExtractionService |
| `backend/app/api/routes/settings.py` | Added extract-now, submit-batch, start-worker endpoints + APPROVED_FOR_SUBMIT to protected states |
| `backend/app/api/routes/queue.py` | resolve-l2 now sets APPROVED_FOR_SUBMIT (no awakeable resolve) |
| `backend/app/db/models.py` | Added APPROVED_FOR_SUBMIT to CaseState |
| `backend/alembic/versions/007_batch_settings.py` | Migration: batch settings + new state |
| `frontend/app/settings/page.tsx` | Portal Processing section: Extract Now, Start Worker, Submit Batch |
| `frontend/lib/api.ts` | Added extractNow, submitBatch, startWorker functions |

## Implementation Progress — ALL DONE

### Deployed:
- [x] ExtractionService — fan-out OCR (parallel, 10 concurrent)
- [x] FinalizeService — batch resolve awakeables for approved cases
- [x] Sync engine — fire-and-forget to ExtractionService after insert
- [x] resolve-l2 — sets APPROVED_FOR_SUBMIT, does NOT resolve awakeable
- [x] APPROVED_FOR_SUBMIT state + protected from flush
- [x] Portal Processing settings section (Extract Now, Start Worker, Submit Batch)
- [x] Migration 007 run on Azure Postgres
- [x] Backend v12 deployed to orchestrator
- [x] Frontend v11 deployed to orchestrator
- [x] Worker updated + 6 Restate services registered

### NOT yet implemented (deferred):
- [ ] Session reuse in UserWorker (navigate_to_dashboard between cases)
- [ ] navigate_to_dashboard() helper in webforms_client.py
- [ ] Auto-trigger worker loop after sync (currently manual via Start Worker button)

## Key Architecture Decisions Made Today

### Restate Invocation Model
- Each `PriorAuth.process_case(case_id)` is an independent Restate invocation with its own journal
- No "batch journal" — the worker loop fires individual invocations serially
- Journal entries 0-3 replay instantly on resume; entry 4 (finalize_pass) is new execution

### FinalizeService Architecture
- L2 approve sets `APPROVED_FOR_SUBMIT` — does NOT resolve awakeable
- FinalizeService (Restate Service) logs in once, loops through approved cases
- Uses `ctx.resolve_awakeable(case.awakeable_id, payload)` for each case
- PriorAuth resumes → replays portal → fast-forward with approved answers → submit
- Same browser session reused across cases (session reuse via `_BROWSER_REGISTRY`)
- If rep changed an answer → backtrack → new questions → back to L1_REVIEW → next batch

### Session Reuse Strategy
- **First pass batch:** Browser stays open between cases, navigate to dashboard between
- **Awakeable suspension:** Browser closes (can't hold during hours of rep review)
- **Resume/finalize pass:** FinalizeService manages the browser, PriorAuth reuses it via `_ensure_session_direct()`

### Restate Abort Timeout
- Default was 1 minute — caused re-login loops (portal automation takes 2-5 min)
- Set to 10 minutes via `RESTATE_WORKER__INVOKER__ABORT_TIMEOUT=10m`

## Key Files for Next Session

| File | Purpose |
|------|---------|
| `/Users/andrewntuyo/.claude/plans/drifting-sauteeing-thompson.md` | Full approved plan |
| `/Users/andrewntuyo/Desktop/ronexa-sub/restate-fan.md` | Extraction fan-out design |
| `backend/app/workflow/extraction_service.py` | Created, needs registration |
| `backend/app/workflow/prior_auth_workflow.py` | Needs `close_after` param + browser close before suspend |
| `backend/app/workflow/user_worker.py` | Needs session reuse between cases |
| `backend/app/workflow/finalize_service.py` | To create — batch awakeable resolution |
| `backend/app/portal/webforms_client.py` | Needs `navigate_to_dashboard()` helper |
| `backend/app/api/routes/queue.py` | resolve-l2 needs APPROVED_FOR_SUBMIT change |
| `backend/app/ingest/sync_engine.py` | Needs inline extraction removed, fire-and-forget |
| `backend/restate_worker.py` | Register ExtractionService + FinalizeService |

## VM Access

| VM | SSH | RDP |
|----|-----|-----|
| Orchestrator | `ssh ronexa@20.29.73.195` | N/A |
| Worker-A | `ssh ronexa@172.202.22.112` | `ronexa-worker-a.centralus.cloudapp.azure.com:3389` (user: ronexa, pass: Ronexa2026!) |

## Restate Admin
- URL: `http://20.29.73.195:9070`
- Worker registered at `http://10.0.0.5:9080`
- Abort timeout: 10 minutes
- **6 services registered:** PriorAuth, BrowserSession, UserWorker, ShiftManager, ExtractionService, FinalizeService

## Current Deployed Versions
- Backend: **v12** (Docker on orchestrator)
- Frontend: **v11** (Docker on orchestrator)
- Worker: **native Python** (VM worker-a, started via `start_worker.sh`)
- Restate: **1.3** (Docker on orchestrator)

## Ready to Test — Full Pipeline

1. **Settings → Sync Now** — pulls from Mongo, inserts cases, fires ExtractionService
2. **Watch extraction** — worker logs show parallel OCR batches (10 concurrent)
3. **Cases transition** — PENDING_NOTES → NOTES_UPLOADED
4. **Settings → Start Worker** — fires ShiftManager worker loop
5. **Worker processes cases** — login → portal → LLM answers → L1_REVIEW
6. **L1 Review** — junior auth reviews questions, approves → L2_REVIEW
7. **L2 Review** — senior auth reviews, clicks Submit → APPROVED_FOR_SUBMIT
8. **Settings → Submit Batch** — FinalizeService resolves awakeables
9. **PriorAuth resumes** — replays portal → fast-forward with approved answers → SUBMIT
10. **Case state** → APPROVED / DENIED / PENDED

---

## Session 6 — Production Testing + Bug Fixes (March 25-26)

### Bugs Found & Fixed

| Bug | Root Cause | Fix |
|-----|-----------|-----|
| **LLM "all providers unavailable"** | `google-genai` SDK not installed on worker; then Fernet decryption mismatch; then `gemini-2.5-pro` response parsing | Installed SDK, bypassed DB encryption for env var fallback, fixed response text extraction |
| **Gemini 2.5 Pro empty response** | `response_mime_type="application/json"` conflicts with thinking mode | Removed `response_mime_type`, parse JSON from text response instead |
| **Multiple logins per batch** | `_run_worker_pass` closed browser on every error; `dry_run=True` accidentally set | Error recovery navigates to dashboard instead of closing; fixed `dry_run=False` |
| **Cases disappearing from Cases page** | `ACTIVE_STATES` didn't include `L1_REVIEW`, `L2_REVIEW`, `APPROVED_FOR_SUBMIT` | Added all review states to active filter + "In Review" bucket |
| **HOLD cases not in Worklist** | `_mark_job_exception_by_case` not called from eligibility, duplicate auth, completeness early-return paths | Added `mark_exception` call to all 4 HOLD paths in `prior_auth_workflow.py` |
| **Eligibility not validated** | `check_eligibility()` existed but was never called in the compiler | Added eligibility gate after extraction — expired coverage → HOLD |
| **Term date not extracted** | Date range regex skipped if effective date already found | Always extract term_date from range pattern |
| **Scott Ward "Could not click Next"** | Portal skips auths page for some members; code still tried to click Next | Smart detection: check for provider search radio vs Next button; skip Next if `skipped=True` |
| **Phone number placeholder extracted** | Portal field `"Enter Phone"` treated as real number | Regex validation — must contain 3+ digits; HOLD if invalid |
| **Referring NPI missing** | `referring_npi=None` passed to `type_text()` | Guard check before provider search — HOLD with `REFERRING_NPI_MISSING` |
| **Duplicate auth false positive** | Old auths (>30 days) flagged as duplicates | Added date check — only flag if auth DOS within last 30 days |
| **Restate 60s abort timeout** | Clinical flow takes 2-5 min; Restate killed connection at 60s → retry loop | Set `RESTATE_WORKER__INVOKER__ABORT_TIMEOUT=10m` |
| **L2 shows empty questions** | L1 resolve changed `review_state` to `REP_APPROVED`; L2 query filtered them out | L2 shows all questions regardless of review_state |
| **Worklist sidebar counts wrong** | Stats query filtered by `SUSPENDED` only | Include `FAILED` status in exception count |
| **xrdp stale sessions** | Reconnect to old display failed with `scp_process_msg` error | Set `MaxSessions=1`, `KillDisconnected=true`, cleanup cron |

### New Exception Types Added

| Exception | Trigger | Worklist Category |
|-----------|---------|-------------------|
| `ELIGIBILITY_EXPIRED` | Coverage terminated or not yet effective | Eligibility Expired |
| `PHONE_MISSING` | Phone field has placeholder text or no digits | Phone Missing |
| `REFERRING_NPI_MISSING` | `referring_npi` is null in case data | Referring NPI Missing |

### Frontend Improvements

- **Requeue button** on HOLD + FAILED case detail pages (resets to NOTES_UPLOADED)
- **Referring Provider card** in flow checks — shows match method, RIS address, fax entered, portal results
- **Confirmation dialogs** on all settings action buttons (Extract Now, Start Portal Batch, Submit Batch)
- **"Start Portal Batch"** label (was "Start Worker")
- **Exam ID** shown on Queue + Worklist pages (was showing UUID)
- **All review states** visible in Cases page (L1_REVIEW, L2_REVIEW, APPROVED_FOR_SUBMIT)

### Current Deployed Versions
- Backend: **v18** (Docker on orchestrator)
- Frontend: **v18** (Docker on orchestrator)
- Worker: **native Python** (VM worker-a, systemd service)
- Restate: **1.6.2** (Docker on orchestrator, 10min abort timeout)

### Infrastructure
- **Orchestrator VM:** `ronexa.centralus.cloudapp.azure.com` (20.29.73.195)
- **Worker-A VM:** `ronexa-worker-a.centralus.cloudapp.azure.com` (172.202.22.112)
- **RDP:** user `ronexa`, pass `Ronexa2026!`
- **VNC viewer** (from RDP terminal): `vncviewer localhost:5900` — shows worker browser on Xvfb :99
- **Worker service:** `sudo systemctl restart ronexa-worker` — auto-starts on boot
- **Restate admin:** `http://20.29.73.195:9070`

### Session Reuse Status
- **Implemented:** `close_after=False` in `_run_worker_pass`, navigate-to-dashboard between cases, error recovery navigates back instead of closing
- **Still seeing multiple logins because:**
  - Cases that SUSPEND for review (line 164) correctly close browser (can't hold during human review)
  - Error cases were closing browser — **FIXED** this session (recover to dashboard instead)
  - `dry_run=True` was accidentally set — **FIXED** to `False`
- **Expected after fix:** 1 login at batch start, browser stays open for non-blocking exceptions (member not found, phone missing, etc.), only closes on SUSPEND (review needed) or fatal error

### Session Reuse — Root Cause Identified (March 26)

**Problem:** After clinical SPA completes questions, navigating to `Default.aspx` or clicking Home button fails because:
1. The clinical SPA (`/exam-entry`, `/pathway-questions`) replaces the ASP.NET DOM — Home button `#asPrimary_ctl00_btnGoToHomepage` doesn't exist in SPA
2. Direct navigation to `Default.aspx` from the SPA URL triggers a new session (ViewState mismatch)
3. Portal shows login page → code detects "session expired" → closes browser → re-logs in

**Reference code solution:** They never navigate to homepage. They stay on the search page and click "Return to Search Results" between cases. They never enter the clinical SPA.

**Our problem is harder:** We DO enter the clinical SPA (for pathway questions). After questions, we need to exit the SPA back to WebForms context. The portal uses `hdnAction` postbacks for SPA→WebForms transitions (e.g., `hdnAction=20` exits clinical SPA to exam summary).

**Proposed fix:** After saving questions (before navigating home):
1. Use `hdnAction` postback to exit the clinical SPA back to a WebForms page
2. From the WebForms page, click the Home button (now in DOM)
3. OR: close the current page tab and open a new tab in the same context (preserves cookies)

**Alternative:** Don't navigate home at all. After clinical questions:
1. Save answers to DB
2. Close the current PAGE (not browser context)
3. Open a NEW PAGE from the same browser context
4. Navigate to `Default.aspx` on the new page → fresh WebForms form, same authenticated session

This matches the reference parallel pattern: `context.new_page()` from shared context.

### Next Steps When Resuming

### Batch Processing — Root Cause Found (March 26, 4am)

**The multiple logins were NOT caused by navigation issues.** They were caused by `_save_for_review()` crashing on DB schema mismatches → Restate retrying the entire `process_batch` invocation → new login each retry.

**Three DB bugs fixed in `_save_for_review()`:**
1. `options` → `options_json` (wrong column name)
2. `portal_question_id` missing (NOT NULL constraint violation)
3. `sequence` missing (NOT NULL constraint violation)

**Navigation approach settled:**
- Navigate to `Default.aspx` on the same page between cases
- Check body text for "Logout" = session alive, "User Confirmation" = expired
- New-page-from-context does NOT work (portal session tied to specific page)
- Home button `#asPrimary_ctl00_btnGoToHomepage` not always in DOM (varies by page state)

**Architecture implemented:**
- `process_batch` handler — single Restate invocation for entire batch
- Login once → loop through queue → per-case try/except → navigate between cases
- `_process_single_case_batch()` — runs compiler, classifies result
- `_save_for_review()` — saves questions to DB without awakeable, sets L1_REVIEW
- `_navigate_to_homepage()` — `Default.aspx` on same page + body text verification
- `_check_for_system_error()` — portal error page detection (from reference code)
- ShiftManager calls `process_batch` instead of looping `process_next`

**Current state:** All DB fixes deployed. Ready to test — click Start Portal Batch.

### Restate Architecture Redesign — APPROVED (March 26)

**Full design doc:** `/docs/restate-architecture.md`
**Implementation plan:** `.claude/plans/drifting-sauteeing-thompson.md`

**Problem:** Current batch architecture has no per-case journals, no awakeables, re-logins on every Restate retry.

**New architecture — 3 services replacing 4:**

| New | Replaces | Type |
|-----|----------|------|
| WorkerSession | UserWorker + BrowserSession | Virtual Object (keyed by worker_id) |
| CaseWorkflow | PriorAuth | Workflow (keyed by case_id) |
| BatchDispatcher | ShiftManager | Service (stateless) |

**Key design:** CaseWorkflow owns awakeable and suspends. WorkerSession handlers always return (never suspend). Exclusive handler queue on WorkerSession = serial portal access.

### Next Steps — Implementation Order

1. Create `worker_session.py` — 3 exclusive handlers + shared status
2. Create `case_workflow.py` — per-case journal + awakeable
3. Create `batch_dispatcher.py` — query queue + fan-out dispatch
4. Update `finalize_service.py` — resolve awakeables
5. Update `restate_worker.py` — register new services
6. Update API routes — point to new services
7. Deploy + test — 5 case batch, verify 1-2 logins

### Debug Status (March 26, 1:30pm)

**Architecture working:**
- BatchDispatcher dispatches 4 CaseWorkflows ✅
- All 4 CaseWorkflows execute (HANDLER ENTERED logged) ✅
- WorkerSession.run_first_pass processes cases serially ✅
- Cases reach L1_REVIEW with Gemini answers ✅

**Session expiry confirmed:**
- Screenshot captured: after case completes, navigate to Default.aspx → shows login page
- Body text: "User Confirmation" + "USERNAME" — genuine session expiry
- Happens every time after a case goes through full clinical flow (3-5 min)
- Portal's ASP.NET session timeout ~15-20 min but clinical SPA API calls don't refresh it

**BUT: Old extraction workflow processes 200+ cases on single login**
- The old code navigates differently (doesn't goto Default.aspx)
- Need to investigate: how does the old code navigate between cases?
- Nav test script created (`nav_test.py`) — captures HAR + screenshots + DOM state after case 1
- Test crashed because `#asPrimary_ctl00_BtnSearch` not visible even after fresh login
- Root cause: login verifies body text ("Start Your Order") but form elements load later

**Login blocked (end of session):**
- Portal rejected credentials: "Username/Password does not match an account"
- Likely rate limited from dozens of login attempts today
- Wait and retry fresh — credentials are correct (worked all day)

**HOME ICON NAVIGATION — SOLVED (March 26, 10:50am local test)**

The house icon in the portal's top nav bar (`#asNavigation_ctl00_hlHome`) triggers `__doPostBack('TopMenu','')` which navigates back to the member search page WITHOUT killing the session.

```html
<a id="asNavigation_ctl00_hlHome" title="Home" href="javascript:__doPostBack('TopMenu','')">
  <img title="Home" src="Support/Images/homepage/homeicon.png">
</a>
```

**Verified locally:** Login → Case 1 (full clinical flow) → Home icon click → Member search page (session alive) → Case 2 started on same session.

**Fix deployed in `worker_session.py`:** `_navigate_to_homepage()` now clicks `#asNavigation_ctl00_hlHome` instead of `page.goto('Default.aspx')`.

**Why `page.goto('Default.aspx')` failed:** It's a full page navigation that creates a new HTTP request outside the ASP.NET ViewState context. The server sees it as a new session. The `__doPostBack` is an in-page form submission that maintains the ViewState chain.

**Nav test script:** `backend/nav_test.py` — ready to run, captures:
1. All links on page after case 1 completes
2. Whether Home button exists in DOM (even hidden)
3. Whether ASP.NET form + ViewState exist under the SPA
4. JS click attempts on home button
5. Screenshots at every transition
6. HAR file for full HTTP trace

**Architecture PROVEN WORKING (March 26, 5pm):**
- 1 login, 25 cases dispatched, serial processing, home icon nav between cases ✅
- CaseWorkflow + WorkerSession + BatchDispatcher all working ✅
- Errors become HOLDs, batch continues without re-login ✅
- Home icon `#asNavigation_ctl00_hlHome` (`__doPostBack('TopMenu','')`) proven ✅

**REMAINING ISSUE: Bot detection / speed**
- Think times are 120-180ms (too fast for a human)
- Between-case navigation is instant
- Need to slow down to auth rep speed: 1-3s between actions
- Bot detection may be causing portal page load failures
- All test cases went HOLD — need to verify if speed is causing portal issues vs real data issues

**Next steps:**
1. Review and increase behavior engine timings (formField, buttonClick, searchResult)
2. Add delays between cases (2-3s after home nav before next search)
3. Re-test batch with realistic timing
4. Find clean test cases that will reach clinical questions
5. **Analytics page** — submission stats, approval rates, exception breakdown
6. **RAG feedback loop** — store approved answers for similar-case matching
