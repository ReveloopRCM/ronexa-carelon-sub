# Awaiting Clinicals Analysis — Strategy Document

## Overview

Probe the Carelon portal with cases that don't have clinical notes yet. Detect Gold Card and Algorithm auto-approvals **before** clinicals arrive. Surface to reps for review and submission — eliminating the wait for fax-backs from doctors.

## Portal API Sequence (Probe — No Submission)

```
┌─── Clinical SPA (all XHR, no page transitions) ───────────┐
│                                                             │
│  GetCase (init)                                             │
│  SetSelectedExam (CPT)                                      │
│  ValidateExam (body side)                                   │
│  AddExam                                                    │
│  SearchDiagnosis (ICD)                                      │
│  SetSelectedDiagnosis                                       │
│  IsBypassAndGoldCardStateForExam  ← Gold Card check         │
│  GetPathwayOptions                                          │
│  SetPathway                                                 │
│  GetPathwayAssetsWithValidation   ← Questions come back     │
│  SetSelectedAnswer (loop)         ← Answer each question    │
│  ProcessAccepted                  ← Portal processes        │
│  IsExamAutoApproved               ← Approval check          │
│                                                             │
│  *** STOP HERE — navigate home ***                          │
│                                                             │
│  (We do NOT call:)                                          │
│  DoneWithExam, hdnAction=20, Facility, Submit               │
└─────────────────────────────────────────────────────────────┘
```

Everything from GetCase through IsExamAutoApproved is XHR inside the clinical SPA. No page transitions, no form submissions. The browser stays on the same URL. Navigate home afterward — portal treats it as an abandoned order (normal rep behavior).

## Two Detection Points

1. **Gold Card** — `IsBypassAndGoldCardStateForExam` returns `GoldCardLevel: 2` after diagnosis entry. Portal auto-approves regardless of answers. Provider reputation bypass.

2. **Algorithm Auto-Approval** — `IsExamAutoApproved` returns true after `ProcessAccepted`. Portal's algorithm decided pathway + answers = approval. Independent of clinical notes.

## Strategy

### Trigger
Manual — "Start Awaiting Batch" button on Settings page (like existing "Start Portal Batch"). Rep controls when probes run.

### Outcomes
- **auto_approved = true + Gold Card** → case → AWAITING_CLINICAL_REVIEW (gold_card tag)
- **auto_approved = true + Algorithm** → case → AWAITING_CLINICAL_REVIEW (algorithm tag)
- **auto_approved = false** → silently back to PENDING_NOTES + audit log (no noise)

### Review Flow
- New "Awaiting Clinical" tab on queue page with Gold Card / Algorithm sub-tabs
- Same review UI as L2 but without clinical notes section
- Rep options: "Submit" (proceed to submission) or "Wait for Clinicals" (return to queue)

### LLM Prompt Strategy
For Algorithm cases (questions must be answered without clinicals):
- Pathway-focused prompt instead of clinical-grounded prompt
- Uses RAG patterns from past approved cases
- Uses pathway intelligence (CPT+ICD → right pathway)
- Confidence reflects likelihood of approval, not clinical certainty

## Architecture: Separate Workflow

This is NOT an extension of the existing first-pass workflow. Separate concerns:
- Separate compiler (`probe_compiler.py`) with truncated phase sequence
- Separate job type (`PROBE`)
- Separate worker endpoint (`POST /probe-case`)
- Separate review state (`AWAITING_CLINICAL_REVIEW`)
- Separate review/resolve endpoints

## Strategy Analysis

### What's Brilliant

1. **Exploiting Carelon's architecture.** The portal separates the clinical decision engine (IsExamAutoApproved) from submission mechanics (DoneWithExam → facility → submit). We peek at the answer before committing.

2. **Gold Card is pure arbitrage.** Zero clinical effort, zero question answering risk. Portal says "this provider is trusted, skip everything." Potentially 15-25% of cases.

3. **Inverts the traditional workflow.** Instead of: get clinicals → answer questions → hope for approval, we do: answer questions strategically → confirm approval → THEN decide if we even need clinicals.

4. **Data moat compounds.** Every probe feeds RAG: pathway+ICD+answers+outcome. System gets smarter with volume.

### Gaps & Mitigations

**Gap 1: Pathway selection without clinicals is the critical risk.**
- Early on, RAG has near-zero data for unknown CPT+ICD combos
- First 50-100 probes are exploration (building the map)
- **Mitigation:** Prioritize probing CPT+ICD combos with existing pathway intelligence data. Unknown combos go through normal flow first.

**Gap 2: Question answering without clinicals — LLM is guessing on clinical-fact questions.**
- Questions like "Duration of symptoms?" can't be answered from pathway knowledge alone
- **Good news:** Many Carelon questions are protocol/pathway-alignment, not patient-specific
- **Mitigation:** Probe failures cost nothing (silent return). Failed probes still teach RAG what doesn't work.

**Gap 3: Probe-then-submit = two portal sessions.**
- Submit job re-runs entire flow from scratch (portal doesn't remember probe)
- **Mitigation:** Save probe answers for replay. Existing resume mechanism handles this. Portal state is stable between probe and submit.

**Gap 4: Portal traffic / abandoned order volume.**
- 200 probes = 200 abandoned orders. Could raise flags at scale.
- **Mitigation:** Manual batch control. Start with 20-30 per batch. Stagger.

**Gap 5: No early exit for low-confidence probes.**
- Running unknown CPT+ICD combos with zero RAG data through full question loop wastes portal sessions.
- **Future optimization:** Confidence gate after pathway selection. Skip question loop if RAG returns zero patterns.

### What's NOT a Risk

- Portal doesn't penalize abandoned orders (normal rep behavior)
- Gold Card detection is deterministic (portal tells us explicitly)
- Algorithm approval is deterministic (IsExamAutoApproved is hard yes/no)
- Failed probes have zero downside (case stays in PENDING_NOTES, clinicals flow takes over later)

## Approval Rate Projection

| Category | Expected Rate | Why |
|----------|--------------|-----|
| Gold Card | ~100% | Portal decides, not us |
| Algorithm (known CPT+ICD) | 60-80% with RAG | Right pathway from past data |
| Algorithm (unknown CPT+ICD) | 20-40% initially | LLM guessing, improves with volume |
| Overall (blended) | 40-60% initially → 70-80% at volume | Data moat effect |

## Phase 1 Prioritization

When building awaiting batch, sort by likelihood of success:
1. CPT+ICD combos with known Gold Card history → probe first
2. CPT+ICD combos with known Algorithm approval from signatures → probe second
3. Unknown CPT+ICD combos → probe last (or defer)

## Files to Modify

| File | Change | Type |
|------|--------|------|
| `backend/app/db/models.py` | AWAITING_CLINICAL_REVIEW state | Edit |
| `backend/alembic/versions/021_*.py` | Migration for new enum | New |
| `backend/app/compiler/probe_compiler.py` | Probe compiler (truncated phases) | New |
| `backend/app/intelligence/prompts.py` | Pathway-focused probe prompt | Edit |
| `backend/app/db/queue.py` | PROBE claim + enqueue_awaiting_batch() | Edit |
| `backend/app/worker/http_server.py` | POST /probe-case endpoint | Edit |
| `backend/app/workflow/worker_loop.py` | PROBE job dispatch | Edit |
| `backend/app/api/routes/settings.py` | POST /start-awaiting-batch | Edit |
| `backend/app/api/routes/queue.py` | Awaiting list + resolve-awaiting | Edit |
| `frontend/lib/api.ts` | API functions | Edit |
| `frontend/app/settings/page.tsx` | Start Awaiting Batch button | Edit |
| `frontend/app/queue/page.tsx` | Awaiting Clinical tab | Edit |
| `frontend/app/queue/[caseId]/page.tsx` | Awaiting clinical review mode | Edit |
