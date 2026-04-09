# Plan: Rename Cases → Exams + Single/Multi-Exam Queue

## Overview

Two changes:
1. **Rename "Cases" → "Exams"** throughout the frontend UI text
2. **Group exams by patient** — identify single-exam vs multi-exam orders and show them in separate queues

## Real Data

```
Total exams in DB: 179
Single-exam patients: 159
Multi-exam patients: 8 (totaling 20 exams)

Examples:
  Marny Webster — 5 exams: 72141, 72146, 72148, 72195, 73221
  Leslie Ramirez — 3 exams: 71260, 71275, 74178
  Gary Lucas — 2 exams: 72141, 72148
```

**Grouping key**: `(policy_num, first_name, last_name)` — most reliable. `OrderRequestID` is null for many cases and Leslie Ramirez has 3 different OrderRequestIDs for her 3 exams, so it can't be the sole grouping key.

---

## Part 1: Rename Cases → Exams (Frontend Only)

**Scope**: UI text only. No backend route changes, no DB changes, no file/folder renames. Keep `/cases` API routes and `/cases` URL paths as-is (avoid breaking bookmarks/links). Just change what the user sees.

### Files & Changes

| File | UI Text Changes |
|------|-----------------|
| `layout.tsx` | Nav link: "Cases" → "Exams" |
| `page.tsx` (home) | Card heading: "Cases" → "Exams", description updated |
| `upload/page.tsx` | "Upload Cases" → "Upload Exams", "Create Cases" → "Create Exams", "View Cases" → "View Exams", success/skip messages |
| `cases/page.tsx` | Page heading: "Cases" → "Exams", empty state messages, sync messages |
| `cases/[caseId]/page.tsx` | "Case: {id}" → "Exam: {id}", "Case not found" → "Exam not found" |
| `queue/page.tsx` | "No cases awaiting review" → "No exams awaiting review" |
| `queue/[caseId]/page.tsx` | "Case Summary" → "Exam Summary", "Case not found" → "Exam not found" |

**NOT changing**: file paths, URL routes, API endpoints, variable names, backend code. This is purely display text.

---

## Part 2: Single vs Multi-Exam Queue

### Approach

Add a **patient grouping layer** on the backend API that groups exams by `(policy_num, first_name, last_name)` and returns an `exam_count` per patient. The frontend uses this to show tabs: **Single Exam** | **Multi-Exam**.

### Backend Changes

**`backend/app/api/routes/cases.py`** — Enhance the list endpoint:

Add `exam_count` and `sibling_exam_ids` to each case in the list response. This is computed via a window function or subquery — no schema change needed:

```python
# In the list query, add a subquery for exam grouping
# For each case, count how many cases share the same (policy_num, first_name, last_name)
# and return sibling exam_ids

@router.get("/")
async def list_cases(...):
    # existing query + add:
    # - exam_count: how many exams this patient has
    # - sibling_ids: list of other exam IDs for this patient
    # - queue_type: "single" or "multi"
```

Concretely, after fetching cases, do a grouping pass:

```python
# Group by patient key
from collections import defaultdict
patient_groups = defaultdict(list)
for c in cases:
    key = (c.policy_num, c.first_name, c.last_name)
    patient_groups[key].append(c)

# Annotate each case with group info
for case_dict in result:
    key = (case_dict["policy_num"], case_dict["first_name"], case_dict["last_name"])
    group = patient_groups[key]
    case_dict["exam_count"] = len(group)
    case_dict["sibling_exam_ids"] = [g.exam_id for g in group if g.exam_id != case_dict["exam_id"]]
    case_dict["queue_type"] = "multi" if len(group) > 1 else "single"
```

### Frontend Changes

**`cases/page.tsx`** — Add sub-tabs within the Active tab:

```
Active tab:
  [All] [Single Exam] [Multi-Exam]
```

- **All**: Shows all active exams (current behavior)
- **Single Exam**: Shows only exams where `exam_count === 1`
- **Multi-Exam**: Shows only exams where `exam_count > 1`, grouped by patient

For multi-exam view, group exams visually by patient:
```
┌─ Marny Webster (T2G944302524) — 5 exams ──────────────┐
│  72141  Cervical Spine MRI   PENDING_NOTES             │
│  72146  Thoracic Spine MRI   PENDING_NOTES             │
│  72148  Lumbar Spine MRI     PENDING_NOTES             │
│  72195  Pelvis MRI           PENDING_NOTES             │
│  73221  Shoulder MRI         PENDING_NOTES             │
└────────────────────────────────────────────────────────┘

┌─ Leslie Ramirez (T2U811058230) — 3 exams ─────────────┐
│  71260  Chest CT             PENDING_NOTES             │
│  71275  CTA Chest            PENDING_NOTES             │
│  74178  Abdomen CT           PENDING_NOTES             │
└────────────────────────────────────────────────────────┘
```

**`cases/[caseId]/page.tsx`** — On exam detail, if multi-exam:
- Show a small "Related Exams" section listing sibling exams with links
- e.g. "Part of 5-exam order for Marny Webster" with clickable sibling exam IDs

---

## What This Does NOT Do

- No DB schema changes (no new tables, no new columns)
- No backend URL route renames (keeps `/api/cases/...`)
- No frontend URL route renames (keeps `/cases/...`)
- No changes to the compiler or workflow (multi-exam portal automation is a separate future task)
- No changes to variable names in code (just UI display text)
- The grouping is purely for **visibility and triage** — the workflow still processes one exam at a time

## File Summary

| File | Change |
|------|--------|
| `frontend/app/layout.tsx` | "Cases" → "Exams" in nav |
| `frontend/app/page.tsx` | "Cases" → "Exams" on home |
| `frontend/app/upload/page.tsx` | All "case" → "exam" in UI text |
| `frontend/app/cases/page.tsx` | Heading rename + Single/Multi sub-tabs + grouped multi-exam view |
| `frontend/app/cases/[caseId]/page.tsx` | "Case" → "Exam" + Related Exams section |
| `frontend/app/queue/page.tsx` | "cases" → "exams" in empty state |
| `frontend/app/queue/[caseId]/page.tsx` | "Case Summary" → "Exam Summary" |
| `backend/app/api/routes/cases.py` | Add `exam_count`, `sibling_exam_ids`, `queue_type` to list response |
