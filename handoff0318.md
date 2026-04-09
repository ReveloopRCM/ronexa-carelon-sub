# Handoff — March 18, 2026

## What was accomplished today

### Fix 1: Question Loop — Full Decision Tree Traversal
**Two bugs fixed in `backend/app/compiler/portal_compiler.py`:**

1. **Wrong QuestionId key** (line 343): Compiler used `q.get("QuestionId")` but `_parse_asset_question()` returns the key as `"Id"`. This caused `to_portal_answer()` to send empty QuestionIds to the portal API, so the portal never advanced — it returned the same first question ("Select all tests...") on every iteration. Fixed to `q.get("Id") or q.get("QuestionId", "")`.

2. **Premature loop exit** (lines 326-336): `seen_question_ids` set broke the loop when the portal reused the same QuestionId across different GroupIds (normal for a decision tree). Removed entirely — `accumulator.get_new_groups()` already deduplicates by GroupId, and the `done` flag + 20-iteration cap are sufficient guards.

Also added `sequence` to the `PortalObservation` constructor (was missing).

### Live Test Verification
Ran `python3 -m tests.test_portal_live` — full end-to-end success:
- 14 rounds, groups 3→16, 16 total answers (2 pre-filled + 14 LLM)
- Ended with "No more questions — clinical questionnaire complete"
- Finalization completed: PrecertID 616255152, auto_approved=N
- Completeness gate: PASSED (1 low-confidence, 0 no-evidence)

### Restate Workflow E2E Test
- Triggered `POST /api/cases/{case_id}/process`
- **14 distinct questions** saved to DB with correct text, confidence, and evidence
- Workflow **SUSPENDED** at awakeable `sign_13pCWc6fPbg8BnQQLD5c7o-27lJ2ckKYeAAAAEQ`
- Total runtime: ~4 min 44 sec (login → questions → suspend)
- Case: Winona Sandlin (`860a5320-dacd-4f7c-bbb8-9648c039c43b`), CPT 75574, ICD R03.0

## Current State

- **Case status**: `IN_REVIEW` with 14 questions pending rep review in GUI
- **Restate invocation**: `inv_11k9dIFF5ZJY0VPTbQ64dgfUeFmzicm3mh` — SUSPENDED
- **Awakeable**: `sign_13pCWc6fPbg8BnQQLD5c7o-27lJ2ckKYeAAAAEQ`
- **Infrastructure**: Docker (Postgres, Redis, Restate) running, FastAPI on :8000, Worker on :9080

## Known Issue: Restate Inactivity Timeout

`compiler.execute()` takes ~5 min without any `ctx.run()` journal writes (all browser work is non-journaled). Restate's default inactivity timeout (~60s) triggers a retry. The retry works correctly — journal replays instantly, then re-executes browser work, hits the journal again, and suspends. But it causes an unnecessary extra login + portal traversal.

**Fix options (not yet implemented):**
- Increase Restate inactivity timeout in Docker config
- Add periodic heartbeat `ctx.run("heartbeat_N", lambda: None)` calls during compiler execution
- Split the compiler into journaled sub-steps

## Next Steps

1. **Test awakeable resolve flow** — rep approves in GUI → `curl localhost:8080/restate/awakeables/{id}/resolve --json '{"approved": true}'` → workflow resumes → finalize
2. **Handle backtrack on edit** — rep edits an answer → re-entry to question loop with changed answer
3. **Fix Restate inactivity timeout** — prevent unnecessary retries during long browser work
4. **Scale testing** — try a second case to verify idempotency and different tree paths

## Key Files Modified Today

| File | Change |
|------|--------|
| `backend/app/compiler/portal_compiler.py` | Fixed QuestionId key (`Id` not `QuestionId`), removed `seen_question_ids` break, added `sequence` to observation |
