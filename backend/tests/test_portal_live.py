"""Live portal test — full SOP flow.

Run with: python -m tests.test_portal_live
Runs headless=False so you can watch the browser.

Tests:
  1. Login via Okta IDX + MFA
  2. Agree to terms
  3. Search for a member from the DB
  4. Extract eligibility details (effective date)
  5. Select Diagnostic Imaging
  6. Extract patient phone number
  7. Start order request
  8. Check existing auths
  9. Search for referring provider + fax
  10. Clinical init (GetCase, available CPTs)
  11. Exam setup (CPT code entry)
  12. ICD diagnosis entry
  13. Pathway selection
  14. Clinical questions (get first batch)
"""
import asyncio
import logging
import sys

from playwright.async_api import async_playwright

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("test_portal_live")


async def run_test():
    # Load settings
    from app.core.settings import settings

    logger.info("=== PORTAL LIVE TEST (Full SOP) ===")
    logger.info(f"Portal: {settings.CARELON_BASE_URL}")
    logger.info(f"Username: {settings.CARELON_USERNAME}")
    logger.info(f"MFA Mailbox: {settings.GRAPH_MAILBOX}")

    # ── Hardcoded test cases (bypass ORM to avoid column mismatch) ──
    HARDCODED_CASES = {
        # CPT HOLD cases
        "17217298": {"id": "17217298", "first_name": "Timothy", "last_name": "Marker", "dob": "09/26/1968", "policy_num": "G4BAR1450097", "center_npi": "1942081153", "center_abbr": "FLM", "cpt_code": "72148", "icd1": "M51.360", "icd2": None, "icd3": None, "referring_npi": "1033145693", "carrier_id": "23523", "referring_fax": "9724733916", "referring_address": "6020 W Parker Rd Ste 200"},
        "17218415": {"id": "17218415", "first_name": "David", "last_name": "Byrd", "dob": "07/17/1977", "policy_num": "XXP967287651", "center_npi": "1649455536", "center_abbr": "ASI", "cpt_code": "72141", "icd1": "M54.2", "icd2": "M54.6", "icd3": "Z02.71", "referring_npi": "1003111576", "carrier_id": "23520", "referring_fax": "3036226880", "referring_address": "1360 S Potomac St"},
        "17219009": {"id": "17219009", "first_name": "Qusai", "last_name": "Lal", "dob": "05/09/1990", "policy_num": "NTZ981042809", "center_npi": "1659628659", "center_abbr": "LAC", "cpt_code": "73721", "icd1": "M25.362", "icd2": None, "icd3": None, "referring_npi": "1871728907", "carrier_id": "23523", "referring_fax": "2144836201", "referring_address": "4301 N MacArthur Blvd"},
        # NO_AUTH / L1_REVIEW cases (DI does not require pre-authorization)
        "17225881": {"id": "17225881", "first_name": "Anne", "last_name": "Finkel", "dob": "01/11/1964", "policy_num": "F2A807609912", "center_npi": "1033492483", "center_abbr": "PLA", "cpt_code": "74177", "icd1": "K59.00", "icd2": "R10.32", "icd3": "R10.814", "referring_npi": "1558810572", "carrier_id": "23523", "referring_fax": "9723957536", "referring_address": "4325 N Josey Ln Ste 105"},
        "17227847": {"id": "17227847", "first_name": "Peter", "last_name": "Hirst", "dob": "01/27/1977", "policy_num": "FHN922510005", "center_npi": "1215358957", "center_abbr": "BED", "cpt_code": "72132", "icd1": "M47.817", "icd2": "M96.1", "icd3": "M51.362", "referring_npi": "1255662557", "carrier_id": "14675", "referring_fax": "2146188025", "referring_address": "5575 Frisco Square Blvd Ste 400"},
        "17223353": {"id": "17223353", "first_name": "Jessica", "last_name": "Hulm", "dob": "06/03/1981", "policy_num": "ZGP843949270", "center_npi": "1659628659", "center_abbr": "LAC", "cpt_code": "71250", "icd1": "R05.3", "icd2": None, "icd3": None, "referring_npi": "1699277525", "carrier_id": "23523", "referring_fax": "2144836201", "referring_address": "221 W Colorado Blvd Ste 525"},
        "17218521": {"id": "17218521", "first_name": "Susanne", "last_name": "Juergens", "dob": "08/27/1970", "policy_num": "H6B4130540CT", "center_npi": "1942081153", "center_abbr": "FLM", "cpt_code": "72148", "icd1": "M41.26", "icd2": "M54.59", "icd3": None, "referring_npi": "1154619971", "carrier_id": "23523", "referring_fax": "2142226660", "referring_address": "5000 Long Prairie Rd Ste 100"},
        "17225657": {"id": "17225657", "first_name": "Debra", "last_name": "Williams", "dob": "06/19/1960", "policy_num": "OGS201409531", "center_npi": "1578546677", "center_abbr": "LAF", "cpt_code": "74181", "icd1": None, "icd2": None, "icd3": None, "referring_npi": "1023350816", "carrier_id": "14675", "referring_fax": "3372696001", "referring_address": "439 Heymann Blvd"},
    }

    # Step 0: Get a test case — hardcoded first, then DB fallback
    # Accepts CLI arg: python3 -m tests.test_portal_live <EXAM_ID>
    import sys
    if len(sys.argv) > 1:
        TEST_MEMBER_ID = sys.argv[1]
        logger.info(f"Using CLI member ID: {TEST_MEMBER_ID}")
    else:
        TEST_MEMBER_ID = "17217298"  # Timothy Marker — CPT 72148, ICD M51.360

    # Helper: save flow check as audit event (visible in GUI)
    async def save_flow_check(case_id: str, check_name: str, check_data: dict):
        """Persist inline check result so reviewer can see it in the GUI."""
        try:
            from app.db.database import async_session_factory as _sf
            from app.db import repositories as _repo
            async with _sf() as _db:
                await _repo.create_audit_event(
                    _db,
                    case_id=case_id,
                    actor="system",
                    action=f"flow_check:{check_name}",
                    data=check_data,
                )
                await _db.commit()
            logger.info(f"Saved flow_check:{check_name} to audit trail")
        except Exception as e:
            logger.warning(f"Could not save flow check: {e}")

    test_case = None
    clinical_context = {}

    # Check hardcoded cases first (avoids ORM column mismatch issues)
    if TEST_MEMBER_ID in HARDCODED_CASES:
        test_case = HARDCODED_CASES[TEST_MEMBER_ID]
        logger.info(
            f"Using hardcoded test case: {test_case['first_name']} {test_case['last_name']}, "
            f"MemberID={test_case['policy_num']}, CPT={test_case['cpt_code']}, "
            f"ICD={test_case['icd1']}, Referring NPI={test_case['referring_npi']}"
        )

    if not test_case:
     try:
        from app.db.database import async_session_factory
        from app.db import repositories as repo
        from app.db.models import CaseState
        async with async_session_factory() as db:
            # If a specific member is requested, find them
            target_cases = []
            if TEST_MEMBER_ID:
                from sqlalchemy import select
                from app.db.models import Case
                stmt = select(Case).where(
                    (Case.policy_num == TEST_MEMBER_ID) | (Case.exam_id == TEST_MEMBER_ID)
                ).limit(1)
                result = await db.execute(stmt)
                target_cases = list(result.scalars().all())
            if not target_cases:
                # Fallback: prefer cases with extracted clinical notes
                target_cases = await repo.list_cases(
                    db, state=CaseState.NOTES_UPLOADED, limit=1,
                )
            if not target_cases:
                target_cases = await repo.list_cases(db, limit=1)

            if target_cases:
                c = target_cases[0]
                test_case = {
                    "id": c.id,
                    "first_name": c.first_name,
                    "last_name": c.last_name,
                    "dob": c.dob,
                    "policy_num": c.policy_num,
                    "referring_npi": c.referring_npi,
                    "center_npi": c.center_npi,
                    "cpt_code": c.cpt_code,
                    "icd1": c.icd1,
                    "icd2": c.icd2,
                    "icd3": c.icd3,
                    "carrier_id": c.carrier_id,
                    "referring_fax": c.referring_fax,
                }
                logger.info(
                    f"Test case: {c.first_name} {c.last_name}, "
                    f"MemberID={c.policy_num}, CPT={c.cpt_code}, "
                    f"ICD={c.icd1}, State={c.state.value}, "
                    f"Referring NPI={c.referring_npi}, Center NPI={c.center_npi}"
                )

                # Load clinical context from extracted notes
                notes = await repo.get_notes_for_case(db, c.id)

                # If no notes but case has a clinical PDF, extract now
                if not notes and (c.clinical_blob_key or c.file_key):
                    blob_key = c.clinical_blob_key or c.file_key
                    logger.info(
                        f"No extracted notes but blob exists ({blob_key}) "
                        f"— downloading and extracting now..."
                    )
                    try:
                        from app.ingest.blob_fetcher import fetch_clinical_pdf
                        from app.ingest.pdf_parser import extract_page_images
                        from app.intelligence.extractor import extract_clinical_context

                        pdf_bytes = fetch_clinical_pdf({
                            "clinical_blob_key": c.clinical_blob_key,
                            "file_key": c.file_key,
                        })
                        if pdf_bytes:
                            pages = extract_page_images(pdf_bytes)
                            logger.info(f"PDF downloaded: {len(pdf_bytes)} bytes, {len(pages)} page(s)")

                            case_context = {"cpt_code": c.cpt_code, "icd1": c.icd1}
                            structured = await extract_clinical_context(pdf_bytes, case_context)
                            logger.info(
                                f"Extraction complete: {list(structured.keys())[:10]}"
                            )

                            # Save to DB
                            await repo.create_clinical_note(
                                db,
                                case_id=c.id,
                                filename=blob_key,
                                page_count=len(pages),
                                document_type=structured.get("document_type", "UNKNOWN"),
                                document_quality=structured.get("document_quality", "CLEAN"),
                                extraction_method="haiku_vision",
                                structured=structured,
                            )
                            await db.commit()

                            # Merge into clinical_context
                            if structured and "error" not in structured:
                                clinical_context.update(structured)
                                logger.info(
                                    f"Clinical context extracted: "
                                    f"{list(clinical_context.keys())[:10]}"
                                )
                            else:
                                logger.warning(
                                    f"Extraction returned error: {structured.get('error')}"
                                )
                        else:
                            logger.warning("Blob fetch returned empty — no PDF available")
                    except Exception as e:
                        logger.error(f"Clinical extraction failed: {e}")

                elif notes:
                    for note in notes:
                        if note.structured:
                            clinical_context.update(note.structured)
                    logger.info(
                        f"Loaded clinical context from {len(notes)} note(s): "
                        f"{list(clinical_context.keys())[:10]}"
                    )
                else:
                    logger.info("No clinical notes and no blob key — LLM will answer without context")

                # Save clinical context status to audit trail
                await save_flow_check(c.id, "clinical_context", {
                    "has_notes": bool(clinical_context),
                    "fields": list(clinical_context.keys())[:10] if clinical_context else [],
                    "status": "loaded" if clinical_context else "missing",
                })
     except Exception as e:
        logger.warning(f"Could not load test case from DB: {e}")

    if not test_case:
        logger.info("No test case found — will test login only")

    # Step 1: Launch browser (visible) with packet capture
    CAPTURE_FILE = "/tmp/portal_packets.jsonl"
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False)
    context = await browser.new_context(
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="America/Chicago",
    )
    page = await context.new_page()

    # Real-time packet capture — intercept every ClinicalFacade + Default.aspx call
    import json as _json
    _capture_fh = open(CAPTURE_FILE, "w")
    _capture_count = 0

    async def _on_response(response):
        nonlocal _capture_count
        url = response.url
        # Only capture portal API calls
        if "ClinicalFacade" not in url and "Default.aspx" not in url:
            return
        try:
            req = response.request
            req_body = req.post_data or ""
            resp_body = ""
            try:
                resp_body = await response.text()
            except:
                resp_body = f"[binary, size={response.headers.get('content-length', '?')}]"

            endpoint = url.split("/")[-1].split("?")[0] if "ClinicalFacade" in url else "Default.aspx"
            _capture_count += 1
            record = {
                "seq": _capture_count,
                "endpoint": endpoint,
                "method": req.method,
                "status": response.status,
                "url": url,
                "req_body": req_body[:5000] if isinstance(req_body, str) else str(req_body)[:5000],
                "resp_body": resp_body[:200000] if isinstance(resp_body, str) else str(resp_body)[:200000],
            }
            _capture_fh.write(_json.dumps(record) + "\n")
            _capture_fh.flush()
        except Exception as e:
            logger.debug(f"Capture error for {url}: {e}")

    page.on("response", _on_response)
    logger.info(f"Packet capture active → {CAPTURE_FILE}")

    from app.portal.behavior_engine import BehaviorEngine
    behavior = BehaviorEngine(page)

    # ---------------------------------------------------------------
    # STEP 1: LOGIN
    # ---------------------------------------------------------------
    logger.info("--- STEP 1: LOGIN ---")
    from app.auth.okta_login import okta_login
    try:
        provider_id, client_id = await okta_login(page, behavior)
        logger.info(f"Login SUCCESS. Provider: {provider_id}, Client: {client_id}")
    except Exception as e:
        logger.error(f"Login FAILED: {e}")
        await page.screenshot(path="/tmp/login_failed.png")
        logger.info("Screenshot saved to /tmp/login_failed.png")
        await browser.close()
        await pw.stop()
        return

    # ---------------------------------------------------------------
    # STEP 2: AGREE TO TERMS
    # ---------------------------------------------------------------
    logger.info("--- STEP 2: AGREE TO TERMS ---")
    from app.portal.session import PlaywrightPortalSession
    session = PlaywrightPortalSession(
        context=context, page=page, center_npi=test_case["center_npi"] if test_case else "test",
        provider_id=provider_id, client_id=client_id,
    )
    from app.portal.webforms_client import WebFormsClient
    webforms = WebFormsClient(session)

    result = await webforms.agree_to_terms()
    logger.info(f"Agree to terms: {result}")
    if not result["ok"]:
        logger.warning(f"Terms failed: {result['message']} — may already be past terms, continuing")

    if not test_case:
        logger.info("No test case — stopping after login")
        await asyncio.sleep(30)
        await browser.close()
        await pw.stop()
        return

    # ---------------------------------------------------------------
    # STEP 3: MEMBER SEARCH
    # ---------------------------------------------------------------
    logger.info("--- STEP 3: MEMBER SEARCH ---")
    dob = test_case["dob"]
    if dob and hasattr(dob, "strftime"):
        dob = dob.strftime("%m/%d/%Y")

    result = await webforms.search_member(
        first_name=test_case["first_name"],
        last_name=test_case["last_name"],
        dob=str(dob),
        policy_num=test_case["policy_num"],
    )
    logger.info(f"Member search result: {result}")

    if not result["ok"]:
        logger.error(f"Member search FAILED: {result['message']}")
        await page.screenshot(path="/tmp/member_not_found.png")
        await browser.close()
        await pw.stop()
        return

    # ---------------------------------------------------------------
    # STEP 4: EXTRACT ELIGIBILITY DETAILS
    # ---------------------------------------------------------------
    logger.info("--- STEP 4: EXTRACT ELIGIBILITY DETAILS ---")
    elig_result = await webforms.extract_eligibility_details()
    logger.info(f"Eligibility details: {elig_result}")
    await page.screenshot(path="/tmp/eligibility_details.png")

    # INLINE CHECK: Eligibility validation (pure Python)
    if elig_result["ok"]:
        from app.portal.clinical_flow import check_eligibility
        eff_date = elig_result["data"].get("effective_date", "")
        term_date = elig_result["data"].get("term_date", "")
        elig_check = check_eligibility(eff_date, term_date)
        logger.info(f"Eligibility CHECK: {elig_check}")
        await save_flow_check(test_case["id"], "eligibility", elig_check)
        if not elig_check["eligible"]:
            logger.error(f"HOLD: Member not eligible — {elig_check['reason']}")
            await browser.close()
            await pw.stop()
            return

        # NO_AUTH CHECK: DI does not require pre-authorization
        if elig_result["data"].get("di_requires_auth") is False:
            logger.warning("*** NO_AUTH DETECTED at eligibility step ***")
            logger.warning("DI does not require pre-authorization for this member's plan")
            await page.screenshot(path="/tmp/no_auth_order_summary.png", full_page=True)
            logger.info("NO_AUTH Order Summary screenshot saved to /tmp/no_auth_order_summary.png")
            logger.info("Pausing 30s so you can inspect the browser...")
            await asyncio.sleep(30)
            await browser.close()
            await pw.stop()
            return

    # ---------------------------------------------------------------
    # STEP 5: SELECT DIAGNOSTIC IMAGING
    # ---------------------------------------------------------------
    logger.info("--- STEP 5: SELECT DIAGNOSTIC IMAGING ---")
    result = await webforms.select_diagnostic_imaging()
    logger.info(f"Select DI result: {result}")

    if not result["ok"]:
        logger.error(f"DI selection FAILED: {result['message']}")
        await page.screenshot(path="/tmp/di_select_failed.png")
        await browser.close()
        await pw.stop()
        return

    # Brief pause to let the phone field appear after DI selection
    await asyncio.sleep(2)

    # ---------------------------------------------------------------
    # STEP 6: EXTRACT PATIENT PHONE
    # ---------------------------------------------------------------
    logger.info("--- STEP 6: EXTRACT PATIENT PHONE ---")
    phone_result = await webforms.extract_patient_phone()
    logger.info(f"Patient phone: {phone_result}")
    await page.screenshot(path="/tmp/after_di_select.png")

    # ---------------------------------------------------------------
    # STEP 7: START ORDER REQUEST
    # ---------------------------------------------------------------
    logger.info("--- STEP 7: START ORDER REQUEST ---")
    result = await webforms.start_order_request()
    logger.info(f"Start order result: {result}")

    if not result["ok"]:
        logger.error(f"Start order FAILED: {result['message']}")
        await page.screenshot(path="/tmp/start_order_failed.png")
        await browser.close()
        await pw.stop()
        return

    await page.screenshot(path="/tmp/after_start_order.png")

    # ---------------------------------------------------------------
    # STEP 8: EXTRACT EXISTING AUTHS
    # ---------------------------------------------------------------
    logger.info("--- STEP 8: EXTRACT EXISTING AUTHS ---")
    auths_result = await webforms.extract_existing_auths()
    logger.info(f"Existing auths: {auths_result}")
    await page.screenshot(path="/tmp/existing_auths.png")

    # NO_AUTH CHECK: Portal says no auth required at auths step
    if auths_result["ok"] and auths_result["data"].get("no_auth_required"):
        logger.warning("*** NO_AUTH DETECTED at existing auths step ***")
        portal_msg = auths_result["data"].get("portal_message", "")[:200]
        logger.warning(f"Portal message: {portal_msg}")
        await page.screenshot(path="/tmp/no_auth_order_summary.png", full_page=True)
        logger.info("NO_AUTH Order Summary screenshot saved to /tmp/no_auth_order_summary.png")
        logger.info("Pausing 30s so you can inspect the browser...")
        await asyncio.sleep(30)
        await browser.close()
        await pw.stop()
        return

    if auths_result["ok"] and auths_result["data"].get("auths"):
        auths = auths_result["data"]["auths"]
        logger.info(f"Found {len(auths)} existing authorizations:")
        for auth in auths:
            logger.info(
                f"  [{auth['order_id']}] {auth['exam_description']} "
                f"({auth['date_of_service']}) — {auth['outcome']}"
            )

        # INLINE CHECK: Duplicate auth validation (pure Python)
        from app.portal.clinical_flow import check_duplicate_auth
        dup_check = check_duplicate_auth(auths, test_case.get("cpt_code", ""))
        logger.info(f"Duplicate auth CHECK: {dup_check}")
        await save_flow_check(test_case["id"], "duplicate_auth", dup_check)
        if dup_check["duplicate"]:
            logger.error(
                f"HOLD: Duplicate auth found — {dup_check['reason']}"
            )
            await browser.close()
            await pw.stop()
            return

    # Click Next
    logger.info("--- STEP 8b: CLICK NEXT ---")
    result = await webforms.click_next_after_auths()
    logger.info(f"Click Next result: {result}")
    await page.screenshot(path="/tmp/after_existing_auths.png")

    # ---------------------------------------------------------------
    # STEP 9: PROVIDER SEARCH + FAX
    # ---------------------------------------------------------------
    if test_case.get("referring_npi"):
        logger.info("--- STEP 9: PROVIDER SEARCH + FAX ---")
        # match_address comes from RIS/DB — for now use empty (select first)
        referring_fax = test_case.get("referring_fax", "7322088110")

        # Derive state from raw_data.CenterState (same logic as portal_compiler)
        _STATE_MAP = {
            "TEXAS": "TX", "COLORADO": "CO", "OKLAHOMA": "OK",
            "LOUISIANA": "LA", "UTAH": "UT", "FLORIDA": "FL",
            "CALIFORNIA": "CA", "NEW YORK": "NY", "ARIZONA": "AZ",
            "NEVADA": "NV", "GEORGIA": "GA", "OHIO": "OH",
            "MICHIGAN": "MI", "ILLINOIS": "IL", "PENNSYLVANIA": "PA",
            "NEW JERSEY": "NJ", "VIRGINIA": "VA", "NORTH CAROLINA": "NC",
            "TENNESSEE": "TN", "INDIANA": "IN", "MISSOURI": "MO",
            "MARYLAND": "MD", "WISCONSIN": "WI", "MINNESOTA": "MN",
            "WASHINGTON": "WA", "OREGON": "OR", "MASSACHUSETTS": "MA",
            "CONNECTICUT": "CT", "SOUTH CAROLINA": "SC", "ALABAMA": "AL",
            "KENTUCKY": "KY", "MISSISSIPPI": "MS", "ARKANSAS": "AR",
            "KANSAS": "KS", "IOWA": "IA", "NEBRASKA": "NE",
            "NEW MEXICO": "NM", "WEST VIRGINIA": "WV",
        }
        provider_state = "TX"  # default
        try:
            from app.db.database import async_session_factory as _sf2
            async with _sf2() as _db2:
                from app.db.models import Case as _CaseModel
                _case = await _db2.get(_CaseModel, test_case["id"])
                if _case and _case.raw_data:
                    center_state_raw = (_case.raw_data.get("CenterState") or "").strip().upper()
                    mapped = _STATE_MAP.get(center_state_raw, center_state_raw)
                    if len(mapped) == 2:
                        provider_state = mapped
                        logger.info(f"Provider state from CenterState: {center_state_raw} → {provider_state}")
        except Exception as e:
            logger.warning(f"Could not derive provider state: {e}")

        result = await webforms.search_provider(
            referring_npi=test_case["referring_npi"],
            state=provider_state,
            fax=referring_fax,
            match_address=test_case.get("referring_address", ""),
        )
        logger.info(f"Provider search result: ok={result['ok']}")
        await page.screenshot(path="/tmp/after_provider_search.png")

        if result["ok"]:
            providers = result["data"].get("providers", [])
            selected_idx = result["data"].get("selected_index", 0)
            logger.info(f"Extracted {len(providers)} provider(s), selected index={selected_idx}")
            for i, p in enumerate(providers):
                marker = " <-- SELECTED" if i == selected_idx else ""
                logger.info(
                    f"  [{i}] {p.get('name', '?')} — {p.get('address', '?')}, "
                    f"{p.get('city', '?')} ({p.get('specialty', '?')}){marker}"
                )
        else:
            logger.error(f"Provider search FAILED: {result['message']}")
            await browser.close()
            await pw.stop()
            return
    else:
        logger.warning("No referring_npi in test case — skipping provider search")

    # ---------------------------------------------------------------
    # STEP 9b: PAUSE — OBSERVE FAX POPUP
    # ---------------------------------------------------------------
    logger.info("--- STEP 9b: PAUSING 15s — LOOK AT THE BROWSER FOR FAX POPUP ---")
    logger.info("Check: Is there a fax popup? What fields/buttons does it have?")
    await page.screenshot(path="/tmp/fax_popup_check.png")
    # Dump the page HTML around the fax area for debugging
    try:
        fax_area_html = await page.evaluate("""() => {
            // Find the fax field and walk up to find its container/popup
            const faxField = document.querySelector('#asPrimary_ctl00_txbFax');
            let html = 'FAX FIELD: ' + (faxField ? 'found, value=' + faxField.value : 'NOT FOUND') + '\\n';

            if (faxField) {
                // Walk up the DOM to find the popup container
                let parent = faxField.parentElement;
                for (let i = 0; i < 10 && parent; i++) {
                    html += `PARENT[${i}]: tag=${parent.tagName} id=${parent.id} class=${parent.className}\\n`;
                    parent = parent.parentElement;
                }
            }

            // Dump ALL buttons, inputs[type=submit/button], and anchors near the fax field
            const allClickables = document.querySelectorAll('input[type="submit"], input[type="button"], button, a[href], input[type="image"]');
            html += '\\nALL CLICKABLE ELEMENTS (' + allClickables.length + '):\\n';
            allClickables.forEach((el, i) => {
                const visible = el.offsetParent !== null || el.offsetWidth > 0;
                if (visible) {
                    html += `[${i}] tag=${el.tagName} id=${el.id} name=${el.name || ''} type=${el.type || ''} value=${el.value || ''} text=${(el.textContent || '').trim().substring(0, 50)} class=${el.className}\\n`;
                }
            });

            // Also get the full HTML of the fax popup area
            if (faxField) {
                let popupContainer = faxField.closest('div[class*="popup"], div[class*="modal"], div[class*="panel"], table');
                if (!popupContainer) popupContainer = faxField.parentElement?.parentElement?.parentElement;
                if (popupContainer) {
                    html += '\\nPOPUP CONTAINER HTML (first 2000 chars):\\n';
                    html += popupContainer.outerHTML.substring(0, 2000);
                }
            }

            return html;
        }""")
        logger.info(f"FAX POPUP DEBUG:\\n{fax_area_html}")
    except Exception as e:
        logger.warning(f"Could not dump fax area HTML: {e}")
    await asyncio.sleep(15)
    await page.screenshot(path="/tmp/fax_popup_after_wait.png")

    # ---------------------------------------------------------------
    # STEP 10: CLINICAL INIT
    # ---------------------------------------------------------------
    logger.info("--- STEP 10: CLINICAL INIT ---")
    from app.portal.clinical_flow import ClinicalExamFlow
    clinical = ClinicalExamFlow(session)

    result = await clinical.initialize()
    logger.info(f"Clinical init: {result}")
    await page.screenshot(path="/tmp/clinical_init.png")

    if not result["ok"]:
        logger.error(f"Clinical init FAILED: {result['message']}")
        await browser.close()
        await pw.stop()
        return

    # ---------------------------------------------------------------
    # STEP 11: EXAM SETUP (CPT Code)
    # ---------------------------------------------------------------
    logger.info("--- STEP 11: EXAM SETUP ---")
    cpt_code = test_case.get("cpt_code", "")

    # Derive contrast from CPT code (intelligence — not guessing)
    from app.portal.clinical_flow import ClinicalExamFlow
    derived_contrast_id, derived_contrast = ClinicalExamFlow.derive_contrast_from_cpt(cpt_code)
    logger.info(
        f"Setting up exam for CPT: {cpt_code} → "
        f"Contrast: {derived_contrast} (id={derived_contrast_id})"
    )

    result = await clinical.setup_exam(cpt_code=cpt_code)
    logger.info(f"Exam setup: {result}")
    if result["ok"]:
        final_contrast = result['data'].get('contrast', '?')
        logger.info(f"Final contrast: {final_contrast}")
        await save_flow_check(test_case["id"], "contrast_selection", {
            "cpt_code": cpt_code,
            "derived_contrast": derived_contrast,
            "derived_contrast_id": derived_contrast_id,
            "final_contrast": final_contrast,
            "source": "derived_from_cpt",
        })
    await page.screenshot(path="/tmp/exam_setup.png")

    if not result["ok"]:
        logger.error(f"Exam setup FAILED: {result['message']}")
        await browser.close()
        await pw.stop()
        return

    # ---------------------------------------------------------------
    # STEP 12: ICD DIAGNOSIS
    # ---------------------------------------------------------------
    icd_code = test_case.get("icd1", "")
    if icd_code:
        logger.info(f"--- STEP 12: ICD DIAGNOSIS ({icd_code}) ---")
        result = await clinical.enter_diagnosis(icd_code)
        logger.info(f"Diagnosis: {result}")
        await page.screenshot(path="/tmp/diagnosis.png")

        if not result["ok"]:
            logger.error(f"Diagnosis FAILED: {result['message']}")
            await browser.close()
            await pw.stop()
            return
    else:
        logger.warning("No ICD code in test case — skipping diagnosis")

    # ---------------------------------------------------------------
    # STEP 13: PATHWAY SELECTION
    # ---------------------------------------------------------------
    logger.info("--- STEP 13: PATHWAY SELECTION ---")
    result = await clinical.select_pathway(preferred_icd=icd_code)
    logger.info(f"Pathway: {result}")
    await page.screenshot(path="/tmp/pathway.png")

    if not result["ok"]:
        logger.error(f"Pathway selection FAILED: {result['message']}")
        await browser.close()
        await pw.stop()
        return

    # ---------------------------------------------------------------
    # STEP 14: CLINICAL QUESTIONS (LLM-driven loop)
    # ---------------------------------------------------------------
    logger.info("--- STEP 14: CLINICAL QUESTIONS (LLM loop) ---")
    from app.intelligence.answer_bridge import build_answer_fn

    pathway_name = result.get("data", {}).get("pathway_name", "")
    llm_answer_fn = build_answer_fn(
        cpt_code=test_case.get("cpt_code", ""),
        icd1=test_case.get("icd1"),
        carrier_id=test_case.get("carrier_id"),
        clinical_context=clinical_context,
        pathway_name=pathway_name,
    )

    # ── HYPOTHESIS TEST: Force weakness branch on Q3 (symptoms multi-select) ──
    # Dumas (approved) selected: Pain + Paresthesia + Weakness
    # George (denied) selected:  Pain + Paresthesia + Reflex abnormality
    # Override Q3 to test if the weakness branch is the auto-approval path
    OVERRIDE_Q3 = False  # Set to True to force weakness branch (hypothesis testing)
    Q3_ID = "1bf488cb-c583-40a0-b9f0-c80609e1610c"  # "Which symptoms or exam findings"
    PAIN_OPT = "758fdd56-8099-4d94-8de9-e33fce4d468f"
    PARESTHESIA_OPT = "3da66608-b8c0-4b93-8e26-7249c96386ff"
    WEAKNESS_OPT = "d734f31c-ade4-4986-bf99-20b72f41a0b3"

    async def answer_fn(questions):
        answers = await llm_answer_fn(questions)
        if OVERRIDE_Q3:
            for i, q in enumerate(questions):
                if q.get("Id") == Q3_ID or "symptoms or exam findings" in (q.get("Text") or ""):
                    logger.info(f"🧪 OVERRIDE Q3: Forcing Pain + Paresthesia + Weakness (Dumas pattern)")
                    logger.info(f"   LLM original Values: {answers[i].get('Values')}")
                    answers[i]["Values"] = [PAIN_OPT, PARESTHESIA_OPT, WEAKNESS_OPT]
                    logger.info(f"   Override Values:     {answers[i]['Values']}")
        return answers

    result = await clinical.run_clinical_questions_loop(
        answer_fn=answer_fn,
        max_rounds=20,
    )
    logger.info(f"Clinical questions loop: {result}")
    await page.screenshot(path="/tmp/clinical_questions_done.png")

    if not result["ok"]:
        logger.error(f"Clinical questions FAILED: {result['message']}")
        await browser.close()
        await pw.stop()
        return

    loop_data = result["data"]
    logger.info(
        f"Questions complete: {loop_data.get('rounds', 0)} round(s), "
        f"{loop_data.get('total_answers', 0)} total answer(s), "
        f"{len(loop_data.get('all_questions', []))} question(s) seen"
    )

    # ---------------------------------------------------------------
    # STEP 14b: COMPLETENESS GATE (Pure Python)
    # ---------------------------------------------------------------
    logger.info("--- STEP 14b: COMPLETENESS GATE ---")
    from app.intelligence.completeness_check import check_completeness

    # Build answers list from the question loop results
    all_questions = loop_data.get("all_questions", [])
    answers_for_gate = []
    for q in all_questions:
        answers_for_gate.append({
            "question_id": q.get("id", ""),
            "question_text": q.get("question_text", ""),
            "confidence": q.get("ai_confidence", 0),
            "evidence": q.get("ai_evidence", ""),
        })

    completeness = check_completeness(answers_for_gate, clinical_context=clinical_context)
    logger.info(f"Completeness CHECK: {completeness}")
    await save_flow_check(test_case["id"], "completeness", completeness)

    if not completeness["passed"]:
        logger.error(
            f"HOLD: Completeness gate failed — {completeness['halt_reason']}"
        )
        logger.info(
            f"  Low confidence: {completeness['low_confidence_count']}, "
            f"  No evidence: {completeness['no_evidence_count']}, "
            f"  Has clinical notes: {completeness['has_clinical_notes']}"
        )
        # Don't stop the test — log the warning but continue for now
        # In production workflow, this would HOLD the case
        logger.warning("Continuing despite completeness failure (test mode)")

    # ---------------------------------------------------------------
    # STEP 15: FINALIZE (Process Determination — stops before DoneWithExam)
    # ---------------------------------------------------------------
    logger.info("--- STEP 15: FINALIZE (determination) ---")

    # Use patient phone from Step 6 if captured
    patient_phone = ""
    if phone_result and phone_result.get("ok"):
        patient_phone = phone_result["data"].get("phone", "")

    result = await clinical.finalize(
        first_name=test_case.get("first_name", ""),
        last_name=test_case.get("last_name", ""),
        phone=patient_phone,
        fax=test_case.get("referring_fax", "7322088110"),
    )
    logger.info(f"Finalize result: {result}")
    await page.screenshot(path="/tmp/finalize.png")

    if result["ok"]:
        det = result["data"]
        logger.info(
            f"🏆 ALGORITHM APPROVED: {det.get('algorithm_approved')} "
            f"(RecommendationType={det.get('algorithm_recommendation')})"
        )
        logger.info(
            f"   IsExamAutoApproved (UI flag only): {det.get('auto_approved')}, "
            f"CDO: {det.get('cdo_approved')}, "
            f"ExamResult={det.get('exam_result')}, ExamOutcome={det.get('exam_outcome')}"
        )
    else:
        logger.error(f"Finalize FAILED: {result['message']}")
        logger.info("Stopping test — finalize failed")
        await asyncio.sleep(60)
        await browser.close()
        await pw.stop()
        return

    # ---------------------------------------------------------------
    # STEP 16: hdnAction=20 → EXAM SUMMARY PAGE
    # ---------------------------------------------------------------
    logger.info("--- STEP 16: TRANSITION TO EXAM SUMMARY (hdnAction=20) ---")

    r = await webforms.postback_hdnaction(20, timeout_ms=20000)
    logger.info(f"hdnAction=20 result: {r}")
    await asyncio.sleep(3)
    await page.screenshot(path="/tmp/exam_summary.png")

    if not r["ok"]:
        logger.warning(f"hdnAction=20 issue: {r['message']} — continuing anyway")

    # ---------------------------------------------------------------
    # STEP 17: EXAM SUMMARY REVIEW (DoneWithExam + FindNextExam)
    # ---------------------------------------------------------------
    logger.info("--- STEP 17: EXAM SUMMARY REVIEW ---")

    cpt_group = clinical._find_cpt_group_id(test_case.get("cpt_code", ""))
    r = await clinical.exam_summary_review(
        cpt_code=test_case.get("cpt_code", ""),
        cpt_group=cpt_group,
    )
    logger.info(f"Exam summary review result: {r}")
    await page.screenshot(path="/tmp/after_exam_review.png")

    if not r["ok"]:
        logger.error(f"Exam summary review FAILED: {r['message']}")
        logger.info("Stopping test — exam summary review failed")
        await asyncio.sleep(60)
        await browser.close()
        await pw.stop()
        return

    # ---------------------------------------------------------------
    # STEP 18: hdnAction=6 → FACILITY SEARCH PAGE
    # ---------------------------------------------------------------
    logger.info("--- STEP 18: TRANSITION TO FACILITY SEARCH (hdnAction=6) ---")

    r = await webforms.postback_hdnaction(6, timeout_ms=20000)
    logger.info(f"hdnAction=6 result: {r}")
    await asyncio.sleep(2)
    await page.screenshot(path="/tmp/facility_search_page.png")

    if not r["ok"]:
        logger.warning(f"hdnAction=6 issue: {r['message']} — continuing anyway")

    # Diagnose: confirm we're on the facility search page
    fac_diag = await page.evaluate("""() => ({
        url: window.location.href,
        bodySnippet: document.body.innerText.substring(0, 400),
        hasFacility: document.body.innerText.toLowerCase().includes('facility'),
        hasAdvancedSearch: !!document.querySelector('[id*="lbProviderSearchAdvanced"]'),
        hasSearchBtn: !!document.querySelector('#asSearch_ctl00_btnSearch'),
    })""")
    logger.info(f"Facility page diagnostics: {fac_diag}")

    # ---------------------------------------------------------------
    # STEP 19: FACILITY SEARCH + SELECT
    # ---------------------------------------------------------------
    logger.info("--- STEP 19: FACILITY SEARCH ---")

    center_npi = test_case.get("center_npi", "")
    fax = test_case.get("referring_fax", "7322088110")
    if center_npi:
        # Reuse provider_state derived earlier from CenterState
        r = await webforms.search_facility(center_npi=center_npi, state=provider_state, fax=fax)
        logger.info(f"Facility search result: {r}")
        await page.screenshot(path="/tmp/facility_selected.png")

        if not r["ok"]:
            logger.error(f"Facility search FAILED: {r['message']}")
        else:
            logger.info("Facility selected + Continue clicked — on order summary page")
            await page.screenshot(path="/tmp/order_summary.png")

            # Diagnose order summary page
            summary_diag = await page.evaluate("""() => ({
                url: window.location.href,
                bodySnippet: document.body.innerText.substring(0, 400),
                hasSubmitBtn: !!document.querySelector('#asPrimary_ctl00_cmdSubmitRequest'),
            })""")
            logger.info(f"Order summary diagnostics: {summary_diag}")
    else:
        logger.warning("No center_npi in test case — skipping facility search")

    # ---------------------------------------------------------------
    # STOP — Do NOT submit (avoid duplicates)
    # ---------------------------------------------------------------
    logger.info("=== TEST COMPLETE — through facility search ===")
    logger.info("Stopped before Submit to avoid duplicate submissions.")
    logger.info("Screenshots saved to /tmp/")
    logger.info("Browser stays open 30s for inspection")
    await asyncio.sleep(30)

    # Close capture file and browser
    _capture_fh.close()
    logger.info(f"Packet capture saved: {CAPTURE_FILE} ({_capture_count} packets)")
    await browser.close()
    await pw.stop()
    logger.info("Done.")


if __name__ == "__main__":
    asyncio.run(run_test())
