# Handoff — March 19-20, 2026

## What was accomplished (March 19 — daytime)

### Fix 1: Post-Questions Page Transition — HAR-Proven hdnAction Postbacks

**The problem:** After clinical questions + finalize, the browser sat on the clinical SPA page (`/exam-entry`). The compiler had a 200-line diagnostic/guess-and-check block trying to navigate back to WebForms via button clicks and `goto Default.aspx`. It never worked — the SPA replaces the DOM so `hdnAction` field lookups failed.

**The fix:** Analyzed 3 HAR files to discover the actual mechanism:
1. The clinical SPA runs INSIDE `Default.aspx` — a hidden `<form action="Default.aspx">` with `__VIEWSTATE` persists in the DOM
2. The SPA submits this hidden form with `hdnAction=20` (full document navigation, not XHR) to transition to the exam summary page
3. After exam summary review, another form submission with `hdnAction=6` transitions to the facility search page

**Files modified:**

| File | Change |
|------|--------|
| `backend/app/portal/clinical_flow.py` | Split `finalize()` — stops after AddRadioTracersIfEligible. New `exam_summary_review()` runs GetCase, DoneWithExam, FindNextExam on the summary page |
| `backend/app/portal/webforms_client.py` | New `postback_hdnaction(action_value)` — sets hdnAction hidden field + submits the ASP.NET form for page transitions |
| `backend/app/compiler/portal_compiler.py` | Rewrote `clinical_complete` phase: finalize() → hdnAction=20 → exam_summary_review() → hdnAction=6. Simplified `facility_search` (removed 200-line ordering provider block) |
| `backend/tests/test_portal_live.py` | Extended to Steps 15-19: finalize → exam summary → facility search → order preview. Stops before Submit |

### Fix 2: finalize() Split for Correct API Ordering

- `finalize()` now only does: ProcessAccepted → IsExamAutoApproved → CDO → AddFeedback → AddRadioTracersIfEligible
- New `exam_summary_review()` does: GetCase → GetCptCodeTable → CheckIfAdditionalDocRequired (x3) → DoneWithExam → FindNextExam

### Live Test — Winona Sandlin (March 19)

Full Steps 1-19 verified: 14 questions → finalize → hdnAction=20 → summary → hdnAction=6 → facility → order preview. Stopped before Submit.

---

## What was accomplished (March 19 — evening session)

### Group 1 "Missing Questions" Investigation

**Concern:** Jonathan Horne case only saved 2 question groups (G2, G3) — Group 1 appeared missing, raising worry that we were skipping the root clinical decision tree question.

**Finding:** Group 1 is **"Date of Birth"** — a `QType=2` (numeric/date) question with `ForDisplay=False`. The portal auto-fills it from case data. It is NOT a clinical question. The real clinical decision tree starts at Group 2.

**Evidence (from Jonathan Horne live test):**
```
ASSET[0] Type=1 ForDisplay=False GroupId=1 QType=2 Seq=1 | Date of Birth
ASSET[1] Type=2 ForDisplay=True  GroupId=1 QType=None Seq=2 |  (recommendation)
ASSET[2] Type=1 ForDisplay=True  GroupId=2 QType=3 Seq=1 | Select from the following clinical scenarios.
ASSET[3] Type=2 ForDisplay=True  GroupId=2 QType=None Seq=2 |  (recommendation)
```

**Code change:** Added raw asset dump logging to `clinical_flow.py:get_questions()` — logs ALL assets (ForDisplay True AND False) with full detail. Returns `hidden_questions` separately from `questions` so we can see what the portal auto-answers without sending them to the LLM.

### Live Test — Jonathan Horne (CPT 73721, ICD M24.662)

Full Steps 1-19 passed:
- 2 clinical questions (G2: "Select clinical scenario" → "Other knee indications", G3: "Select diagnosis" → "None of these apply")
- Finalize → hdnAction=20 → `/summary` ✓
- Exam summary review → hdnAction=6 → `/Default.aspx` ✓
- Facility search NPI 1144550369 → Envision Imaging at Plano → fax → Continue ✓
- **Order Request Preview**: "Has Not Been Submitted", HORNE, JONATHAN, Submit button visible ✓

### Attempted Cases That Failed

1. **Oshodi Olumide** (exam 17101960, CPT 73721, ICD M25.561)
   - Names were swapped in DB (first/last reversed from Mongo import) — fixed
   - Member found after swap, but **existing Authorized auth** (order 283350578) for same CPT blocked exam setup (0 available CPTs)
   - **Root cause:** Portal won't allow duplicate CPT when there's already an active authorization

2. **Elliot Pershing** (CPT 73721, ICD M23.8X1)
   - Same issue — existing Authorized auth (282670120) for same CPT, 0 available CPTs
   - Also had 2 Non-Authorized auths from prior attempts

**Lesson:** Cases with existing Authorized auths for the same CPT will fail at exam setup. Need to pick cases with NO prior authorized auth, or use a different CPT.

### Cleanup Done

- Killed stale Restate invocation (`inv_195L3AxFayEZ0FBt9J0DONMhjvSFp5YSHL`)
- Cleared all chromium profiles
- Verified 0 remaining Restate invocations
- Extracted clinical notes for Oshodi Olumide and Elliot Pershing (saved to DB)
- Fixed Oshodi Olumide name swap (first_name ↔ last_name)

### Data Issue: Mongo Name Import

Oshodi Olumide had first/last names swapped in the DB. This may affect other cases imported from Cosmos DB. The Mongo poller or import logic may be mapping `FirstName`/`LastName` fields incorrectly for some records.

---

## Files Modified (Evening Session)

| File | Change |
|------|--------|
| `backend/app/portal/clinical_flow.py` | Added raw asset dump logging in `get_questions()`. Returns `hidden_questions` (ForDisplay=False) separately. Logs all asset details including Type, ForDisplay, GroupId, QType, Options |
| `backend/tests/test_portal_live.py` | Changed TEST_MEMBER_ID to `RZL23692209` (Kristal Shackelford — CPT 72148, ICD M43.22). This was the next case queued but not yet tested |

## Current State

- **Steps 1-19 proven working** on 2 different cases (Winona Sandlin CPT 75574, Jonathan Horne CPT 73721)
- **Group 1 mystery resolved** — it's auto-filled DOB, not a clinical question
- **Diagnostic logging in place** — raw asset dumps show everything portal returns
- **Not yet tested**: Kristal Shackelford (CPT 72148, ICD M43.22) — notes extracted, case prepped, test script configured but not run yet
- **Not yet tested**: full Restate workflow E2E with replay path
- **Not yet tested**: actual submission (clicking Submit This Request)

## Next Steps (Morning)

1. **Run Kristal Shackelford test** — `python3 -m tests.test_portal_live` (already configured, CPT 72148 lumbar MRI, should have no prior auth conflicts)
2. **If Steps 1-19 pass**, move to full Restate E2E workflow:
   - Reset case state → trigger workflow → first pass answers questions → SUSPEND at awakeable
   - Approve in GUI → workflow RESUMES → fresh login → replay all phases → fast-forward questions → finalize → facility → dry_run stop
3. **Test actual submission** on a controlled case
4. **Investigate Mongo name import** — check if other cases have swapped first/last names
5. **Add duplicate auth detection** — cases with existing Authorized auth for same CPT should be caught BEFORE entering the portal (save time and login cycles)

## Prepped Test Case

**Kristal Shackelford**
- Case ID: `1929efc6-0cc8-40c6-b0bf-100bfe41f6d4`
- Policy: `RZL23692209`
- CPT: 72148 (Lumbar MRI) — different from previous tests
- ICD: M43.22 (Fusion of spine, cervical region)
- Center NPI: 1144550369 (Envision Plano)
- Referring NPI: 1043501232
- Clinical notes: extracted (8 pages, lumbar radiculopathy + spine fusion)
- State: NOTES_UPLOADED
- No referring fax on file (will hit "Fax Unavailable" flow)

## Key Logs

- `/tmp/portal_live_test.log` — Jonathan Horne full run (successful)
- `/tmp/portal_live_oshodi.log` — Oshodi Olumide first attempt (name swap, member not found)
- `/tmp/portal_live_oshodi2.log` — Oshodi Olumide second attempt (existing auth, 0 CPTs)
- `/tmp/portal_live_pershing.log` — Elliot Pershing (existing auth, 0 CPTs)
