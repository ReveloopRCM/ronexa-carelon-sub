# New Pipeline: Process ALL Cases Continuously

## Date: April 8, 2026

---

## Problem Statement

Currently we manually filter cases (`has_notes`) and assign specific workers to specific job types. The client wants ALL cases processed continuously — including awaiting-clinicals cases. We need a pipeline that:

1. Processes every case that comes in from Cosmos DB
2. Identifies NO_AUTH, Gold Card, and Algorithm Approved cases automatically
3. Leverages order forms (every case has one) as clinical evidence
4. Uses signature replay to answer clinical questions when no clinicals exist
5. Runs continuously with smart worker dispatch — no manual babysitting

---

## The Core Insight: Order Forms Are Clinical Evidence

Every case from the client has an **order form** (`file_key` in Cosmos DB). Examples:

**Karen Conley** — Chisholm Trail Orthopedics order:
- CPT 73722 (MRI Hip Arthrogram)
- ICD M70.62 (Trochanteric bursitis, left hip)
- Indication: "Trochanteric Bursitis, Left"
- Body part: Left hip, Protocol: MR Arthrogram
- Provider: Molly Lopez FNP-C, NPI 1487181889

**Sergio Lopez** — Direct Orthopedic Care order:
- Left shoulder MRI without contrast
- Insurance: Blue Cross Blue Shield TX
- Provider: Jessica MacPhee PA-C

These order forms contain the exam type, diagnosis, indication, body part, laterality, and provider — everything needed to get through Carelon's portal up to clinical questions. **You don't need clinical notes for many cases.**

---

## The Funnel: What Happens When You Run ALL Cases

```
ALL CASES FROM COSMOS DB
  |
  |  (1) Extract order form (file_key) -- every case has this
  |
  v
PORTAL FIRST PASS
  |
  |-- X  Member not found / Eligibility expired --> HOLD (nothing we can do)
  |
  |-- OK NO_AUTH_REQUIRED (~??%)
  |     Portal says "DI does not require pre-auth"
  |     No clinical questions. No notes needed. Just proof.
  |     --> Rep confirms --> DONE
  |
  |-- OK GOLD CARD (~??%)
  |     Provider has earned auto-approval privileges
  |     No clinical questions asked at all
  |     --> Rep confirms --> DONE
  |
  |-- Clinical Questions reached...
  |   |
  |   |  (2) Order context feeds the LLM:
  |   |     "Trochanteric Bursitis, Left Hip" + "MR Arthrogram"
  |   |     That's REAL clinical evidence -- it's what the doctor ordered and why
  |   |
  |   |  (3) Signature replay (if matching CPT+ICD exists):
  |   |     Pre-approved answer sequence for same exam type
  |   |
  |   |-- OK Algorithm Approved
  |   |     Portal's own algorithm said yes based on our answers
  |   |     --> Rep confirms --> SUBMIT --> DONE
  |   |
  |   |-- ~~ Answers look reasonable
  |   |     --> L1/L2 Review --> SUBMIT
  |   |
  |   |-- !! Can't answer confidently
  |         Keep case, flag "needs clinicals"
  |         Request notes from provider
  |
  |-- Cases where clinicals arrive later
      --> Re-run with full context --> higher confidence --> better outcomes
```

**Key realization**: NO_AUTH, Gold Card, and Algorithm Approved are all determined BEFORE or DURING clinical questions. You don't need clinical notes for any of them. You just need to run the case through the portal.

---

## The Flywheel Effect

```
More cases processed --> More signatures captured
More signatures --> More cases auto-resolved
More auto-resolved --> Less rep time per case
Less rep time --> Higher throughput
Higher throughput --> More signatures...
```

The signature library grows with every successful case. Every NO_AUTH, Gold Card, and Algorithm Approved case we catch is one less case sitting idle waiting for clinical notes that were never needed.

---

## Architecture Changes

### 1. Order Form Extraction (New)

The extraction service currently only pulls `clinical_blob_key`. We add extraction for `file_key` (order PDF):

```
file_key (order PDF) --> Azure Document Intelligence --> structured order data
                                                          |
                                                          |-- CPT code + description
                                                          |-- ICD codes + descriptions
                                                          |-- Indication / clinical reason
                                                          |-- Body part + laterality
                                                          |-- Protocol (contrast, arthrogram, etc.)
                                                          |-- Ordering provider + NPI
                                                          |-- Facility
```

Store as `ClinicalNote` with `document_type="ORDER_FORM"`. This happens for **every case at ingestion**.

After order extraction --> case moves to `NOTES_UPLOADED` (eligible for first-pass).

### 2. Two Tiers of Clinical Context

When the worker builds the event for the LLM:

| Has clinicals? | Has order form? | Clinical context | Confidence expectation |
|---|---|---|---|
| Yes | Yes | Full notes + order form (richest) | High |
| No | Yes | Order form only | Medium -- enough for many questions |
| No | No | Case metadata only (CPT, ICD from DB) | Low -- mostly signature replay |

The LLM prompt section changes based on what's available:

- **Full clinicals**: "Clinical documentation extracted from patient's notes" (existing)
- **Order form only**: "Physician's order for this exam -- use the diagnosis, indication, and exam details as clinical evidence. The ordering physician has determined this exam is medically necessary for the stated indication."
- **No docs**: "No clinical documentation available -- rely on signature patterns and case metadata"

### 3. Worker Priority Cascade

Remove dedicated worker job types. Every worker runs the same cascade:

```
Every worker: SUBMIT --> FIRST_PASS --> SIGNATURE_REPLAY --> Sleep
```

**Why SUBMIT first:**
- Submissions are the finish line -- rep already reviewed, answers approved
- Submissions are fast (~2-3 min, replay approved answers + click submit)
- Volume is naturally lower (only approved cases), won't starve first-pass
- No idle worker while submissions wait

**Why NOT a dedicated submitter:**
- If submit worker goes down, nothing gets submitted
- If no submissions pending (common -- reps review in batches), a worker sits idle
- You lose 33% first-pass throughput for a queue that's empty most of the time

Worker loop change (~20 lines in `worker_loop.py`):
```python
# Priority cascade -- every worker tries all job types in order
for jt in ["SUBMIT", "FIRST_PASS", "SIGNATURE_REPLAY"]:
    event = await _claim_and_build_event(worker_id, jt)
    if event:
        break
# if no event --> sleep on awakeable + 5 min fallback
```

### 4. Post-First-Pass Routing

After first-pass completes, routing adds awareness of context level:

```python
if outcome == "NO_AUTH":
    --> IN_REVIEW (rep confirms, already built April 7)
elif outcome == "GOLD_CARD" or outcome == "ALGORITHM_APPROVED":
    --> L1_REVIEW or CLINICAL_REVIEW (fast review)
elif all_questions_answered and avg_confidence >= threshold:
    --> L1_REVIEW --> L2_REVIEW --> SUBMIT (normal flow)
elif low_confidence and no_clinical_notes:
    --> AWAITING_CLINICALS (flag case, request clinicals from provider)
    # "We tried, portal needs more info, request clinicals"
```

### 5. Re-run When Clinicals Arrive

When clinical notes finally arrive for a case that was already run:
- Extract clinicals (existing flow)
- Re-queue as FIRST_PASS with full context
- LLM now has both order form + clinical notes
- Much higher confidence answers
- Signature replay + clinical evidence = strong case

---

## Current vs New: Data Sources

### Current (file_key ignored for processing)

| Cosmos DB Field | Case Column | Used For | Used In Processing? |
|---|---|---|---|
| `FileKey` | `file_key` | Order PDF blob key | NO -- only stored, not extracted |
| `ClinicalAttachments` | `clinical_blob_key` | Clinical notes PDF | YES -- extracted, feeds LLM |
| `ClinicalHistoryFileKey` | `raw_data` (JSON) | Unknown | NO -- stored in raw_data only |

### New (file_key becomes primary for all cases)

| Cosmos DB Field | Case Column | Used For | Used In Processing? |
|---|---|---|---|
| `FileKey` | `file_key` | Order PDF blob key | YES -- extracted as ORDER_FORM, feeds LLM |
| `ClinicalAttachments` | `clinical_blob_key` | Clinical notes PDF | YES -- extracted, feeds LLM (when available) |
| `ClinicalHistoryFileKey` | `raw_data` (JSON) | Possible additional clinicals | FUTURE -- investigate |

---

## Implementation Plan (Ordered by Priority)

| # | Change | Files | Impact | Effort |
|---|---|---|---|---|
| 1 | **Extract order forms** (`file_key`) in extraction service | `extraction_service.py`, `blob_fetcher.py` | Every case gets structured order data | Medium |
| 2 | **Move cases to NOTES_UPLOADED after order extraction** | `extraction_service.py`, `sync_engine.py` | ALL cases eligible for first-pass | Small |
| 3 | **Worker priority cascade** (SUBMIT --> FIRST_PASS --> SIGNATURE_REPLAY) | `worker_loop.py` | Smart dispatch, no dedicated workers | Small |
| 4 | **LLM prompt for order-form-only context** | `prompts.py`, `evaluator.py` | Better answers without clinicals | Medium |
| 5 | **Combine order context + signature replay** | `evaluator.py`, `portal_compiler.py` | Best possible answers without clinicals | Medium |
| 6 | **"Needs clinicals" routing** after first-pass | `helpers.py`, `portal_compiler.py` | Flag cases that truly need clinical notes | Small |
| 7 | **Re-run capability when clinicals arrive** | Existing rerun flow | Second pass with full context | Already exists |
| 8 | **Wake workers on SUBMIT jobs** | `queue.py` | Instant submission pickup after rep approval | Small (~5 lines) |

**No new workers. No new infrastructure. Same 3 VMs. Same deployment.**

The order extraction is the unlock -- it turns every PENDING_NOTES case into a NOTES_UPLOADED case that can be processed.

---

## What We Learn From Running Everything

Running all cases gives us data we don't have today:

- **What % of cases are NO_AUTH?** (could be significant -- saves the most time)
- **What % are Gold Card?** (provider-level insight)
- **What % get Algorithm Approved with just order form context?**
- **Which CPT+ICD combos always need clinicals?** (target clinical requests)
- **Signature library growth rate** (how fast does the flywheel spin?)

This data shapes the next iteration of the pipeline.

---

## Risk Assessment

| Risk | Mitigation |
|---|---|
| Order form extraction quality varies | Azure Doc Intelligence handles structured forms well; order forms are cleaner than clinical notes |
| LLM answers poorly without clinicals | Confidence scoring already exists; low-confidence cases route to "needs clinicals" |
| Too many cases overwhelm workers | Priority cascade ensures SUBMIT (finish line) always goes first; STAT cases have priority=1000 |
| Portal rate limiting from higher volume | Behavior engine already simulates human timing; 3 workers is within normal rep volume |
| Signature replay answers wrong questions | Signatures are keyed by CPT+ICD+pathway; mismatch detection already built |

---

## Success Criteria

1. All cases from Cosmos DB are processed within 24 hours of ingestion
2. NO_AUTH and Gold Card cases identified and resolved without clinical notes
3. Signature library grows daily from successful cases
4. Workers never sit idle while work exists (priority cascade)
5. Rep review queue shows cases with order form context (not empty text boxes)
6. Cases that truly need clinicals are flagged and tracked separately
