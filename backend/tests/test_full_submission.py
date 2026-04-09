"""Full submission test — login through Submit This Request.

Run with browser visible so you can watch every step:
    cd backend && python -m tests.test_full_submission

This script:
  1. Loads a real case from the DB
  2. Walks through every SOP step with pause points
  3. Goes ALL THE WAY to Submit (unless --dry-run flag)
  4. Captures screenshots + detailed diagnostics at each transition
  5. Designed for debugging the hdnAction=20→6 facility search failure

HAR-PROVEN sequence (3 HARs analyzed — Procter, Tamayo, Pending):
  Phase A (clinical SPA):
    ProcessAccepted → IsExamAutoApproved → [IsExamApprovedCDO] →
    [AddFeedback] → AddRadioTracers → hdnAction=20
  Phase B (exam summary page):
    GetClientMessages → GetCase → EnableEditForRadioTracerCodes →
    ShouldWithdrawButtonDisplayed → [GetCptCodeTable] →
    GetProcedureSubstitutions → CheckIfAdditionalDocRequired×3 →
    DoneWithExam → FindNextExam → ShouldWithdrawButtonDisplayed →
    hdnAction=6
  Phase C (facility search page):
    Advanced search → NPI → Select → Fax → Continue
  Phase D (order preview):
    Submit This Request
"""
import asyncio
import logging
import sys
import os
import time
import json

# Setup logging — verbose for debugging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("test_full_submission")

# Track every step for the final report
STEP_LOG = []


def log_step(step_num: str, name: str, result: dict, elapsed_ms: int = 0):
    """Record a step result for the final report."""
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


async def pause(page, label: str, seconds: int = 3):
    """Pause with screenshot for visual inspection."""
    safe_label = label.replace(" ", "_").replace("/", "_")[:40]
    path = f"/tmp/ronexa_test_{safe_label}.png"
    try:
        await page.screenshot(path=path)
        logger.info(f"  📸 Screenshot: {path}")
    except Exception:
        pass
    await asyncio.sleep(seconds)


async def diagnose_page(page, label: str) -> dict:
    """Capture comprehensive page diagnostics."""
    try:
        diag = await page.evaluate("""() => ({
            url: window.location.href,
            title: document.title,
            bodyText: document.body.innerText.substring(0, 500),
            hasViewState: !!document.querySelector('[name="__VIEWSTATE"]'),
            hasHdnAction: !!document.getElementById('asPrimary_ctl00_hdnAction'),
            hasCmdNext: !!document.getElementById('asPrimary_ctl00_cmdNext'),
            hasCmdContinue: !!document.getElementById('asPrimary_ctl00_cmdContinue') ||
                            !!document.getElementById('cmdContinue'),
            hasFacilitySearch: !!document.querySelector('[id*="lbProviderSearchAdvanced"]'),
            hasSubmitBtn: !!document.querySelector('#asPrimary_ctl00_cmdSubmitRequest') ||
                          !!document.querySelector('input[value="Submit This Request"]'),
            hasErrorPage: document.body.innerText.includes('Cannot be Displayed') ||
                          document.body.innerText.includes('Temporarily Unavailable') ||
                          document.body.innerText.includes('Our apologies'),
            formCount: document.forms.length,
            formAction: document.forms[0] ? document.forms[0].action : 'none',
            hdnActionValue: (() => {
                const h = document.getElementById('asPrimary_ctl00_hdnAction');
                return h ? h.value : 'not found';
            })(),
            allButtons: Array.from(document.querySelectorAll('input[type="submit"], input[type="button"], button'))
                .slice(0, 10).map(b => ({id: b.id, value: b.value, type: b.type, visible: b.offsetParent !== null})),
        })""")
        logger.info(f"  🔍 Page diagnostics [{label}]:")
        logger.info(f"     URL: {diag.get('url')}")
        logger.info(f"     Title: {diag.get('title')}")
        logger.info(f"     ViewState: {diag.get('hasViewState')}, hdnAction: {diag.get('hasHdnAction')} (val={diag.get('hdnActionValue')})")
        logger.info(f"     cmdNext: {diag.get('hasCmdNext')}, cmdContinue: {diag.get('hasCmdContinue')}")
        logger.info(f"     FacilitySearch: {diag.get('hasFacilitySearch')}, SubmitBtn: {diag.get('hasSubmitBtn')}")
        logger.info(f"     ErrorPage: {diag.get('hasErrorPage')}")
        if diag.get("allButtons"):
            logger.info(f"     Buttons: {json.dumps(diag['allButtons'], indent=2)}")
        if diag.get("hasErrorPage"):
            logger.error(f"     ⚠️  ERROR PAGE DETECTED: {diag.get('bodyText', '')[:200]}")
        return diag
    except Exception as e:
        logger.warning(f"  Page diagnostics failed: {e}")
        return {}


async def run_test():
    DRY_RUN = "--dry-run" in sys.argv or "-n" in sys.argv
    SUBMIT = "--submit" in sys.argv

    if DRY_RUN:
        logger.info("🔒 DRY RUN MODE — will stop before Submit")
    elif SUBMIT:
        logger.info("🚀 LIVE SUBMIT MODE — will actually submit!")
    else:
        logger.info("🔍 DEBUG MODE — will pause at Submit for inspection (default)")

    # Load settings
    from app.core.settings import settings

    logger.info("=" * 70)
    logger.info("FULL SUBMISSION TEST — Login through Submit")
    logger.info("=" * 70)
    logger.info(f"Portal: {settings.CARELON_BASE_URL}")
    logger.info(f"Username: {settings.CARELON_USERNAME}")

    # ── Load test case from DB ──
    test_case = None
    clinical_context = {}

    # Allow override via command line
    MEMBER_ID = None
    for arg in sys.argv[1:]:
        if not arg.startswith("-"):
            MEMBER_ID = arg
            break

    try:
        from app.db.database import async_session_factory
        from app.db import repositories as repo
        from app.db.models import CaseState
        async with async_session_factory() as db:
            all_cases = await repo.list_cases(db, limit=200)
            target = None

            if MEMBER_ID:
                for c in all_cases:
                    if c.policy_num == MEMBER_ID or c.exam_id == MEMBER_ID or c.id == MEMBER_ID:
                        target = c
                        break
                if not target:
                    logger.error(f"Case not found: {MEMBER_ID}")
                    logger.info("Available cases:")
                    for c in all_cases[:20]:
                        logger.info(f"  {c.id[:8]}.. {c.first_name} {c.last_name} MID={c.policy_num} CPT={c.cpt_code} ICD={c.icd1}")
                    return
            else:
                # Pick first case with clinical notes
                for c in all_cases:
                    if c.state in (CaseState.NOTES_UPLOADED, CaseState.PENDING_NOTES):
                        target = c
                        break
                if not target and all_cases:
                    target = all_cases[0]

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
                logger.info(f"Test case: {c.first_name} {c.last_name}")
                logger.info(f"  MemberID={c.policy_num}, CPT={c.cpt_code}, ICD={c.icd1}")
                logger.info(f"  Center NPI={c.center_npi}, Referring NPI={c.referring_npi}")
                logger.info(f"  State={c.state.value}")

                # Load clinical context
                notes = await repo.get_notes_for_case(db, c.id)
                if not notes and (c.clinical_blob_key or c.file_key):
                    blob_key = c.clinical_blob_key or c.file_key
                    logger.info(f"Extracting clinical notes from blob: {blob_key}")
                    try:
                        from app.ingest.blob_fetcher import fetch_clinical_pdf
                        from app.intelligence.extractor import extract_clinical_context
                        pdf_bytes = fetch_clinical_pdf({"clinical_blob_key": c.clinical_blob_key, "file_key": c.file_key})
                        if pdf_bytes:
                            case_ctx = {"cpt_code": c.cpt_code, "icd1": c.icd1}
                            clinical_context = await extract_clinical_context(pdf_bytes, case_ctx) or {}
                            logger.info(f"Extracted {len(clinical_context)} fields")
                    except Exception as e:
                        logger.warning(f"Extraction failed: {e}")
                elif notes:
                    for note in notes:
                        if note.structured:
                            clinical_context.update(note.structured)
                    logger.info(f"Loaded clinical context from {len(notes)} note(s)")
    except Exception as e:
        logger.warning(f"Could not load test case: {e}")

    if not test_case:
        logger.error("No test case available. Pass a member ID, exam ID, or case ID as argument.")
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

    # ==================================================================
    # STEP 1: LOGIN
    # ==================================================================
    logger.info("\n" + "=" * 60)
    logger.info("STEP 1: LOGIN (Okta IDX + MFA)")
    logger.info("=" * 60)
    t0 = time.time()

    from app.auth.okta_login import okta_login
    try:
        provider_id, client_id = await okta_login(page, behavior)
        log_step("1", "Login", {"ok": True}, int((time.time() - t0) * 1000))
        logger.info(f"  Provider: {provider_id}, Client: {client_id}")
    except Exception as e:
        log_step("1", "Login", {"ok": False, "message": str(e)})
        await pause(page, "login_failed")
        await browser.close()
        await pw.stop()
        return

    # Build session
    session = PlaywrightPortalSession(
        context=context, page=page,
        center_npi=test_case["center_npi"],
        provider_id=provider_id, client_id=client_id,
    )
    webforms = WebFormsClient(session)
    clinical = ClinicalExamFlow(session)

    # ==================================================================
    # STEP 2: AGREE TO TERMS
    # ==================================================================
    logger.info("\n--- STEP 2: AGREE TO TERMS ---")
    t0 = time.time()
    r = await webforms.agree_to_terms()
    log_step("2", "Agree to terms", r, int((time.time() - t0) * 1000))
    await pause(page, "after_terms", 1)

    # ==================================================================
    # STEP 3: MEMBER SEARCH
    # ==================================================================
    logger.info("\n--- STEP 3: MEMBER SEARCH ---")
    t0 = time.time()
    dob = test_case["dob"]
    if dob and hasattr(dob, "strftime"):
        dob = dob.strftime("%m/%d/%Y")
    r = await webforms.search_member(
        first_name=test_case["first_name"],
        last_name=test_case["last_name"],
        dob=str(dob),
        policy_num=test_case["policy_num"],
    )
    log_step("3", "Member search", r, int((time.time() - t0) * 1000))
    if not r["ok"]:
        await pause(page, "member_failed", 30)
        await browser.close(); await pw.stop(); return

    # ==================================================================
    # STEP 4: ELIGIBILITY
    # ==================================================================
    logger.info("\n--- STEP 4: ELIGIBILITY ---")
    t0 = time.time()
    r = await webforms.extract_eligibility_details()
    log_step("4", "Eligibility", r, int((time.time() - t0) * 1000))
    if r["ok"]:
        logger.info(f"  Effective: {r['data'].get('effective_date')}, Term: {r['data'].get('term_date')}")

    # ==================================================================
    # STEP 5: SELECT DIAGNOSTIC IMAGING
    # ==================================================================
    logger.info("\n--- STEP 5: SELECT DIAGNOSTIC IMAGING ---")
    t0 = time.time()
    r = await webforms.select_diagnostic_imaging()
    log_step("5", "Select DI", r, int((time.time() - t0) * 1000))
    if not r["ok"]:
        await pause(page, "di_failed", 30)
        await browser.close(); await pw.stop(); return

    await asyncio.sleep(2)
    phone_result = await webforms.extract_patient_phone()
    logger.info(f"  Patient phone: {phone_result}")

    # ==================================================================
    # STEP 6: START ORDER
    # ==================================================================
    logger.info("\n--- STEP 6: START ORDER ---")
    t0 = time.time()
    r = await webforms.start_order_request()
    log_step("6", "Start order", r, int((time.time() - t0) * 1000))
    if not r["ok"]:
        await pause(page, "start_order_failed", 30)
        await browser.close(); await pw.stop(); return

    # ==================================================================
    # STEP 7: EXISTING AUTHS + NEXT
    # ==================================================================
    logger.info("\n--- STEP 7: EXISTING AUTHS ---")
    t0 = time.time()
    auths_r = await webforms.extract_existing_auths()
    log_step("7a", "Extract auths", auths_r, int((time.time() - t0) * 1000))
    if auths_r["ok"] and auths_r["data"].get("auths"):
        for a in auths_r["data"]["auths"]:
            logger.info(f"  Auth: {a.get('order_id')} {a.get('exam_description')} → {a.get('outcome')}")

    t0 = time.time()
    r = await webforms.click_next_after_auths()
    log_step("7b", "Click Next", r, int((time.time() - t0) * 1000))
    await pause(page, "after_auths", 1)

    # ==================================================================
    # STEP 8: PROVIDER SEARCH
    # ==================================================================
    logger.info("\n--- STEP 8: PROVIDER SEARCH ---")
    t0 = time.time()
    fax = test_case.get("referring_fax", "")
    r = await webforms.search_provider(
        referring_npi=test_case["referring_npi"],
        state="TX",
        fax=fax,
        match_address=test_case.get("referring_address", ""),
    )
    log_step("8", "Provider search", r, int((time.time() - t0) * 1000))
    if not r["ok"]:
        await pause(page, "provider_failed", 30)
        await browser.close(); await pw.stop(); return
    await pause(page, "after_provider", 1)

    # ==================================================================
    # STEP 9: CLINICAL INIT
    # ==================================================================
    logger.info("\n--- STEP 9: CLINICAL INIT ---")
    t0 = time.time()
    r = await clinical.initialize()
    log_step("9", "Clinical init", r, int((time.time() - t0) * 1000))
    if not r["ok"]:
        await pause(page, "clinical_init_failed", 30)
        await browser.close(); await pw.stop(); return

    # ==================================================================
    # STEP 10: EXAM SETUP (CPT)
    # ==================================================================
    logger.info("\n--- STEP 10: EXAM SETUP ---")
    t0 = time.time()
    r = await clinical.setup_exam(cpt_code=test_case["cpt_code"])
    log_step("10", f"Exam setup CPT={test_case['cpt_code']}", r, int((time.time() - t0) * 1000))
    if not r["ok"]:
        await pause(page, "exam_setup_failed", 30)
        await browser.close(); await pw.stop(); return

    # ==================================================================
    # STEP 11: DIAGNOSIS (ICD)
    # ==================================================================
    logger.info("\n--- STEP 11: DIAGNOSIS ---")
    t0 = time.time()
    r = await clinical.enter_diagnosis(test_case["icd1"])
    log_step("11", f"Diagnosis ICD={test_case['icd1']}", r, int((time.time() - t0) * 1000))
    if not r["ok"]:
        await pause(page, "diagnosis_failed", 30)
        await browser.close(); await pw.stop(); return

    # ==================================================================
    # STEP 12: PATHWAY
    # ==================================================================
    logger.info("\n--- STEP 12: PATHWAY ---")
    t0 = time.time()
    r = await clinical.select_pathway(preferred_icd=test_case["icd1"])
    log_step("12", "Pathway selection", r, int((time.time() - t0) * 1000))
    if not r["ok"]:
        await pause(page, "pathway_failed", 30)
        await browser.close(); await pw.stop(); return
    logger.info(f"  Pathway: {r.get('data', {}).get('pathway_name', 'unknown')}")

    # ==================================================================
    # STEP 13: CLINICAL QUESTIONS (LLM loop)
    # ==================================================================
    logger.info("\n--- STEP 13: CLINICAL QUESTIONS ---")
    t0 = time.time()
    from app.intelligence.answer_bridge import build_answer_fn
    pathway_name = r.get("data", {}).get("pathway_name", "")
    answer_fn = build_answer_fn(
        cpt_code=test_case.get("cpt_code", ""),
        icd1=test_case.get("icd1"),
        carrier_id=test_case.get("carrier_id"),
        clinical_context=clinical_context,
        pathway_name=pathway_name,
    )
    r = await clinical.run_clinical_questions_loop(answer_fn=answer_fn, max_rounds=20)
    log_step("13", "Clinical questions", r, int((time.time() - t0) * 1000))
    if not r["ok"]:
        await pause(page, "questions_failed", 30)
        await browser.close(); await pw.stop(); return

    loop_data = r["data"]
    logger.info(f"  Rounds: {loop_data.get('rounds', 0)}, Answers: {loop_data.get('total_answers', 0)}")
    await pause(page, "after_questions", 2)

    # ==================================================================
    # STEP 14: FINALIZE (ProcessAccepted → AddRadioTracers)
    # HAR: This runs on the clinical SPA page
    # ==================================================================
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
        await pause(page, "finalize_failed", 60)
        await browser.close(); await pw.stop(); return

    logger.info(f"  Auto approved: {r['data'].get('auto_approved')}")
    logger.info(f"  CDO approved: {r['data'].get('cdo_approved')}")
    await pause(page, "after_finalize", 2)

    # ==================================================================
    # STEP 15: hdnAction=20 → EXAM SUMMARY
    # HAR: Document navigation from clinical SPA to exam summary page
    # ==================================================================
    logger.info("\n" + "=" * 60)
    logger.info("STEP 15: hdnAction=20 → EXAM SUMMARY")
    logger.info("=" * 60)
    await diagnose_page(page, "before_hdnaction20")

    t0 = time.time()
    r = await webforms.postback_hdnaction(20, timeout_ms=30000)
    log_step("15", "hdnAction=20", r, int((time.time() - t0) * 1000))

    await asyncio.sleep(3)
    diag = await diagnose_page(page, "after_hdnaction20")
    await pause(page, "exam_summary", 3)

    if diag.get("hasErrorPage"):
        logger.error("⚠️  ERROR PAGE after hdnAction=20! Stopping.")
        await asyncio.sleep(120)
        await browser.close(); await pw.stop(); return

    # ==================================================================
    # STEP 16: EXAM SUMMARY REVIEW (DoneWithExam + FindNextExam)
    # HAR: API calls on the exam summary page
    # ==================================================================
    logger.info("\n" + "=" * 60)
    logger.info("STEP 16: EXAM SUMMARY REVIEW")
    logger.info("=" * 60)

    t0 = time.time()
    cpt_group = clinical._find_cpt_group_id(test_case["cpt_code"])
    logger.info(f"  CPT group for {test_case['cpt_code']}: {cpt_group}")

    r = await clinical.exam_summary_review(
        cpt_code=test_case["cpt_code"],
        cpt_group=cpt_group,
    )
    log_step("16", "Exam summary review", r, int((time.time() - t0) * 1000))

    if not r["ok"]:
        logger.error(f"Exam summary review FAILED: {r['message']}")
        await diagnose_page(page, "exam_summary_review_failed")
        await asyncio.sleep(120)
        await browser.close(); await pw.stop(); return

    await pause(page, "after_exam_summary_review", 2)

    # ==================================================================
    # STEP 17: hdnAction=6 → FACILITY SEARCH
    # HAR: Document navigation from exam summary to facility search
    # THIS IS THE STEP THAT FAILS IN PRODUCTION
    # ==================================================================
    logger.info("\n" + "=" * 60)
    logger.info("STEP 17: hdnAction=6 → FACILITY SEARCH ⚠️  (THE FAILING STEP)")
    logger.info("=" * 60)
    await diagnose_page(page, "before_hdnaction6")

    t0 = time.time()
    r = await webforms.postback_hdnaction(6, timeout_ms=30000)
    log_step("17", "hdnAction=6", r, int((time.time() - t0) * 1000))

    await asyncio.sleep(3)
    diag = await diagnose_page(page, "after_hdnaction6")
    await pause(page, "facility_search_page", 5)

    if diag.get("hasErrorPage"):
        logger.error("=" * 60)
        logger.error("⚠️  FACILITY SEARCH ERROR PAGE!")
        logger.error("This is the production bug. The portal returned an error")
        logger.error("instead of the facility search page after hdnAction=6.")
        logger.error("=" * 60)
        logger.info("Browser stays open 5 minutes for debugging...")
        await asyncio.sleep(300)
        await browser.close(); await pw.stop(); return

    if not diag.get("hasFacilitySearch"):
        logger.warning("⚠️  Facility search elements not found — page may not have loaded")
        logger.info("Browser stays open 2 minutes for debugging...")
        await asyncio.sleep(120)

    # ==================================================================
    # STEP 18: FACILITY SEARCH + SELECT
    # ==================================================================
    logger.info("\n--- STEP 18: FACILITY SEARCH ---")
    t0 = time.time()
    center_npi = test_case["center_npi"]
    fax = test_case.get("referring_fax", "")
    r = await webforms.search_facility(center_npi=center_npi, fax=fax)
    log_step("18", f"Facility search NPI={center_npi}", r, int((time.time() - t0) * 1000))

    if not r["ok"]:
        logger.error(f"Facility search FAILED: {r['message']}")
        await diagnose_page(page, "facility_search_failed")
        await asyncio.sleep(120)
        await browser.close(); await pw.stop(); return

    await pause(page, "after_facility", 3)

    # ==================================================================
    # STEP 19: ORDER PREVIEW / SUBMIT
    # ==================================================================
    logger.info("\n" + "=" * 60)
    logger.info("STEP 19: ORDER PREVIEW")
    logger.info("=" * 60)
    diag = await diagnose_page(page, "order_preview")
    await pause(page, "order_preview", 3)

    if DRY_RUN:
        logger.info("🔒 DRY RUN — stopping before Submit")
    elif SUBMIT:
        logger.info("🚀 SUBMITTING...")
        t0 = time.time()
        # Click Submit This Request
        try:
            submit_sel = "#asPrimary_ctl00_cmdSubmitRequest"
            await page.wait_for_selector(submit_sel, state="visible", timeout=10000)
            await page.click(submit_sel)
            await asyncio.sleep(5)

            # Capture submission result
            diag = await diagnose_page(page, "after_submit")
            log_step("19", "Submit", {"ok": True}, int((time.time() - t0) * 1000))

            # Try to extract auth number from result page
            result_text = await page.evaluate("() => document.body.innerText.substring(0, 1000)")
            logger.info(f"Submission result text: {result_text[:500]}")

            await pause(page, "submission_result", 10)
        except Exception as e:
            log_step("19", "Submit", {"ok": False, "message": str(e)})
            logger.error(f"Submit failed: {e}")
    else:
        logger.info("🔍 DEBUG MODE — pausing at Submit for 5 min inspection")
        logger.info("Run with --submit to actually submit, or --dry-run to skip")

    # ==================================================================
    # FINAL REPORT
    # ==================================================================
    logger.info("\n" + "=" * 60)
    logger.info("STEP LOG")
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

    # Keep browser open for inspection
    if not DRY_RUN and not SUBMIT:
        logger.info("\nBrowser stays open 5 min for inspection...")
        await asyncio.sleep(300)
    else:
        logger.info("\nBrowser stays open 30s...")
        await asyncio.sleep(30)

    await browser.close()
    await pw.stop()
    logger.info("Done.")


if __name__ == "__main__":
    asyncio.run(run_test())
