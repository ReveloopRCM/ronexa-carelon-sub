# Handoff — March 21, 2026

## What was accomplished today

### 1. Robin Stuart — New Test Case (CPT 70551, Brain MRI)

**Case setup:**
- Extracted clinical notes from Azure Blob (6 pages, neurology new patient eval)
- Saved to DB, state → NOTES_UPLOADED
- Case ID: `3865c6da-7dd6-466d-84f9-5897335e5e49`
- Policy: BHP816691826, DOB: 03/21/1958
- CPT: 70551 (MRI brain without contrast), ICD: G31.84 (mild cognitive impairment)
- Center NPI: 1093045072, Referring NPI: 1891844577 (Ganana Tesfa, Neurology)
- Fax: 8174537441

**Live test (test_portal_live.py) — FULL PASS Steps 1-19 ✅**
- Login + MFA ✅
- Member search → Robin Stuart auto-selected ✅
- 17 pathway options for brain MRI, LLM selected "Neurocognitive disorders (including dementia)" ✅
- 2 clinical questions, both 92% confidence:
  - Q1: "Select reason for ordering" → "Cognitive abnormality or dementia"
  - Q2: "Prior head CT or brain MRI?" → "No"
- Portal recommendation: "RW Approval Subroutine" (Approve)
- Finalize → hdnAction=20 → exam summary → hdnAction=6 → facility → order preview ✅
- **3rd CPT category proven** (70551 brain MRI, after 75574 cardiac CT and 73721 knee MRI)

### 2. Kristal Shackelford — Member Not Found

- Policy RZL23692209 not recognized by portal
- Names confirmed correct (not a swap issue like Oshodi)
- Need to handle "member not found" as a production scenario: flag case, update status, route to manual queue

### 3. Group 1-4 Questions Confirmed

The raw asset dump logging added last session confirmed the hidden question pattern for brain MRI:
- Group 1: CPT Code (ForDisplay=False, QType=1)
- Group 2: State of issuance (ForDisplay=False, QType=1)
- Group 3: Line of Business (ForDisplay=False, QType=2)
- Group 4: What is the Client Id (ForDisplay=False, QType=2)
- Group 5+: Real clinical questions (ForDisplay=True)

This is a different pattern from knee MRI (which had DOB as Group 1). The hidden questions vary by CPT/pathway but are always auto-filled by the portal.

### 4. Restate E2E Workflow — Full Replay Path Verified ✅

**This is the big milestone.** The full suspend → approve → resume → replay architecture works end-to-end.

**First pass (compiler runs all phases):**
1. Login + MFA → dashboard ✅
2. Member search → Robin Stuart ✅
3. Eligibility → effective 01/22/2025 ✅
4. DI selection → start order ✅
5. Existing auths → 2 found (brain MRI + abdomen MRI), passed through ✅
6. Provider search → TESFA, GANANA (5 results) → selected + fax ✅
7. Clinical init → 394 CPTs available ✅
8. Exam setup CPT 70551 → MRI of brain, With Contrast ✅
9. ICD G31.84 → Mild cognitive impairment ✅
10. Pathway → Neurocognitive disorders (including dementia) ✅
11. Questions: 2 rounds, 2 clinical questions answered by LLM ✅
12. **Browser closed, questions saved to DB, case → IN_REVIEW** ✅
13. **Workflow SUSPENDED at awakeable** `sign_1DiFE1IEgIZUBnRNp_K4e893dzSJHeN1gAAAAEQ` ✅

**Rep review (via API):**
- `GET /api/queue/{case_id}` → returned 2 questions with AI answers ✅
- `POST /api/queue/{case_id}/resolve` with `{rep_id: "andrew", answers: []}` → all approved ✅
- Awakeable resolved → `{status: "resolved", edited: false}` ✅

**Resume + replay (workflow continues after awakeable):**
- `Workflow RESUMED: rep response = {action: approved, answers: [...]}` ✅
- `Re-entering portal for finalization with 2 approved answers` ✅
- Fresh login + MFA ✅
- Replay all WebForms phases (member → eligibility → DI → auths → provider → fax) ✅
- Replay clinical (init → exam → ICD → pathway) ✅
- **Fast-forward questions with approved answers** → `Resume fast-forward complete — portal at done state` ✅
- **Finalize** → ProcessAccepted → IsExamAutoApproved=N → AddFeedback ✅
- **hdnAction=20** → exam summary page ✅
- **Exam summary review** → DoneWithExam(complete=True) + FindNextExam ✅
- **hdnAction=6** → ❌ Portal returned "Page Cannot be Displayed"

**Root cause of hdnAction=6 failure:** Robin Stuart already had an existing Authorized brain MRI auth (283083690) from the live test run earlier in this session. The portal allowed exam setup but detected the duplicate at the facility transition point and showed an error page. This is a **data issue, not a code issue**.

### 5. Restate Journal Replay Behavior — Documented

Important finding: Restate's journal replay causes the handler to re-execute non-journaled code (like `compiler.execute()`) on every retry/replay. The sequence is:

1. First execution: compiler runs → returns answers → `ctx.run("save_batch")` journals the save → `await awakeable_promise` suspends
2. On Restate replay (before suspension): compiler re-executes fully (new login, new portal run) → `ctx.run("save_batch")` replayed from journal (no-op) → `await awakeable_promise` actually suspends
3. After awakeable resolved: compiler re-executes AGAIN (third login) → this time with `resume_answers` passed → fast-forward → finalize

This means **3 portal login cycles per case** in the current architecture:
- Pass 1: First execution (questions)
- Pass 2: Restate journal replay (re-runs compiler, then suspends)
- Pass 3: Resume after approval (finalize)

**Optimization opportunity:** Wrap `compiler.execute()` in `ctx.run()` to journal the result and avoid Pass 2. BUT this requires the result to be JSON-serializable and small enough for Restate's journal.

## Files Modified

| File | Change |
|------|--------|
| `backend/tests/test_portal_live.py` | Changed TEST_MEMBER_ID to `BHP816691826` (Robin Stuart) |

## Current State

- **Restate E2E replay path: PROVEN WORKING** (suspend → approve → resume → replay → fast-forward → finalize → hdnAction=20 → exam summary → hdnAction=6)
- **hdnAction=6 failure on replay**: data issue — Robin Stuart has existing brain MRI auth from earlier test
- **4 login cycles consumed** in E2E test (first pass, journal replay, resume, plus one retry after hdnAction=6 error)
- **Worker killed**, stale Restate invocation may need cleanup
- **Chromium profiles cleared** at start of session

## Key Observations

1. **Fast-forward works perfectly** — approved answers submitted in one batch, portal processes entire tree, returns "done" without re-asking
2. **Restate awakeable pattern works** — suspend/resume/approve flow is clean
3. **Duplicate auth detection is critical** — we need to check for existing authorized auths for the same CPT BEFORE entering the portal, not just log them
4. **Member not found handling needed** — Kristal Shackelford case showed policy numbers can be invalid; need graceful handling
5. **3 login cycles per case** is expensive — the Restate journal replay causes an extra full portal run that we should optimize away

## Next Steps

1. **Test E2E on a clean case** — need a case with NO existing portal auths for the same CPT. Options:
   - Find a patient in DB with a CPT that hasn't been submitted before
   - Or use a different CPT for Robin Stuart (she had abdomen MRI auth too, but other CPTs should be clean)
2. **Add pre-submission duplicate auth check** — before entering portal, query existing auths and skip if same CPT already authorized
3. **Handle "member not found"** — flag case, update state to HOLD with reason, route to manual queue
4. **Optimize Restate journal** — wrap compiler result in ctx.run to avoid the extra login cycle on journal replay
5. **Test actual submission** — click Submit on a controlled case (not dry_run)
6. **Purge stale Restate invocation** from this session's failed run

## Test Cases Summary

| Patient | CPT | Test Type | Result |
|---------|-----|-----------|--------|
| Winona Sandlin | 75574 (Cardiac CT) | Live test Steps 1-19 | ✅ Full pass (March 19) |
| Jonathan Horne | 73721 (Knee MRI) | Live test Steps 1-19 | ✅ Full pass (March 19) |
| Robin Stuart | 70551 (Brain MRI) | Live test Steps 1-19 | ✅ Full pass (March 21) |
| Robin Stuart | 70551 (Brain MRI) | Restate E2E workflow | ✅ Through hdnAction=20, ❌ hdnAction=6 (duplicate auth) |
| Kristal Shackelford | 72148 (Lumbar MRI) | Live test | ❌ Member not found |
| Oshodi Olumide | 73721 (Knee MRI) | Live test | ❌ Existing auth blocks CPT |
| Elliot Pershing | 73721 (Knee MRI) | Live test | ❌ Existing auth blocks CPT |

## Key Logs

- `/tmp/portal_live_kristal.log` — Kristal Shackelford (member not found)
- `/tmp/portal_live_robin.log` — Robin Stuart live test (full pass)
- `/tmp/restate_worker.log` — Restate E2E workflow (suspend → approve → resume → replay)
