# Handoff — March 30, 2026

## Session Summary

Production launch day. Multi-worker testing, clinical scenario fix, auth cookie fix, Mongo sync clarification, and Restate stability work.

---

## 1. Multi-Worker Discovery — Restate Routing Issue

### Problem
Deployed 3 workers (worker-a, worker-b, worker-c) on separate VMs. Discovered that **Restate load-balances across all registered deployments** — it doesn't pin `WorkerSession/worker-a` to the VM at 10.0.0.5. Any deployment can handle any key.

This caused worker-b's VM to try processing worker-a's cases with the wrong browser profile/credentials, leading to login failures and "Could not find password field" errors.

### Resolution
- Shut down worker-b and worker-c processes
- Removed their Restate deployments (`dp remove --force`)
- Deactivated worker-b and worker-c in DB (`is_active = false`)
- Worker-a is the sole deployment at rev 28

### Multi-Worker Architecture TODO
Restate doesn't support per-key deployment pinning. Options to research:
1. Run all 3 workers in a single Restate process on one VM (separate browser profiles)
2. Use separate Restate service names per worker (e.g., `WorkerSessionA`, `WorkerSessionB`)
3. Custom routing layer that dispatches to worker-specific HTTP endpoints
4. Wait for Restate partition/affinity features

### VM Status
| VM | IP (Public) | IP (Private) | Status |
|----|-------------|-------------|--------|
| worker-a | 172.202.22.112 | 10.0.0.5 | ✅ Active, sole worker |
| worker-b | 74.249.202.85 | 10.0.0.6 | ⏸️ Process stopped, DB inactive |
| worker-c | 20.106.37.51 | 10.0.0.7 | ⏸️ Process stopped, DB inactive |

---

## 2. Restate Stability — Journal Mismatch Root Cause

### Problem
Cases kept getting "Disconnected. The connection to the restate server was lost" errors during batch processing.

### Root Cause
Old invocations from previous code versions were stuck in Restate's journal. When Restate retried them, the journal entries didn't match the current code → RT0016 journal mismatch → constant retry/fail cycle → overwhelmed the Restate-to-worker connection → healthy invocations got disconnected as collateral damage.

### Fix Applied
1. Killed ALL invocations (including poisoned old ones)
2. Restarted Restate server (clears cached state)
3. Re-registered worker-a fresh
4. Reset stuck PROCESSING cases back to NOTES_UPLOADED

### Rule Going Forward
After ANY code change to a Restate handler's `ctx.*` call sequence:
1. Kill ALL in-flight invocations
2. Re-register the deployment
3. THEN dispatch new work

---

## 3. Clinical Scenario (Pathway) Now in Review

### Problem
Rep feedback: clinical scenario selection STILL missing from L1 Review. The pathway selection (`GetPathwayOptions` → `SetPathway`) was auto-selected by LLM but never saved for review.

### Fix
**`clinical_flow.py`**: `select_pathway()` now returns `pathway_options` (all available scenarios) and `pathway_selected_id`.

**`portal_compiler.py`**: Two changes:
1. `clinical_pathway` phase captures pathway decision into `context_vars["_pathway_decision"]`
2. `clinical_questions` phase prepends it as **Group 0** in `all_decisions` before serialization

The rep now sees in L1 Review:
```
Group 0: "Select the clinical scenario that best matches diagnosis M79.671 for CPT 73721"
  ○ Congenital or developmental anomalies
  ○ Mass, tumor, or neoplasm
  ● ANKLE & FOOT: Tendon or ligament injury  ← AI selected
  ○ Signs and symptoms
  ○ Other diagnosis or reasons for imaging

Group 1: (first clinical question within the pathway)
  ...
```

---

## 4. Frontend Auth Cookie Fix

### Problem
After login, clicking "Start Portal Batch" and other buttons did nothing. No errors visible.

### Root Cause
`apiFetch` in `lib/api.ts` was missing `credentials: "include"`. The browser wasn't sending the `ronexa_session` httpOnly cookie with API requests.

Login worked because the login page calls the auth endpoint directly. Page navigation worked because the Next.js middleware only checks cookie existence (server-side). But client-side fetch calls to the backend API failed silently with 401.

### Fix
Added `credentials: "include"` to `apiFetch`:
```typescript
const res = await fetch(`${API_BASE}${path}`, {
  credentials: "include",  // ← added
  headers: { "Content-Type": "application/json", ...options?.headers },
  ...options,
});
```

---

## 5. Mongo Sync — No Changes Needed

### Investigation
Thought we lost 430 cases because second sync only returned 85. After investigation:
- Mongo collection genuinely has only 85 records (client clears processed records)
- The 430 from first sync are in our Postgres DB
- Our `mark_mongo_records_synced()` pattern is correct — marks as "Synced" so client can clean up
- Dedup against Postgres `exam_id` prevents duplicates

### Decision
Keep the existing Mongo sync flow as-is. No changes.

---

## 6. Batch Size Fix

### Problem
"Start Portal Batch" only dispatched 25 cases even when more were eligible.

### Root Cause
Two settings: `portal_batch_size = 25` and `max_cases_per_batch = 50`. The start-worker endpoint used `portal_batch_size`.

### Discussion
For production, dispatching all eligible cases at once is fine — Restate queues them on the WorkerSession and processes serially. With multiple workers, round-robin distributes across workers. Each worker processes its queue back-to-back with one login.

### Resolution
Updated `portal_batch_size` to match desired throughput. Single click dispatches full queue.

---

## 7. NSG Security Hardening

### Restate Ports
- Closed 9070/9071/8080 from public access initially
- Reverted: added IP-restricted rules for admin access
- 9070/9071: restricted to your IP (66.208.6.50) + VNet (10.0.0.0/24)
- 8080: VNet only (workers need internal access)

### Final NSG State
| Port | Access | Purpose |
|------|--------|---------|
| 22 | Public | SSH |
| 80 | Public | HTTP (nginx) |
| 443 | Public | HTTPS (ready for SSL) |
| 9070/9071 | Your IP + VNet | Restate admin UI |
| 8080 | VNet only | Restate ingress (workers) |

To update your IP:
```bash
az network nsg rule update --resource-group RG-RONEXA-PROD --nsg-name ronexa-orchestratorNSG \
  --name restate-admin-restricted --source-address-prefixes "NEW_IP" "10.0.0.0/24"
```

---

## Deployment State

| Component | Version | Notes |
|-----------|---------|-------|
| Backend API | v33 | Pathway decision in review |
| Frontend | v30 | credentials: include fix |
| Worker-a | rev 28 | Sole worker, 10.0.0.5 |
| Restate | 1.6.2 | Clean, single deployment |

### Batch Status (at handoff)
- Batch dispatched and running via authenticated start-worker
- 5 cases suspended from earlier batch (in L1_REVIEW — valid, not poisoned)
- New batch processing with Group 0 pathway decision included

### Active Batch
```
PENDING_NOTES:  264  (awaiting extraction)
NOTES_UPLOADED:  ~80 (ready for batch)
HOLD:            ~50 (data/portal errors)
L1_REVIEW:       ~5  (waiting for rep)
PENDING_STAT:    18  (STAT queue)
```

---

## Blocked Items
- **SSL Setup** — saved to `sslsetup.md`, blocked on missing private key
- **Multi-worker** — blocked on Restate routing architecture (see section 1)
- **RingCentral Fax** — code written, not yet tested end-to-end
- **Mongo writeback** — post-submission status update to RIS (template defined, not wired)
