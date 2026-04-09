"""Finalize pass test — simulates the SECOND pass after rep approval.

This tests the EXACT code path that fails in production:
  1. Fresh browser login (browser was closed during review period)
  2. Replay all WebForms phases from scratch
  3. Clinical init/setup/diagnosis/pathway
  4. FAST-FORWARD questions with pre-built answers (not LLM — simulates resume)
  5. Finalize → hdnAction=20 → exam summary → hdnAction=6 → facility search

The key difference from first pass: questions are submitted ALL AT ONCE
(fast-forward) instead of answered one-by-one via LLM loop.

Run:
    cd backend && python -m tests.test_finalize_pass 17189819

Uses the same case as test_full_submission but takes a shortcut on questions.
"""
import asyncio
import logging
import sys
import os
import time
import json

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("test_finalize_pass")

STEP_LOG = []


def log_step(step_num: str, name: str, result: dict, elapsed_ms: int = 0):
    entry = {
        "step": step_num,
        "name": name,
        "ok": result.get("ok", True),
        "message": result.get("message", ""),
        "elapsed_ms": elapsed_ms,
        "timestamp": time.strftime("%H:%M:%S"),
    }
    STEP_LOG.append(entry)
    status = "✓" if entry["ok"] else "✗"
    logger.info(f"  [{status}] Step {step_num}: {name} ({elapsed_ms}ms)")


async def diagnose_page(page, label: str) -> dict:
    try:
        diag = await page.evaluate("""() => ({
            url: window.location.href,
            title: document.title,
            bodyText: document.body.innerText.substring(0, 500),
            hasViewState: !!document.querySelector('[name="__VIEWSTATE"]'),
            hasHdnAction: !!document.getElementById('asPrimary_ctl00_hdnAction'),
            hasFacilitySearch: !!document.querySelector('[id*="lbProviderSearchAdvanced"]'),
            hasSubmitBtn: !!document.querySelector('#asPrimary_ctl00_cmdSubmitRequest'),
            hasErrorPage: document.body.innerText.includes('Cannot be Displayed') ||
                          document.body.innerText.includes('Temporarily Unavailable'),
        })""")
        logger.info(f"  🔍 [{label}] URL: {diag.get('url')}")
        if diag.get("hasErrorPage"):
            logger.error(f"  ⚠️  ERROR PAGE: {diag.get('bodyText', '')[:200]}")
        return diag
    except Exception as e:
        logger.warning(f"  Diagnostics failed: {e}")
        return {}


async def run_test():
    from app.core.settings import settings

    logger.info("=" * 70)
    logger.info("FINALIZE PASS TEST — Simulates second pass after rep approval")
    logger.info("=" * 70)

    # ── Load test case ──
    MEMBER_ID = None
    for arg in sys.argv[1:]:
        if not arg.startswith("-"):
            MEMBER_ID = arg
            break

    test_case = None
    clinical_context = {}

    try:
        from app.db.database import async_session_factory
        from app.db import repositories as repo
        async with async_session_factory() as db:
            all_cases = await repo.list_cases(db, limit=200)
            target = None
            if MEMBER_ID:
                for c in all_cases:
                    if c.policy_num == MEMBER_ID or c.exam_id == MEMBER_ID or c.id == MEMBER_ID:
                        target = c
                        break

            if target:
                c = target
                test_case = {
                    "id": c.id,
                    "first_name": c.first_name,
                    "last_name": c.last_name,
                    "dob": c.dob,
                    "policy_num": c.policy_num,
                    "patient_zip": c.patient_zip,
                    "referring_npi": c.referring_npi,
                    "center_npi": c.center_npi,
                    "center_abbr": c.center_abbr,
                    "cpt_code": c.cpt_code,
                    "icd1": c.icd1,
                    "icd2": c.icd2,
                    "icd3": c.icd3,
                    "carrier_id": c.carrier_id,
                    "referring_fax": c.referring_fax,
                    "is_stat": c.is_stat,
                    "scheduled_dt": str(c.scheduled_dt) if c.scheduled_dt else None,
                    "raw_data": c.raw_data or {},
                }
                logger.info(f"Case: {c.first_name} {c.last_name}")
                logger.info(f"  MemberID={c.policy_num}, CPT={c.cpt_code}, ICD={c.icd1}")
    except Exception as e:
        logger.error(f"Failed to load case: {e}")
        return

    if not test_case:
        logger.error("No test case found")
        return

    # ── Launch browser (VISIBLE) ──
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False, slow_mo=100)
    context = await browser.new_context(
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="America/Chicago",
    )
    page = await context.new_page()

    from app.portal.behavior_engine import BehaviorEngine
    from app.portal.session import PlaywrightPortalSession
    from app.portal.webforms_client import WebFormsClient
    from app.portal.clinical_flow import ClinicalExamFlow

    behavior = BehaviorEngine(page)

    # ================================================================
    # STEP 1: LOGIN
    # ================================================================
    logger.info("\n--- STEP 1: LOGIN ---")
    t0 = time.time()
    from app.auth.okta_login import okta_login
    try:
        provider_id, client_id = await okta_login(page, behavior)
        log_step("1", "Login", {"ok": True}, int((time.time() - t0) * 1000))
    except Exception as e:
        log_step("1", "Login", {"ok": False, "message": str(e)})
        await browser.close(); await pw.stop(); return

    session = PlaywrightPortalSession(
        context=context, page=page,
        center_npi=test_case["center_npi"],
        provider_id=provider_id, client_id=client_id,
    )
    wf = WebFormsClient(session)
    clinical = ClinicalExamFlow(session)

    # ================================================================
    # STEPS 2-8: WEBFORMS (same as first pass — portal requires replay)
    # ================================================================
    logger.info("\n--- STEP 2: TERMS ---")
    t0 = time.time()
    r = await wf.agree_to_terms()
    log_step("2", "Terms", r, int((time.time() - t0) * 1000))

    logger.info("\n--- STEP 3: MEMBER SEARCH ---")
    t0 = time.time()
    dob = test_case["dob"]
    if hasattr(dob, "strftime"):
        dob = dob.strftime("%m/%d/%Y")
    r = await wf.search_member(
        first_name=test_case["first_name"],
        last_name=test_case["last_name"],
        dob=str(dob),
        policy_num=test_case["policy_num"],
    )
    log_step("3", "Member search", r, int((time.time() - t0) * 1000))
    if not r["ok"]:
        await browser.close(); await pw.stop(); return

    logger.info("\n--- STEP 4: ELIGIBILITY ---")
    t0 = time.time()
    r = await wf.extract_eligibility_details()
    log_step("4", "Eligibility", r, int((time.time() - t0) * 1000))

    logger.info("\n--- STEP 5: SELECT DI ---")
    t0 = time.time()
    r = await wf.select_diagnostic_imaging()
    log_step("5", "Select DI", r, int((time.time() - t0) * 1000))
    if not r["ok"]:
        await browser.close(); await pw.stop(); return

    await asyncio.sleep(2)
    phone_result = await wf.extract_patient_phone()

    logger.info("\n--- STEP 6: START ORDER ---")
    t0 = time.time()
    r = await wf.start_order_request()
    log_step("6", "Start order", r, int((time.time() - t0) * 1000))
    if not r["ok"]:
        await browser.close(); await pw.stop(); return

    logger.info("\n--- STEP 7: EXISTING AUTHS ---")
    t0 = time.time()
    r = await wf.extract_existing_auths()
    log_step("7a", "Extract auths", r, int((time.time() - t0) * 1000))
    t0 = time.time()
    r = await wf.click_next_after_auths()
    log_step("7b", "Click Next", r, int((time.time() - t0) * 1000))

    logger.info("\n--- STEP 8: PROVIDER SEARCH ---")
    t0 = time.time()
    r = await wf.search_provider(
        referring_npi=test_case["referring_npi"],
        state="TX",
        fax=test_case.get("referring_fax", ""),
        match_address=test_case.get("referring_address", ""),
    )
    log_step("8", "Provider search", r, int((time.time() - t0) * 1000))
    if not r["ok"]:
        await browser.close(); await pw.stop(); return

    # ================================================================
    # STEPS 9-12: CLINICAL SETUP (same as first pass)
    # ================================================================
    logger.info("\n--- STEP 9: CLINICAL INIT ---")
    t0 = time.time()
    r = await clinical.initialize()
    log_step("9", "Clinical init", r, int((time.time() - t0) * 1000))
    if not r["ok"]:
        await browser.close(); await pw.stop(); return

    logger.info("\n--- STEP 10: EXAM SETUP ---")
    t0 = time.time()
    r = await clinical.setup_exam(cpt_code=test_case["cpt_code"])
    log_step("10", f"Exam setup CPT={test_case['cpt_code']}", r, int((time.time() - t0) * 1000))
    if not r["ok"]:
        await browser.close(); await pw.stop(); return

    logger.info("\n--- STEP 11: DIAGNOSIS ---")
    t0 = time.time()
    r = await clinical.enter_diagnosis(test_case["icd1"])
    log_step("11", f"Diagnosis ICD={test_case['icd1']}", r, int((time.time() - t0) * 1000))
    if not r["ok"]:
        await browser.close(); await pw.stop(); return

    logger.info("\n--- STEP 12: PATHWAY ---")
    t0 = time.time()
    r = await clinical.select_pathway(preferred_icd=test_case["icd1"])
    log_step("12", "Pathway", r, int((time.time() - t0) * 1000))
    if not r["ok"]:
        await browser.close(); await pw.stop(); return

    # ================================================================
    # STEP 13: FAST-FORWARD QUESTIONS (THE KEY DIFFERENCE!)
    # Instead of LLM answering one-by-one, we:
    #   a. Get the first batch of questions
    #   b. Pick the FIRST option for each (simulating approved answers)
    #   c. Submit all at once
    #   d. Repeat until done
    # This mirrors what the compiler does on the resume/finalize path.
    # ================================================================
    logger.info("\n" + "=" * 60)
    logger.info("STEP 13: FAST-FORWARD QUESTIONS (finalize simulation)")
    logger.info("=" * 60)
    t0 = time.time()

    total_answers = 0
    rounds = 0
    max_rounds = 20

    while rounds < max_rounds:
        rounds += 1
        q_result = await clinical.get_questions()
        if not q_result["ok"]:
            logger.error(f"Get questions failed: {q_result['message']}")
            break

        questions = q_result["data"].get("questions", [])
        done = q_result["data"].get("done", False)

        if not questions or done:
            logger.info(f"Questions done after {rounds} round(s), {total_answers} total answers")
            break

        # Build answers — pick last option for each (deterministic, like approved answers)
        # "None of these apply" is usually last — leads to clinical review (safe)
        answers_batch = []
        for q in questions:
            options = q.get("Options", [])
            if not options:
                # Text/date question — use a dummy value
                val = q.get("default_value", "N/A")
                answers_batch.append({
                    "QuestionId": q["Id"],
                    "QuestionType": q.get("QuestionType", 3),
                    "GroupId": q.get("GroupId", 1),
                    "Sequence": q.get("Sequence", 0),
                    "Values": [val],
                })
            else:
                # Single/multi choice — pick last option ("None of these apply")
                selected = options[-1]
                answers_batch.append({
                    "QuestionId": q["Id"],
                    "QuestionType": q.get("QuestionType", 3),
                    "GroupId": q.get("GroupId", 1),
                    "Sequence": q.get("Sequence", 0),
                    "Values": [selected["Id"]],
                })

        logger.info(f"  Round {rounds}: answering {len(answers_batch)} questions (fast-forward)")
        for a in answers_batch:
            logger.info(f"    GroupId={a['GroupId']} QID={str(a['QuestionId'])[:8]}... → Values={a['Values']}")

        r = await clinical.answer_questions(answers_batch)
        total_answers += len(answers_batch)

        if not r["ok"]:
            logger.error(f"Answer submission failed: {r['message']}")
            break

        remaining = r["data"].get("questions", [])
        done = r["data"].get("done", False)
        if done or not remaining:
            logger.info(f"Portal at done state after round {rounds}")
            break

    log_step("13", f"Fast-forward questions ({total_answers} answers)", {"ok": True}, int((time.time() - t0) * 1000))

    # ================================================================
    # STEP 14: FINALIZE (ProcessAccepted → AddRadioTracers)
    # This is where the production flow diverges — same API calls
    # but questions were fast-forwarded instead of LLM-answered
    # ================================================================
    logger.info("\n" + "=" * 60)
    logger.info("STEP 14: FINALIZE (determination)")
    logger.info("=" * 60)
    await diagnose_page(page, "before_finalize")

    t0 = time.time()
    patient_phone = phone_result["data"].get("phone", "") if phone_result and phone_result.get("ok") else ""
    r = await clinical.finalize(
        first_name=test_case["first_name"],
        last_name=test_case["last_name"],
        phone=patient_phone,
        fax=test_case.get("referring_fax", ""),
    )
    log_step("14", "Finalize", r, int((time.time() - t0) * 1000))
    if not r["ok"]:
        logger.error(f"FINALIZE FAILED: {r['message']}")
        await diagnose_page(page, "finalize_failed")
        await asyncio.sleep(300)
        await browser.close(); await pw.stop(); return

    logger.info(f"  Auto approved: {r['data'].get('auto_approved')}")
    await asyncio.sleep(2)

    # ================================================================
    # STEP 15: hdnAction=20 → EXAM SUMMARY
    # ================================================================
    logger.info("\n" + "=" * 60)
    logger.info("STEP 15: hdnAction=20 → EXAM SUMMARY")
    logger.info("=" * 60)
    await diagnose_page(page, "before_hdnaction20")

    t0 = time.time()
    r = await wf.postback_hdnaction(20, timeout_ms=30000)
    log_step("15", "hdnAction=20", r, int((time.time() - t0) * 1000))

    await asyncio.sleep(3)
    diag = await diagnose_page(page, "after_hdnaction20")
    if diag.get("hasErrorPage"):
        logger.error("⚠️  ERROR after hdnAction=20!")
        await asyncio.sleep(300)
        await browser.close(); await pw.stop(); return

    # ================================================================
    # STEP 16: EXAM SUMMARY REVIEW
    # ================================================================
    logger.info("\n" + "=" * 60)
    logger.info("STEP 16: EXAM SUMMARY REVIEW")
    logger.info("=" * 60)

    t0 = time.time()
    cpt_group = clinical._find_cpt_group_id(test_case["cpt_code"])
    r = await clinical.exam_summary_review(
        cpt_code=test_case["cpt_code"],
        cpt_group=cpt_group,
    )
    log_step("16", "Exam summary review", r, int((time.time() - t0) * 1000))
    if not r["ok"]:
        logger.error(f"EXAM SUMMARY REVIEW FAILED: {r['message']}")
        await diagnose_page(page, "exam_review_failed")
        await asyncio.sleep(300)
        await browser.close(); await pw.stop(); return

    await asyncio.sleep(2)

    # ================================================================
    # STEP 17: hdnAction=6 → FACILITY SEARCH  ⚠️  THE CRITICAL STEP
    # ================================================================
    logger.info("\n" + "=" * 60)
    logger.info("STEP 17: hdnAction=6 → FACILITY SEARCH  ⚠️  CRITICAL")
    logger.info("=" * 60)
    await diagnose_page(page, "before_hdnaction6")

    t0 = time.time()
    r = await wf.postback_hdnaction(6, timeout_ms=30000)
    log_step("17", "hdnAction=6", r, int((time.time() - t0) * 1000))

    await asyncio.sleep(3)
    diag = await diagnose_page(page, "after_hdnaction6")

    if diag.get("hasErrorPage"):
        logger.error("=" * 60)
        logger.error("⚠️  FACILITY SEARCH ERROR — PRODUCTION BUG REPRODUCED!")
        logger.error("=" * 60)
        await asyncio.sleep(300)
        await browser.close(); await pw.stop(); return

    # ================================================================
    # STEP 18: FACILITY SEARCH
    # ================================================================
    logger.info("\n--- STEP 18: FACILITY SEARCH ---")
    t0 = time.time()
    r = await wf.search_facility(center_npi=test_case["center_npi"], fax=test_case.get("referring_fax", ""))
    log_step("18", f"Facility search NPI={test_case['center_npi']}", r, int((time.time() - t0) * 1000))
    if not r["ok"]:
        logger.error(f"FACILITY SEARCH FAILED: {r['message']}")
        await asyncio.sleep(300)
        await browser.close(); await pw.stop(); return

    # ================================================================
    # STEP 19: ORDER PREVIEW
    # ================================================================
    logger.info("\n" + "=" * 60)
    logger.info("STEP 19: ORDER PREVIEW")
    logger.info("=" * 60)
    diag = await diagnose_page(page, "order_preview")

    # ================================================================
    # FINAL REPORT
    # ================================================================
    logger.info("\n" + "=" * 60)
    logger.info("FINALIZE PASS RESULTS")
    logger.info("=" * 60)
    total_ms = 0
    for entry in STEP_LOG:
        status = "✓" if entry["ok"] else "✗"
        logger.info(
            f"  [{status}] {entry['step']:5s} {entry['name']:40s} "
            f"{entry['elapsed_ms']:6d}ms  {entry['timestamp']}"
        )
        total_ms += entry["elapsed_ms"]
    logger.info(f"\n  Total time: {total_ms / 1000:.1f}s")
    logger.info(f"  Steps passed: {sum(1 for e in STEP_LOG if e['ok'])}/{len(STEP_LOG)}")

    if diag.get("hasSubmitBtn"):
        logger.info("\n  ✅ SUCCESS — Submit This Request button is visible!")
        logger.info("  The finalize pass works correctly.")
    else:
        logger.warning("\n  ⚠️  Submit button not found — check page state")

    logger.info("\nBrowser stays open 2 min for inspection...")
    await asyncio.sleep(120)
    await browser.close()
    await pw.stop()
    logger.info("Done.")


if __name__ == "__main__":
    asyncio.run(run_test())
