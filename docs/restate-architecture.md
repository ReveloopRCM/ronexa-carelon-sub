# Ronexa Carelon Submission Engine — Restate Architecture Design

## Core Constraint

The Carelon provider portal (ASP.NET WebForms) allows **one active browser session per login**. All portal operations for a given login must be serial. Scale comes from multiple logins, not parallelism within a login.

## Core Technology: Restate Durable Execution

Every case processed through the portal has its own **durable journal**. The journal records completed steps. If the system crashes, restarts, or a human pauses the workflow, the journal replays completed steps and resumes exactly where it left off. Side effects wrapped in `ctx.run()` execute once and return cached results on replay.

## Design Principles

1. **One Restate invocation per case** — each case has its own journal, its own state, its own awakeable for human-in-the-loop
2. **Virtual Object per worker = serial task queue** — exclusive handler guarantee means cases execute one at a time per worker, matching the portal's serial constraint
3. **Browser lifecycle owned by the worker VO** — login, session reuse, timeout detection all managed at the worker level, not the case level
4. **Human-in-the-loop via awakeables** — review pauses cost zero resources. The invocation suspends, no thread blocked, no browser open. Resumes when the human acts.
5. **Future extensibility** — any new pause point (medical director review, STAT clinical call, RPO manual entry) is just another awakeable in the case's journal

---

## Service Topology

### 1. BatchDispatcher (Restate Service — stateless)

**Purpose:** Receives "Start Portal Batch" trigger. Queries the queue for eligible cases. Dispatches each case as an event to the appropriate WorkerSession VO.

**Why a Service (not VO):** Stateless. Multiple dispatchers can run concurrently. No per-key state needed.

```
Handler: dispatch_batch(config)
  1. Query DB: SELECT cases WHERE state=NOTES_UPLOADED ORDER BY priority DESC LIMIT batch_size
  2. For each case:
     ctx.object_send(WorkerSession, key="worker-a", handler="process_case", arg={case_id, case_data})
  3. Return {dispatched: N}
```

All sends are fire-and-forget. The dispatcher returns immediately. Cases queue up in the WorkerSession's exclusive handler queue and execute serially.

### 2. WorkerSession (Restate Virtual Object — keyed by worker_id)

**Purpose:** Owns the browser session for a Carelon login. Processes cases serially through its exclusive handler queue. Manages login lifecycle, session timeout detection, and navigation between cases.

**Key:** `worker_id` (e.g., "worker-a", "worker-b") — one VO per Carelon login account

**State:**
- `logged_in: bool` — is the browser currently authenticated
- `browser_pid: int` — process ID of the Chromium instance
- `cases_processed: int` — count for this session
- `last_activity: datetime` — for session timeout detection

**Exclusive Handler: process_case(case_event)**

This is the core execution path. Because it's an exclusive handler on the WorkerSession VO, Restate guarantees only one runs at a time per worker_id. Cases queue automatically.

```
process_case(ctx: ObjectContext, case_event: dict):
    case_id = case_event["case_id"]
    case_data = case_event["case_data"]

    # Step 1: Ensure browser is logged in
    # On first call: launches browser, logs into Carelon (MFA etc.)
    # On subsequent calls: verifies session is alive
    # On session timeout: re-logs in
    browser_ok = await ctx.run("ensure_login", _ensure_browser_login)

    # Step 2: Create per-case awakeable for human review
    review_awakeable_id, review_promise = ctx.awakeable()

    # Step 3: Run the portal compiler (member search → eligibility → clinical → questions)
    # This is NOT in ctx.run() — browser state can't be journaled
    # If it fails, Restate retries from Step 1 (re-login if needed)
    try:
        result = await _run_portal_first_pass(case_data, clinical_context)
    except PortalSessionExpired:
        # Session died mid-case — clear login state, Restate will retry
        ctx.clear("logged_in")
        raise  # Restate retries → Step 1 re-logs in
    except Exception as e:
        # Case-level error — mark exception, don't kill the worker
        await ctx.run("mark_exception", _mark_exception, args=(case_id, str(e)))
        return {"status": "error", "case_id": case_id}

    # Step 4: Handle result
    if result.get("case_state") == "HOLD":
        await ctx.run("mark_hold", _mark_hold, args=(case_id, result))
        return {"status": "hold", "case_id": case_id}

    if result.get("answers"):
        # Save questions + awakeable ID to DB
        await ctx.run("save_questions", _save_questions,
            args=(case_id, result["answers"], review_awakeable_id))

        # Navigate back to homepage for next case
        await _navigate_to_homepage()

        # SUSPEND — wait for human review
        # Zero cost. No browser held. No thread blocked.
        # Next case in the queue will trigger a NEW process_case invocation
        # which will re-login (browser was released).
        rep_response = await review_promise

        # RESUMED — human approved/edited
        # Step 5: Re-login (browser was closed during suspension)
        await ctx.run("ensure_login_resume", _ensure_browser_login)

        # Step 6: Run finalize pass (replay portal with approved answers)
        if rep_response.get("action") == "edited":
            # Backtrack: replay to changed question, get new pathway
            finalize_result = await _run_portal_replay(case_data, rep_response)
            if finalize_result.get("answers"):
                # New questions from changed pathway — suspend again
                new_awakeable_id, new_promise = ctx.awakeable()
                await ctx.run("save_questions_r2", _save_questions,
                    args=(case_id, finalize_result["answers"], new_awakeable_id, 2))
                await _navigate_to_homepage()
                rep_response_2 = await new_promise
                # ... continue until all approved
        else:
            # All approved — fast-forward and submit
            finalize_result = await _run_portal_submit(case_data, rep_response["answers"])

        await ctx.run("mark_complete", _mark_complete, args=(case_id, finalize_result))
        await _navigate_to_homepage()
        return {"status": "submitted", "case_id": case_id}
```

**Critical behavior:** When `process_case` suspends on `await review_promise`:
- The WorkerSession's exclusive handler queue is **blocked** for this key
- BUT — this is intentional. This worker can't process more cases while one is in review (the browser session context is tied to the suspended case's portal state)
- When the rep approves, the handler resumes, re-logs in, and completes

**Wait — this blocks the queue.** If 25 cases are dispatched and case 1 suspends for review, cases 2-25 are stuck waiting. That's a problem.

### The Queue Blocking Problem

When `process_case` suspends on an awakeable, the exclusive handler holds its position. No other `process_case` invocation can run on this worker until the suspended one completes.

**Solution: Split into two handlers.**

```
Exclusive Handler: run_first_pass(case_event)
  - Ensure login
  - Run portal compiler
  - Save questions
  - Navigate home
  - Return (does NOT suspend)
  - Next case in queue starts immediately

Exclusive Handler: run_finalize(case_event)
  - Called by FinalizeService after approval
  - Ensure login
  - Run portal with approved answers
  - Submit
  - Navigate home
  - Return
```

No awakeables in the WorkerSession handlers. They always return. The queue never blocks.

**Where does the awakeable live?** In a separate CaseWorkflow (Restate Workflow, keyed by case_id):

### 3. CaseWorkflow (Restate Workflow — keyed by case_id)

**Purpose:** Manages the lifecycle of a single case. Owns the awakeable for human review. Coordinates between first pass and finalize.

**Key:** `case_id` — one workflow per case

```
@case_workflow.main()
async def run(ctx: WorkflowContext, case_data: dict):
    case_id = ctx.key()
    worker_id = case_data["worker_id"]

    # Step 1: Dispatch first pass to the worker
    first_pass_result = await ctx.object_call(
        WorkerSession, key=worker_id,
        handler="run_first_pass",
        arg={"case_id": case_id, "case_data": case_data}
    )
    # ctx.object_call waits for run_first_pass to return
    # run_first_pass does NOT suspend — it returns after saving questions

    if first_pass_result["status"] in ("hold", "error"):
        return first_pass_result

    if first_pass_result["status"] == "review":
        # Step 2: Wait for human review
        review_awakeable_id, review_promise = ctx.awakeable()

        # Store awakeable ID in DB so frontend can resolve it
        await ctx.run("save_awakeable", _save_awakeable_id,
            args=(case_id, review_awakeable_id))

        # SUSPEND — wait for rep
        # CaseWorkflow suspends. WorkerSession is FREE to process other cases.
        rep_response = await review_promise

        # RESUMED
        if rep_response.get("action") == "edited":
            # Dispatch replay to worker (may need re-login)
            replay_result = await ctx.object_call(
                WorkerSession, key=worker_id,
                handler="run_replay",
                arg={"case_id": case_id, "case_data": case_data, "edits": rep_response}
            )
            if replay_result["status"] == "review":
                # New questions — suspend again (round 2)
                # ... recursive review cycle ...
                pass
        else:
            # All approved — dispatch finalize to worker
            finalize_result = await ctx.object_call(
                WorkerSession, key=worker_id,
                handler="run_finalize",
                arg={"case_id": case_id, "case_data": case_data, "approved_answers": rep_response["answers"]}
            )

        return finalize_result

    return first_pass_result
```

**Why this works:**
- `CaseWorkflow.run` suspends on the awakeable — but it's keyed by `case_id`, not `worker_id`. It doesn't block the WorkerSession queue.
- `WorkerSession.run_first_pass` returns immediately after portal work. The exclusive handler queue advances to the next case.
- `WorkerSession.run_finalize` is called later (after approval) as a new exclusive handler invocation. It queues behind any currently-running case on that worker.
- The browser session persists in WorkerSession's in-process state between `run_first_pass` calls. If the session expires between cases, `_ensure_browser_login` detects it and re-logs in.

### 4. FinalizeService (Restate Service — stateless)

**Purpose:** Triggered by "Submit Batch" button. Queries approved cases and resolves their awakeables. CaseWorkflow resumes, which dispatches `run_finalize` to the appropriate WorkerSession.

```
Handler: submit_batch(config)
  1. Query DB: SELECT cases WHERE state=APPROVED_FOR_SUBMIT
  2. For each case:
     awakeable_id = case.awakeable_id (stored in DB)
     ctx.resolve_awakeable(awakeable_id, {"action": "approved", "answers": case.approved_answers})
  3. Return {resolved: N}
```

FinalizeService doesn't need to log in or touch the browser. It just resolves awakeables. CaseWorkflow handles the rest.

### 5. ExtractionService (Restate Service — stateless)

**Purpose:** Fan-out OCR extraction. Unchanged from current implementation.

```
Handler: extract_batch(case_ids, max_concurrent)
  Parallel extraction via restate.gather()
```

---

## Flow Diagrams

### First Pass Batch (Start Portal Batch)

```
Frontend: "Start Portal Batch"
    │
    ▼
BatchDispatcher.dispatch_batch()
    │
    ├── Query: 25 NOTES_UPLOADED cases by priority
    │
    ├── For each case:
    │   ctx.object_send(CaseWorkflow, key=case_id, "run", {worker_id: "worker-a", ...})
    │   (fire-and-forget, returns immediately)
    │
    └── Return {dispatched: 25}

CaseWorkflow/case-1.run():
    │
    ├── await ctx.object_call(WorkerSession/"worker-a", "run_first_pass", {case-1 data})
    │   │
    │   WorkerSession/"worker-a".run_first_pass():  ← EXCLUSIVE, serial
    │   │   ├── ctx.run("ensure_login") → login if needed
    │   │   ├── Run compiler: member → eligibility → DI → auths → provider → clinical
    │   │   ├── ctx.run("save_questions") → DB
    │   │   ├── Navigate to homepage
    │   │   └── Return {"status": "review"}
    │   │
    │   Result returned to CaseWorkflow
    │
    ├── Create review awakeable
    ├── ctx.run("save_awakeable") → store awakeable_id in DB
    ├── await review_promise ← SUSPENDS (zero cost)
    │   │
    │   WorkerSession/"worker-a" is now FREE
    │   │
    │   CaseWorkflow/case-2.run() starts:
    │   └── await ctx.object_call(WorkerSession/"worker-a", "run_first_pass", {case-2 data})
    │       WorkerSession processes case-2... then case-3... etc.
    │
    │   ... hours later, rep approves case-1 ...
    │
    ├── RESUMED
    ├── await ctx.object_call(WorkerSession/"worker-a", "run_finalize", {approved answers})
    │   │
    │   WorkerSession/"worker-a".run_finalize():  ← queues behind any active case
    │   │   ├── ctx.run("ensure_login") → re-login (session expired during review)
    │   │   ├── Run compiler with approved answers → fast-forward → submit
    │   │   ├── Navigate to homepage
    │   │   └── Return {"status": "submitted", "auth_number": "..."}
    │   │
    └── Return result
```

### Edited Case Replay

```
Rep edits answer on Q3 → L2 approves with edits → state = EDITED_FOR_REPLAY

FinalizeService.submit_batch():
    │
    ├── case has edits → resolve awakeable with {"action": "edited", "changed_group_id": 3}
    │
    CaseWorkflow/case-7 RESUMES:
    │
    ├── await ctx.object_call(WorkerSession/"worker-a", "run_replay", {edits})
    │   │
    │   WorkerSession.run_replay():
    │   │   ├── Login
    │   │   ├── Run compiler → replay to Q3 → use new answer
    │   │   ├── Portal re-evaluates → new Q4, Q5, Q6
    │   │   ├── LLM answers new questions
    │   │   ├── Save new questions → state = L1_REVIEW
    │   │   ├── Navigate home
    │   │   └── Return {"status": "review", "answers": [...]}
    │   │
    ├── New questions need review → create new awakeable → SUSPEND again
    │
    │   ... rep reviews new questions → all approved ...
    │
    ├── RESUMED
    ├── await ctx.object_call(WorkerSession/"worker-a", "run_finalize", {all answers})
    └── Submitted
```

### Scaling to Multiple Workers

```
BatchDispatcher.dispatch_batch():
    │
    ├── Cases 1-25:  ctx.object_send(CaseWorkflow, key=case_id, {worker_id: "worker-a"})
    ├── Cases 26-50: ctx.object_send(CaseWorkflow, key=case_id, {worker_id: "worker-b"})
    ├── Cases 51-75: ctx.object_send(CaseWorkflow, key=case_id, {worker_id: "worker-c"})
    │
    WorkerSession/"worker-a" processes 1-25 serially (VM 1, Login A)
    WorkerSession/"worker-b" processes 26-50 serially (VM 2, Login B)
    WorkerSession/"worker-c" processes 51-75 serially (VM 3, Login C)

    75 cases, 3 workers, ~5 min/case = ~2 hours total
```

---

## State Management

| State Owner | What it stores | Persistence |
|-------------|---------------|-------------|
| WorkerSession VO state | `logged_in`, `last_activity` | Restate (per worker_id) |
| WorkerSession in-process | Browser instance, page object | Python dict (volatile, lost on crash → re-login) |
| CaseWorkflow journal | Completed ctx.run() results, awakeable state | Restate (per case_id) |
| PostgreSQL | Case data, questions, answers, audit trail | Persistent |
| Restate journal | All ctx.run() results, awakeable resolutions | Persistent (survives crash) |

---

## Error Handling

| Error | What happens |
|-------|-------------|
| Portal session timeout | WorkerSession detects → clears `logged_in` state → next `ensure_login` re-logs in |
| Portal "Page Cannot be Displayed" | WorkerSession detects system error → closes browser → clears state → re-login on next case |
| Member not found | `run_first_pass` returns `{status: "hold"}` → CaseWorkflow marks exception → no awakeable created |
| Gemini API error | Compiler catches → fallback answer → continues (or HOLD if all providers fail) |
| Worker VM crash | Restate detects handler disconnected → retries on reconnect → journal replays → re-login → resumes |
| Restate server crash | Journal persisted to disk → on restart, all in-flight invocations resume from journal |
| Rep never approves | Awakeable stays open indefinitely (zero cost) → case sits in L1_REVIEW until acted on |

---

## Future Extensibility

| Future Feature | How it fits |
|----------------|-----------|
| Medical director review | Add second awakeable in CaseWorkflow after clinical questions |
| STAT clinical call | Awakeable after submission → rep logs call outcome → workflow continues |
| Auto-bypass (high confidence) | CaseWorkflow skips awakeable if all answers > threshold AND bypass rule exists |
| Multi-exam bundling | CaseWorkflow orchestrates multiple run_first_pass calls for related exams |
| RAG feedback loop | After submission outcome received, CaseWorkflow writes to pgvector index |
| Provider follow-up (Day 2) | Delayed send: `ctx.object_send(CaseWorkflow, delay=timedelta(hours=24))` |
