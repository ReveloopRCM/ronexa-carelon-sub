"""Test facility search — logs into portal, runs full flow through to facility search.

Run with: python -m tests.test_facility_search [harvey|pastrana|lal]

This does a two-pass test:
  Pass 1: Run compiler normally → get clinical answers
  Pass 2: Navigate home, re-run with those answers as resume_answers → reaches facility search

Default: harvey (Colorado Springs Imaging, CO).
"""
import asyncio
import logging
import sys

from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("test_facility_search")

# ── Test fixtures (exported from production) ──
TEST_CASES = {
    "harvey": {
        "id": "e8da10a2-32c9-402e-93c4-94ca7ae16254",
        "first_name": "Michele", "last_name": "Harvey",
        "dob": "03/18/1970", "policy_num": "VAD864M91445",
        "cpt_code": "74177", "icd1": "R10.9", "icd2": None, "icd3": None,
        "center_npi": "1720060437", "referring_npi": "1124004353",
        "referring_fax": "7192607750",
        "raw_data": {
            "CenterNPI": "1720060437",
            "CenterDesc": "Colorado Springs Imaging",
            "CenterState": "COLORADO",
            "CenterAddress": "6005 Delmonico Dr, #180",
            "CenterAbbr": "CSI",
            "PatientZipCode": "80904",
            "ReferringProviderFax": "7192607750",
            "ReferringProviderAddress": "104 Pro Rodeo Dr",
            "ReferringProviderLastName": "Barrick",
            "ReferringProviderFirstName": "Steven",
        },
    },
    "pastrana": {
        "id": "022533e7-906c-48d5-934a-1e7b2dac2d08",
        "first_name": "Joseph", "last_name": "Pastrana",
        "dob": "03/20/1998", "policy_num": "YUP811974168",
        "cpt_code": "72148", "icd1": "M54.42", "icd2": None, "icd3": None,
        "center_npi": "1326743923", "referring_npi": "1003889551",
        "referring_fax": "9183927013",
        "patient_phone": "9185551234",
        "raw_data": {
            "CenterNPI": "1326743923",
            "CenterDesc": "Envision Imaging of Tulsa",
            "CenterState": "OKLAHOMA",
            "CenterAddress": "7714 E. 91st Street, Suite 100",
            "CenterAbbr": "TSA",
            "PatientZipCode": "74133",
            "ReferringProviderFax": "9183927013",
            "ReferringProviderAddress": "9001 S 101st East Ave Ste 270",
            "ReferringProviderLastName": "Roy",
            "ReferringProviderFirstName": "Jess",
        },
    },
    "lal": {
        "id": "f11307a7-b89c-42d6-8227-b3d56d429a5c",
        "first_name": "Qusai", "last_name": "Lal",
        "dob": "05/09/1990", "policy_num": "NTZ981042809",
        "cpt_code": "73721", "icd1": "M25.362", "icd2": None, "icd3": None,
        "center_npi": "1659628659", "referring_npi": "1871728907",
        "referring_fax": "2144836201",
        "patient_phone": "2145551234",
        "raw_data": {
            "CenterNPI": "1659628659",
            "CenterDesc": "Envision Imaging of Las Colinas",
            "CenterState": "TEXAS",
            "CenterAddress": "925 W. Royal Lane, Suite 100",
            "CenterAbbr": "LAC",
            "PatientZipCode": "75252",
            "ReferringProviderFax": "2144836201",
            "ReferringProviderAddress": "4301 N MacArthur Blvd",
            "ReferringProviderLastName": "Shah",
            "ReferringProviderFirstName": "Jay",
        },
    },
}


async def run_test():
    from app.core.settings import settings

    case_key = sys.argv[1].lower() if len(sys.argv) > 1 else "harvey"
    if case_key not in TEST_CASES:
        logger.error(f"Unknown case: {case_key}. Options: {list(TEST_CASES.keys())}")
        return

    case_data = TEST_CASES[case_key]
    raw = case_data["raw_data"]

    logger.info(f"=== FACILITY SEARCH TEST: {case_key.upper()} ===")
    logger.info(f"  Patient: {case_data['first_name']} {case_data['last_name']}")
    logger.info(f"  Policy: {case_data['policy_num']}")
    logger.info(f"  CenterNPI: {case_data['center_npi']}")
    logger.info(f"  CenterDesc: {raw.get('CenterDesc')}")
    logger.info(f"  CenterState: {raw.get('CenterState')}")
    logger.info(f"  CPT: {case_data['cpt_code']}, ICD: {case_data['icd1']}")

    # ── Launch browser ──
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False)
    context = await browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
    )
    page = await context.new_page()

    from app.portal.behavior_engine import BehaviorEngine
    behavior = BehaviorEngine(page)

    from app.auth.okta_login import okta_login
    try:
        provider_id, client_id = await okta_login(page, behavior)
        logger.info(f"Login OK — provider={provider_id}, client={client_id}")
    except Exception as e:
        logger.error(f"Login failed: {e}")
        await browser.close(); await pw.stop()
        return

    # ── Wait for homepage to be ready (don't navigate — login already landed here) ──
    logger.info(f"Post-login URL: {page.url}")
    await page.wait_for_timeout(2000)

    from app.portal.session import PlaywrightPortalSession
    session = PlaywrightPortalSession(
        context=context, page=page,
        center_npi=case_data["center_npi"] or "",
        provider_id=provider_id, client_id=client_id,
    )
    session.behavior = behavior

    from app.compiler.portal_compiler import load_compiler
    compiler = load_compiler("carelon_provider_portal")

    # ══════════════════════════════════════════════════════════
    # PASS 1: First pass — get clinical answers
    # ══════════════════════════════════════════════════════════
    logger.info("=" * 60)
    logger.info("PASS 1: First pass — getting clinical answers")
    logger.info("=" * 60)

    try:
        result1 = await compiler.execute(
            case=case_data, session=session,
            clinical_context=None, dry_run=False,
        )
    except Exception as e:
        logger.error(f"Pass 1 failed: {e}")
        await page.screenshot(path="/tmp/test_fac_pass1_fail.png")
        await browser.close(); await pw.stop()
        return

    answers = result1.get("answers", [])
    if not answers:
        logger.info(f"Pass 1 result: {list(result1.keys())}")
        if result1.get("case_state") == "HOLD":
            logger.error(f"HOLD: {result1.get('hold_reason')}")
        elif result1.get("facility_match"):
            # Auto-approved — already reached facility
            logger.info("Auto-approved — already reached facility search!")
            fac = result1["facility_match"]
            logger.info(f"  Name: {fac.get('name')}")
            logger.info(f"  Address: {fac.get('address')}")
            logger.info(f"  Match: {fac.get('match_method')}")
        await browser.close(); await pw.stop()
        return

    logger.info(f"Pass 1 got {len(answers)} answers for review")
    for a in answers[:5]:
        logger.info(f"  Q: {a.get('question_text', '?')[:60]} → {a.get('answer_value', '?')[:40]}")

    # ══════════════════════════════════════════════════════════
    # Navigate home before pass 2
    # ══════════════════════════════════════════════════════════
    logger.info("Navigating to homepage before pass 2 (via Home icon postback)...")
    try:
        nav_result = await page.evaluate("""
            () => {
                const homeLink = document.getElementById('asNavigation_ctl00_hlHome');
                if (homeLink) { homeLink.click(); return 'clicked home icon'; }
                const homeByTitle = document.querySelector('a[title="Home"]');
                if (homeByTitle) { homeByTitle.click(); return 'clicked home by title'; }
                if (typeof __doPostBack === 'function') {
                    __doPostBack('TopMenu', '');
                    return 'called __doPostBack(TopMenu)';
                }
                return 'not found';
            }
        """)
        logger.info(f"Home navigation: {nav_result}")
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(3000)
    except Exception as e:
        logger.warning(f"Home nav failed: {e}")

    # ══════════════════════════════════════════════════════════
    # PASS 2: Submit pass with approved answers → facility search
    # ══════════════════════════════════════════════════════════
    logger.info("=" * 60)
    logger.info("PASS 2: Submit pass with approved answers → facility search")
    logger.info("=" * 60)

    try:
        result2 = await compiler.execute(
            case=case_data, session=session,
            clinical_context=None,
            resume_answers=answers,  # Fast-forward clinical questions
            dry_run=True,            # Stop before submit
        )
    except Exception as e:
        logger.error(f"Pass 2 failed: {e}")
        await page.screenshot(path="/tmp/test_fac_pass2_fail.png")
        logger.info("Screenshot: /tmp/test_fac_pass2_fail.png")
        await browser.close(); await pw.stop()
        return

    logger.info(f"Pass 2 result keys: {list(result2.keys())}")

    if result2.get("case_state") == "HOLD":
        logger.error(f"HOLD: {result2.get('hold_reason')}")
    elif result2.get("case_state") == "DRY_RUN_COMPLETE":
        logger.info("=== DRY RUN COMPLETE — reached submit page! ===")

    fac = result2.get("facility_match")
    if fac:
        logger.info("=== FACILITY SEARCH RESULT ===")
        logger.info(f"  Name: {fac.get('name')}")
        logger.info(f"  Address: {fac.get('address')}")
        logger.info(f"  Match method: {fac.get('match_method')}")
        logger.info(f"  Results count: {fac.get('results_count')}")
        logger.info(f"  Selected index: {fac.get('selected_index')}")
        logger.info(f"  Fax: {fac.get('fax_entered')}")
    else:
        logger.warning("No facility_match in pass 2 result")

    await page.screenshot(path="/tmp/test_fac_final.png")
    logger.info("Screenshot: /tmp/test_fac_final.png")

    logger.info("Browser open — press Ctrl+C to close.")
    try:
        await asyncio.sleep(300)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass

    await browser.close()
    await pw.stop()
    logger.info("=== TEST COMPLETE ===")


if __name__ == "__main__":
    asyncio.run(run_test())
