"""Debug test: Run 5 failing cases through login → member search → start order → check auths.

Focuses on the provider search page transition issue.
Reuses the same browser session across all cases (navigate home between each).
"""
import asyncio
import logging

from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("test_prov_debug")

# The 5 failing cases
FAILING_CASES = [
    {"id": "e61d48c2-4160-4b06-8d9d-967bf40df897", "first_name": "Jodi", "last_name": "Ripley", "dob": "06/18/1970", "policy_num": "ZGP845335357", "cpt_code": "00004", "icd1": "M89.8X9", "center_npi": "1689095325", "referring_npi": "1881633766"},
    {"id": "8d2c7f57-c7cf-44f4-a31a-dc3b4500db76", "first_name": "Jodi", "last_name": "Ripley", "dob": "06/18/1970", "policy_num": "ZGP845335357", "cpt_code": "78315", "icd1": "M89.8X9", "center_npi": "1689095325", "referring_npi": "1881633766"},
    {"id": "feb17cde-1e15-464e-a2fb-9f5e54972f65", "first_name": "Hazel", "last_name": "Abarca", "dob": "10/09/2014", "policy_num": "T8M603802702", "cpt_code": "73221", "icd1": "S53.114A", "center_npi": "1265762470", "referring_npi": "1063807576"},
    {"id": "1de57a69-8a53-4319-9c43-006ff8adac10", "first_name": "Darlene", "last_name": "Graham", "dob": "09/24/1964", "policy_num": "GHP921405890", "cpt_code": "70450", "icd1": "R51.9", "center_npi": "1932520673", "referring_npi": "1669735734"},
    {"id": "257fd55d-ce23-4397-90c3-8754e6b355bb", "first_name": "Jamel", "last_name": "Shannon", "dob": "10/02/1983", "policy_num": "ZGP817044976", "cpt_code": "70450", "icd1": "R20.2", "center_npi": "1275892341", "referring_npi": "1114718418"},
]


async def test_single_case(webforms, session, case, case_num):
    """Run one case through member search → start order → check auths."""
    name = f"{case['first_name']} {case['last_name']}"
    logger.info(f"\n{'='*60}")
    logger.info(f"CASE {case_num}/5: {name} | CPT={case['cpt_code']} | MID={case['policy_num']}")
    logger.info(f"{'='*60}")

    page = session.page

    # Step 1: Member search
    result = await webforms.search_member(
        first_name=case["first_name"],
        last_name=case["last_name"],
        dob=case["dob"],
        policy_num=case["policy_num"],
    )
    if not result["ok"]:
        logger.error(f"  MEMBER NOT FOUND: {result['message']}")
        return {"case": name, "cpt": case["cpt_code"], "result": "MEMBER_NOT_FOUND"}

    logger.info(f"  Member found: {result['data']}")

    # Step 2: EARLY DETECTION — extract_eligibility_details() now checks di_requires_auth
    elig_result = await webforms.extract_eligibility_details()
    elig_data = elig_result.get("data", {})
    di_requires_auth = elig_data.get("di_requires_auth", True)

    if not di_requires_auth:
        logger.warning(f"  🟢 EARLY DETECTION: di_requires_auth=False — DI no pre-auth needed")
        await page.screenshot(path=f"/tmp/case{case_num}_early_detect.png")
        return {"case": name, "cpt": case["cpt_code"], "result": "NO_AUTH_REQUIRED_EARLY", "di_requires_auth": di_requires_auth}
    else:
        logger.info(f"  Eligibility: di_requires_auth=True (normal flow)")

    # Step 3: Select DI + Start Order (only if DI requires auth)
    await webforms.select_diagnostic_imaging()
    await asyncio.sleep(1)

    result = await webforms.start_order_request()
    if not result["ok"]:
        logger.error(f"  START ORDER FAILED: {result['message']}")
        return {"case": name, "cpt": case["cpt_code"], "result": "START_ORDER_FAILED"}

    # Step 4: FALLBACK DETECTION — check auths page for "does not require"
    result = await webforms.extract_existing_auths()
    auths_data = result.get("data", {})

    if auths_data.get("no_auth_required"):
        logger.warning(f"  🔶 NO AUTH REQUIRED — portal says no Order ID needed")
        portal_msg = auths_data.get("portal_message", "")[:200]
        logger.info(f"  Portal message: {portal_msg}")
        await page.screenshot(path=f"/tmp/case{case_num}_no_auth.png")
        return {"case": name, "cpt": case["cpt_code"], "result": "NO_AUTH_REQUIRED", "di_no_auth_on_elig": di_no_auth}

    if auths_data.get("already_on_provider"):
        logger.info(f"  ✅ NORMAL FLOW — skipped to provider search (no existing auths)")
        await page.screenshot(path=f"/tmp/case{case_num}_provider_page.png")
        return {"case": name, "cpt": case["cpt_code"], "result": "NORMAL_FLOW_PROVIDER", "di_no_auth_on_elig": di_no_auth}

    if auths_data.get("auths"):
        logger.info(f"  ✅ NORMAL FLOW — found {len(auths_data['auths'])} existing auths")
        await page.screenshot(path=f"/tmp/case{case_num}_auths_page.png")
        return {"case": name, "cpt": case["cpt_code"], "result": "NORMAL_FLOW_AUTHS", "di_no_auth_on_elig": di_no_auth}

    # Unexpected state
    page_text = await page.evaluate("() => document.body.innerText.substring(0, 500)")
    logger.warning(f"  ❓ UNEXPECTED PAGE: {page_text[:200]}")
    await page.screenshot(path=f"/tmp/case{case_num}_unexpected.png")
    return {"case": name, "cpt": case["cpt_code"], "result": "UNEXPECTED", "page_text": page_text[:200], "di_no_auth_on_elig": di_no_auth}


async def navigate_home(page):
    """Navigate back to member search page."""
    clicked = await page.evaluate("""
        () => {
            const homeLink = document.getElementById('asNavigation_ctl00_hlHome');
            if (homeLink) { homeLink.click(); return 'clicked'; }
            const homeByTitle = document.querySelector('a[title="Home"]');
            if (homeByTitle) { homeByTitle.click(); return 'clicked_title'; }
            if (typeof __doPostBack === 'function') { __doPostBack('TopMenu', ''); return 'postback'; }
            return 'not_found';
        }
    """)
    logger.info(f"  Home nav: {clicked}")
    if clicked == "not_found":
        return False
    await asyncio.sleep(3)
    try:
        await page.wait_for_selector("#asPrimary_ctl00_BtnSearch", state="visible", timeout=15000)
        return True
    except Exception:
        return False


async def run_test():
    from app.core.settings import settings

    logger.info("=== PROVIDER SEARCH DEBUG TEST (5 cases) ===")

    # Launch browser
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False)
    context = await browser.new_context(
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="America/Chicago",
    )
    page = await context.new_page()

    from app.portal.behavior_engine import BehaviorEngine
    behavior = BehaviorEngine(page)

    # Login
    logger.info("--- LOGIN ---")
    from app.auth.okta_login import okta_login
    try:
        provider_id, client_id = await okta_login(page, behavior)
        logger.info(f"Login SUCCESS")
    except Exception as e:
        logger.error(f"Login FAILED: {e}")
        await browser.close()
        await pw.stop()
        return

    # Skip terms (already past them after login)
    from app.portal.session import PlaywrightPortalSession
    session = PlaywrightPortalSession(
        context=context, page=page, center_npi="test",
        provider_id=provider_id, client_id=client_id,
    )
    from app.portal.webforms_client import WebFormsClient
    webforms = WebFormsClient(session)
    await webforms.agree_to_terms()  # May timeout — that's fine

    # Run all 5 cases
    results = []
    for i, case in enumerate(FAILING_CASES, 1):
        try:
            r = await test_single_case(webforms, session, case, i)
            results.append(r)
        except Exception as e:
            logger.error(f"  EXCEPTION on case {i}: {e}")
            results.append({"case": f"{case['first_name']} {case['last_name']}", "cpt": case["cpt_code"], "result": f"EXCEPTION: {e}"})

        # Navigate home for the next case (unless it's the last)
        if i < len(FAILING_CASES):
            logger.info("  Navigating home for next case...")
            ok = await navigate_home(page)
            if not ok:
                logger.warning("  Home nav failed — trying page reload")
                await page.goto("https://www.providerportal.com/Default.aspx")
                await asyncio.sleep(3)

    # Summary
    logger.info("\n" + "=" * 60)
    logger.info("SUMMARY")
    logger.info("=" * 60)
    for r in results:
        logger.info(f"  {r['case']} (CPT {r['cpt']}): {r['result']} | di_requires_auth={r.get('di_requires_auth', '?')}")

    logger.info("\nBrowser stays open 60s for inspection")
    await asyncio.sleep(60)
    await browser.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(run_test())
