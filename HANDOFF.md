# Ronexa — Session Handoff (March 16, 2026)

## What This Project Is

Ronexa is a prior authorization submission platform. It automates the Carelon Provider Portal — an ASP.NET WebForms site behind Okta IDX + MFA — to submit diagnostic imaging auth requests. The stack is FastAPI + Playwright + Restate (durable workflows) + PostgreSQL + Redis.

The system processes cases from a DB (imported from RIS Excel), drives a headless browser through the portal's full SOP, uses an LLM to answer clinical questions, and suspends via Restate awakeables for human rep review before submitting answers.

---

## What Changed This Session (March 16)

### 1. LLM Evaluator — Gemini Fallback Added ✅

**File: `backend/app/intelligence/evaluator.py`**

- Refactored from Anthropic-only to **dual-provider**: Anthropic (primary) → Google Gemini (fallback)
- New `_call_anthropic()`, `_call_gemini()`, `_call_llm()` functions with automatic failover
- Uses `google-genai` SDK v1.67.0 (new SDK, not deprecated `google-generativeai`)
- Gemini uses `response_mime_type="application/json"` for structured output
- Empty/null LLM answers now fall back to first option with a gap note (portal always needs a submittable value)
- Smoke-tested: Anthropic credits confirmed working, returns valid TypedDecision

### 2. Clinical Notes Upload Endpoint — Built ✅

**File: `backend/app/api/routes/cases.py`**

- `POST /api/cases/{case_id}/notes` — real multipart PDF upload (was a placeholder)
- Flow: PDF bytes → `extract_page_images()` → `extract_clinical_context()` (Haiku vision) → save `ClinicalNote` to DB → audit event
- Updates case state from `PENDING_NOTES` → `NOTES_UPLOADED`
- Returns structured extraction inline so frontend can display it
- Gracefully handles extraction failures (still saves note, can retry)

### 3. Frontend Upload UI — Built ✅

**File: `frontend/app/cases/[caseId]/page.tsx`**

- Clinical Notes section with drag-and-drop PDF upload zone
- Shows existing uploaded notes (filename, page count, document type, timestamp)
- Upload spinner with "Uploading & extracting clinical data..." state
- Extraction result preview (chief complaint, diagnoses, findings, prior treatments, body part, etc.)
- Error handling for failed uploads/extractions

**File: `frontend/lib/api.ts`**

- Added `uploadNotes(caseId, file)` — multipart FormData upload function

### 4. Verified Live

- Amanda Durham case detail page renders with upload zone at `http://localhost:3000/cases/{id}`
- Anthropic API confirmed working (smoke test returned valid TypedDecision with reasoning)
- `google-genai` SDK installed and Gemini path coded (not live-tested — needs `GOOGLE_API_KEY` in env)

---

## What Is Done (Steps 1-14 — All Proven Live)

Every step below has been run against the real Carelon portal and confirmed working.

### Portal Execution Layer (the "hands")

| Step | What | Key Detail |
|------|------|------------|
| 1 | **Okta Login + MFA** | IDX flow, PKCE OAuth2, Graph API email OTP polling with prepare/wait pattern |
| 2 | **Agree to Terms** | HIPAA disclaimer click |
| 3 | **Member Search** | Auto-selects on single match (skips grid). DOB converted from YYYY-MM-DD to MM/DD/YYYY |
| 4 | **Eligibility Extraction** | Effective date, plan info, member address from page text |
| 5 | **Select Diagnostic Imaging** | Set hidden field `hdnSelectedType` (can't click the card div) |
| 6 | **Extract Patient Phone** | Read `#txbPhone` after DI selection |
| 7 | **Start Order Request** | `#cmdContinue` is hidden — must JS-submit (make visible + click) |
| 8 | **Existing Auths Check** | Extract grid, check for CPT duplicates, click Next |
| 9 | **Provider Search + Fax** | NPI search, extract all results with addresses, match by address from DB, fax modal |
| 10 | **Clinical Init** | `GetCase` returns 842 CPTs with CptGroupIds via ClinicalFacade API |
| 11 | **Exam Setup** | CPT lookup → body side/part → contrast from API (not hardcoded) → validate → add exam |
| 12 | **ICD Diagnosis** | Search by ICD-10 code → select → set diagnosis |
| 13 | **Pathway Selection** | Get pathway options → match by exact ICD code > matching flag > first |
| 14 | **Clinical Questions** | Parse from `Data.Assets` (not `Data.Questions`), capture pre-filled answers, extract ForDisplay=True questions |

### Key Technical Discoveries (Don't Re-Learn These)

1. **GetCase response nesting**: `{"d": {"Data": {..., "AvailableCptCodes": [...]}}}` — CPTs are inside `Data`, not top-level `d`
2. **ContrastCaptureId**: Comes from `GetCptCode` response, not hardcoded. Map: 0=Without, 1=With, 2=With, 3=Both
3. **Provider grid columns**: Name | Address | City | Specialty — only 4 columns, NO state/zip
4. **Clinical questions are in Assets**: `Data.Assets[].Type=1` with `ForDisplay=True` = needs answer. `Type=2` = recommendation. Auto-answered questions have `ForDisplay=False`
5. **QuestionType**: 2=numeric/date, 3=single choice, 4=multi choice
6. **Pre-filled Answers**: Portal auto-answers Client ID (value "300") and DOB. These come in `Data.Answers[]` and must be accumulated
7. **Option text**: Nested as `Options[].Text.Base`, not `Options[].Text`
8. **Exam State Machine**: NEW=6 → IN_ICD=3 → IN_DIAGNOSIS=2 → IN_QUESTIONNAIRE=8 → DONE=1
9. **ASP.NET postbacks**: Destroy JS execution context. PageReader retries once on navigation error. Use `load` wait state, not `networkidle`
10. **DI card selection**: Cards are `<h3>` elements, no checkbox/radio. Must set hidden field directly via JS

---

## Architecture — How the Layers Connect

```
User/API
  POST /api/cases/{id}/process
         │
         ▼
  Restate Ingress (port 8080)
  POST /PriorAuth/process_case
         │
         ▼
  prior_auth_workflow.py
  ┌─ ctx.run("load_case")          ← durable checkpoint
  ├─ ctx.run("load_clinical_context")
  ├─ ctx.run("set_processing")
  ├─ SessionPool.get_session()     ← Playwright + Okta login
  ├─ compiler.execute()            ← drives all phases
  │   ├─ WEBFORM phases           ← Steps 1-9 (webforms_client.py)
  │   ├─ API_SEQUENCE phases      ← Steps 10-13 (clinical_flow.py)
  │   └─ RECURSIVE_STATE_MACHINE  ← Step 14+ (question loop)
  │       ├─ LLM decides answer   ← intelligence/evaluator.py ✅ BUILT
  │       ├─ ctx.awakeable()      ← durable suspension for rep review
  │       ├─ await promise        ← ZERO COST wait
  │       └─ rep resolves → resume
  ├─ ctx.run("save_result")
  └─ ctx.run("index_outcomes")    ← RAG for future cases
```

### Document Upload Flow (NEW)

```
Frontend (case detail page)
  Drop PDF → POST /api/cases/{id}/notes (multipart)
         │
         ▼
  cases.py upload_notes()
  ├─ pdf_parser.extract_page_images()    ← PDF → PNG pages
  ├─ extractor.extract_clinical_context() ← Haiku vision → structured JSON
  ├─ ClinicalNote saved to DB            ← with structured extraction
  ├─ Case state → NOTES_UPLOADED
  └─ Returns structured data to frontend for preview
```

### The Gap: Compiler ↔ Our Portal Code

The `PortalCompiler` is designed to be portal-agnostic — it reads `portals/carelon_provider_portal.json` (PortalDNA) and dispatches phases. But the real proven-working code lives in:
- `webforms_client.py` — Steps 1-9 (WebForms DOM interactions)
- `clinical_flow.py` — Steps 10-14 (ClinicalFacade API calls)

These are called from `test_portal_live.py` directly. The compiler's `_run_webform()` and `_run_question_loop()` need to be wired to use them, OR the workflow should call our code directly instead of going through the compiler abstraction. This is the main integration decision.

---

## What Is NOT Done

### Immediate Next: Wire LLM → Clinical Flow → Live Test

The evaluator (`decide_answer`) is built and tested. The question loop in `clinical_flow.py` accepts an `answer_fn` callback. They need to be wired together and tested live against Amanda Durham.

**Steps:**
1. Wire `decide_answer` as the `answer_fn` callback in `clinical_flow.py`'s `run_clinical_questions_loop()`
2. Load the `ClinicalNote.structured` data from DB as the `clinical_context` param
3. Run live test through the full flow — login → member search → ... → questions answered by LLM
4. Observe: do questions get answered? What confidence? Any portal errors?

### After Questions: Finalize + Submit

`clinical_flow.py` already has `finalize()` stubbed:
1. `GetAlgorithmAttemptLimitCount`
2. `ProcessAccepted` — processes the clinical decision
3. `IsExamAutoApproved` / `IsExamApprovedClinicalDecisionOverride`
4. `AddFeedback` — provider contact info
5. `CheckIfAdditionalDocRequired`
6. `DoneWithExam`
7. `FindNextExam`

This needs to be tested live (Step 15 in the SOP).

### Facility Search (Step 16)

After clinical finalize, the portal shows a facility/rendering provider search. Not yet explored. Likely similar to provider search but for the imaging center.

### Final Submission + Auth Capture (Step 17)

Submit the completed case, capture auth number or denial/pend reason.

### Restate Wiring

The workflow handler (`prior_auth_workflow.py`) exists and works but currently routes through `PortalCompiler`. Options:
- **Option A**: Wire our proven code (`webforms_client.py` + `clinical_flow.py`) directly into the workflow handler, bypassing the compiler
- **Option B**: Update the PortalDNA JSON + compiler to dispatch to our code

The awakeable pattern for rep review is already coded in `_run_question_loop()` in the compiler. The same pattern needs to work with our `clinical_flow.py`.

### Rep Review UI (Frontend)

The React dashboard needs a queue view where reps see AI-suggested answers, approve/edit them, which resolves the Restate awakeable.

---

## Key Files

| File | Purpose |
|------|---------|
| `backend/app/auth/okta_login.py` | Okta IDX + MFA login |
| `backend/app/auth/mfa_resolver.py` | Graph API OTP polling |
| `backend/app/portal/webforms_client.py` | Steps 1-9: DOM interactions |
| `backend/app/portal/clinical_flow.py` | Steps 10-14: ClinicalFacade API orchestrator |
| `backend/app/portal/clinical_client.py` | Low-level ClinicalFacade API methods |
| `backend/app/portal/session.py` | Playwright session with in-page `fetch()` for API calls |
| `backend/app/portal/page_reader.py` | DOM message reader (errors, info, grids) |
| `backend/app/portal/behavior_engine.py` | Human-like timing (experienced rep speed) |
| `backend/app/portal/session_pool.py` | Per-NPI browser session pool |
| `backend/app/workflow/prior_auth_workflow.py` | Restate durable workflow |
| `backend/app/compiler/portal_compiler.py` | Phase dispatcher (WEBFORM, API_SEQUENCE, RECURSIVE_STATE_MACHINE) |
| `backend/portals/carelon_provider_portal.json` | Portal DNA (selectors, phases, auth config) |
| `backend/tests/test_portal_live.py` | Live test — runs Steps 1-14 against real portal |
| `backend/restate_worker.py` | Restate handler server (port 9080) |
| `backend/app/db/models.py` | Case, Question, ClinicalNote, AuditEvent models |
| `backend/app/intelligence/evaluator.py` | LLM question evaluator (Anthropic + Gemini fallback) |
| `backend/app/intelligence/extractor.py` | Haiku vision PDF → structured clinical data |
| `backend/app/intelligence/models.py` | PortalObservation, TypedDecision dataclasses |
| `backend/app/intelligence/prompts.py` | Evaluation system prompt + prompt builder |
| `backend/app/ingest/pdf_parser.py` | PDF → PNG page images (handles fax DPI normalization) |
| `backend/app/api/routes/cases.py` | Case CRUD + clinical note upload endpoint |
| `frontend/app/cases/[caseId]/page.tsx` | Case detail page with PDF upload UI |
| `frontend/lib/api.ts` | API client functions |
| `infrastructure/docker-compose.yml` | Postgres + Redis + Restate containers |

## Reference Implementation

Working code from the original carelon-sub project (for comparison, not to copy blindly):
- `/Users/andrewntuyo/Desktop/carelon-sub/apps/worker/portal/submission_flow.py`
- `/Users/andrewntuyo/Desktop/carelon-sub/apps/worker/portal/playwright_webforms_client.py`

## Test Case

Amanda Durham — the test case in the DB:
- Member ID: AN4713638, DOB: 07/16/1991
- CPT: 74178 (CT ABD&PLV), ICD: R10.9 (Unspecified abdominal pain)
- Pathway: "Unexplained abdominal pain" (exact ICD match)
- Clinical question: "Select which best describes the abdominal pain." → Acute / Chronic or recurrent / Unknown

## Installed Dependencies

- `google-genai==1.67.0` — new Google Gemini SDK (async support via `client.aio.models.generate_content`)
- Requires `GOOGLE_API_KEY` in env for Gemini fallback

## Memory Files

The `memory/` directory under `.claude/projects/` has detailed notes:
- `MEMORY.md` — Overview + preferences
- `webforms-flow.md` — Detailed portal flow with selectors and API structures
- `login-flow.md` — Okta login details

---

## Resume Checklist

When picking back up:
1. Read this file + `memory/MEMORY.md` + `memory/webforms-flow.md`
2. **Wire `decide_answer` → `answer_fn`** in `clinical_flow.py` and test live with Amanda Durham
3. Upload a real clinical note PDF for Amanda via the UI → verify extraction
4. Run full flow: login → member search → ... → LLM answers questions
5. Test `finalize()` live (Step 15)
6. Explore facility search + final submit (Steps 16-17)
7. Wire into Restate workflow with awakeable gates
8. Build the rep review queue UI
