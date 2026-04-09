# Plan: Restate Fan-Out Extraction Pipeline

## Context

Clinical extraction is currently serial inside the sync HTTP handler — one case at a time, ~8s per case for blob download + Azure Document Intelligence OCR. At 200+ cases, this takes 25+ minutes and times out the HTTP request. Production needs parallel extraction that's durable (survives crashes), trackable, and fast.

## Design: Restate ExtractionService with Fan-Out

```
Sync (fast, no extraction)
  │
  │  Insert 200 cases in ~5s
  │
  ▼
POST /ExtractionService/extract_batch  ──── fire-and-forget from sync
  │
  ▼
ExtractionService.extract_batch(ctx, {case_ids, max_concurrent: 10})
  │
  ├── ctx.run("filter") → filter eligible cases (have blob, no existing notes)
  │
  ├── ctx.run("batch_0") → asyncio.gather(extract_case_1, ..., extract_case_10)
  ├── ctx.run("batch_1") → asyncio.gather(extract_case_11, ..., extract_case_20)
  ├── ...                   (10 concurrent per batch, sequential between batches)
  └── ctx.run("batch_N") → last batch
  │
  ▼
Results: {extracted: 180, skipped: 15, failed: 5, duration: 45s}
```

**Why this works:**
- Each `ctx.run("batch_N")` is journaled — if worker crashes mid-batch, completed batches are skipped on retry
- `asyncio.gather()` inside each batch runs 10 extractions concurrently (Document Intelligence handles parallel requests)
- Sync returns immediately (fire-and-forget invocation) — frontend sees cases instantly
- 200 cases ÷ 10 concurrent × ~8s per case = **~160s** (vs 1600s serial)

## Files to Create/Modify

| File | Action | Purpose |
|------|--------|---------|
| `backend/app/workflow/extraction_service.py` | **Create** | Restate Service: `extract_batch` handler + `_extract_single_case` |
| `backend/restate_worker.py` | **Modify** | Register `extraction_service` |
| `backend/app/ingest/sync_engine.py` | **Modify** | Remove inline extraction, fire-and-forget to ExtractionService after insert |
| `backend/app/api/routes/settings.py` | **Modify** | Add `POST /api/settings/extract-now` endpoint for manual trigger |

## Implementation

### 1. ExtractionService (new file)

**File:** `backend/app/workflow/extraction_service.py`

```python
extraction_service = Service("ExtractionService")

@extraction_service.handler()
async def extract_batch(ctx: Context, batch_data: dict) -> dict:
    """Fan-out extraction for N cases in parallel batches."""
    case_ids = batch_data["case_ids"]
    max_concurrent = batch_data.get("max_concurrent", 10)

    # Step 1: Filter eligible (have blob key, no existing notes)
    eligible = await ctx.run("filter", _filter_eligible, args=(case_ids,))

    # Step 2: Process in batches of max_concurrent
    batches = [eligible[i:i+max_concurrent] for i in range(0, len(eligible), max_concurrent)]

    extracted = 0
    failed = []
    for i, batch in enumerate(batches):
        result = await ctx.run(f"batch_{i}", _process_batch, args=(batch,))
        extracted += result["extracted"]
        failed.extend(result["failed"])

    return {"extracted": extracted, "skipped": len(case_ids) - len(eligible), "failed": failed}
```

Inside `_process_batch`: uses `asyncio.gather()` to run N extractions concurrently. Each extraction:
1. `fetch_clinical_pdf()` — download blob (reuse existing)
2. `extract_clinical_context()` — Azure Document Intelligence OCR (reuse existing)
3. `create_clinical_note()` — store in DB (reuse existing)
4. Update case state `PENDING_NOTES → NOTES_UPLOADED`

### 2. Register in Restate Worker

**File:** `backend/restate_worker.py`

Add `extraction_service` to the `restate.app(services=[...])` list.

### 3. Sync Engine — Fire and Forget

**File:** `backend/app/ingest/sync_engine.py`

Remove the inline extraction loop (lines 108-163). After inserting all cases and committing:

```python
# Fire-and-forget: trigger Restate fan-out extraction
if extract and case_ids_with_blobs:
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{settings.RESTATE_URL}/ExtractionService/extract_batch/send",  # /send = fire-and-forget
            json={"case_ids": case_ids_with_blobs, "max_concurrent": 10},
        )
```

The `/send` suffix on Restate invocations makes it fire-and-forget — the sync endpoint returns immediately with the case counts.

### 4. Manual Extract Trigger

**File:** `backend/app/api/routes/settings.py`

```
POST /api/settings/extract-now  — triggers extraction for all PENDING_NOTES cases with blob keys
```

Queries DB for cases in `PENDING_NOTES` state that have `clinical_blob_key` or `file_key`, then fires the same Restate `extract_batch` call.

### 5. Frontend: Extract Now Button

Add to Settings page next to Sync Now — "Extract Now" button that calls `/api/settings/extract-now`. Shows extraction progress (extracted/total) when results come back.

## Key Design Decisions

- **Service, not VirtualObject** — extraction is stateless, no per-key affinity needed. Multiple extract_batch calls can run concurrently.
- **Batched gather, not individual ctx.run per case** — 200 individual `ctx.run()` calls would create 200 journal entries. Batches of 10 create only 20 journal entries. Much cleaner.
- **max_concurrent=10** — Document Intelligence S0 tier allows 15 concurrent requests. 10 leaves headroom.
- **Fire-and-forget from sync** — sync is a user-facing HTTP endpoint that must return fast. Extraction runs in background via Restate.

## Verification

1. Flush DB → sync 200 cases (extract=false) → should return in <10s
2. Fire extract_batch via API → monitor Restate logs → should process 10 at a time
3. Check DB: cases should transition PENDING_NOTES → NOTES_UPLOADED as extraction completes
4. Kill worker mid-extraction → restart → Restate replays from last completed batch (skips already-done batches)
5. Settings page: click "Extract Now" → triggers extraction for remaining PENDING_NOTES cases
