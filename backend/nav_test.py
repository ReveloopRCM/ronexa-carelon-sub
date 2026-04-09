"""Navigation test — run 2 cases, capture what happens between them."""
import asyncio
from playwright.async_api import async_playwright


async def main():
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False)
    context = await browser.new_context(
        record_har_path="/home/ronexa/session_nav_test.har",
        record_har_mode="full",
        viewport={"width": 1920, "height": 1080},
    )
    page = await context.new_page()

    from app.auth.okta_login import okta_login
    from app.core.settings import settings

    await okta_login(page, settings.CARELON_USERNAME, settings.CARELON_PASSWORD)
    print("LOGGED IN")

    # Wait for the search form to fully load (not just body text)
    try:
        await page.wait_for_selector("#asPrimary_ctl00_BtnSearch", state="visible", timeout=20000)
        print("Search button visible — form loaded")
    except Exception:
        print("Search button not visible after 20s — taking screenshot")
        await page.screenshot(path="/home/ronexa/screenshots/test_post_login.png")
        url = page.url
        title = await page.title()
        body = await page.evaluate("() => (document.body?.innerText || '').substring(0, 300)")
        print(f"URL: {url}")
        print(f"Title: {title}")
        print(f"Body: {body[:200]}")
        # Try reload
        await page.reload(wait_until="domcontentloaded")
        await asyncio.sleep(3)
        try:
            await page.wait_for_selector("#asPrimary_ctl00_BtnSearch", state="visible", timeout=15000)
            print("Search button visible after reload")
        except Exception:
            print("Still no search button after reload — aborting")
            await page.screenshot(path="/home/ronexa/screenshots/test_post_login_reload.png")
            await context.close()
            await browser.close()
            await pw.stop()
            return

    from app.compiler.portal_compiler import load_compiler
    from app.portal.session import PlaywrightPortalSession

    session = PlaywrightPortalSession(
        context=context, page=page, center_npi="worker-test"
    )
    session.provider_id = None
    session.client_id = None
    compiler = load_compiler("carelon_provider_portal")

    from app.db.database import async_session_factory
    from app.db.models import Case, CaseState, ClinicalNote
    from sqlalchemy import select

    async with async_session_factory() as db:
        cases = (
            await db.execute(
                select(Case).where(Case.state == CaseState.NOTES_UPLOADED).limit(2)
            )
        ).scalars().all()
        case_dicts = []
        for c in cases:
            note = (
                await db.execute(
                    select(ClinicalNote)
                    .where(ClinicalNote.case_id == c.id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            case_dicts.append(
                {
                    "id": c.id,
                    "exam_id": c.exam_id,
                    "first_name": c.first_name,
                    "last_name": c.last_name,
                    "dob": c.dob,
                    "policy_num": c.policy_num,
                    "patient_zip": c.patient_zip,
                    "center_npi": c.center_npi,
                    "cpt_code": c.cpt_code,
                    "icd1": c.icd1,
                    "referring_npi": c.referring_npi,
                    "referring_fax": c.referring_fax,
                    "carrier_id": c.carrier_id,
                    "is_stat": c.is_stat,
                    "raw_data": c.raw_data or {},
                    "clinical_context": note.structured if note else None,
                }
            )

    print(f"Case 1: {case_dicts[0]['first_name']} {case_dicts[0]['last_name']}")
    print(f"Case 2: {case_dicts[1]['first_name']} {case_dicts[1]['last_name']}")

    # === CASE 1 ===
    print("=== CASE 1 START ===")
    result1 = await compiler.execute(
        case=case_dicts[0],
        session=session,
        clinical_context=case_dicts[0].get("clinical_context"),
    )
    status1 = (
        "review"
        if result1.get("answers")
        else result1.get("case_state", "unknown")
    )
    print(f"Case 1 result: {status1}")
    print(f"After case 1 URL: {page.url}")
    await page.screenshot(path="/home/ronexa/screenshots/test_after_case1.png")

    # === NAVIGATION TESTS ===
    # Dump all links on current page
    links = await page.evaluate("""
        () => Array.from(document.querySelectorAll('a')).map(a => ({
            href: (a.href || '').substring(0, 80),
            text: (a.textContent || '').trim().substring(0, 40),
            id: a.id,
            visible: a.offsetParent !== null,
        })).filter(l => l.text)
    """)
    print(f"Links on page after case 1 ({len(links)}):")
    for l in links[:15]:
        print(f"  [{l['id']}] '{l['text']}' → {l['href']} (visible={l['visible']})")

    # Check if home button exists in DOM (even hidden)
    home_btn = await page.evaluate("""
        () => {
            const btn = document.getElementById('asPrimary_ctl00_btnGoToHomepage');
            if (btn) return {found: true, visible: btn.offsetParent !== null, tag: btn.tagName, text: btn.textContent};
            return {found: false};
        }
    """)
    print(f"Home button: {home_btn}")

    # Check if the ASP.NET form exists
    form_info = await page.evaluate("""
        () => {
            const form = document.querySelector('form[action*="Default.aspx"]');
            if (form) {
                const vs = form.querySelector('#__VIEWSTATE');
                return {found: true, action: form.action, hasViewState: !!vs};
            }
            return {found: false};
        }
    """)
    print(f"ASP.NET form: {form_info}")

    # Test 1: Try clicking home button via JS
    print("\n=== NAV TEST 1: JS click home button ===")
    nav1 = await page.evaluate("""
        () => {
            const btn = document.getElementById('asPrimary_ctl00_btnGoToHomepage');
            if (btn) { btn.click(); return 'clicked'; }
            // Try any link with "Home" or "My Homepage"
            const links = document.querySelectorAll('a');
            for (const a of links) {
                if (a.textContent.includes('My Homepage') || a.textContent.includes('Home')) {
                    a.click();
                    return 'clicked: ' + a.textContent.trim();
                }
            }
            return 'not found';
        }
    """)
    print(f"Nav test 1 result: {nav1}")
    await asyncio.sleep(5)
    await page.screenshot(path="/home/ronexa/screenshots/test_nav1.png")
    print(f"After nav1 URL: {page.url}")
    title1 = await page.title()
    print(f"After nav1 title: {title1}")

    # Check if search button appears
    try:
        await page.wait_for_selector(
            "#asPrimary_ctl00_BtnSearch", state="visible", timeout=10000
        )
        print("SEARCH BUTTON FOUND — session alive!")
    except Exception:
        print("NO SEARCH BUTTON")
        body = await page.evaluate(
            "() => (document.body?.innerText || '').substring(0, 200)"
        )
        print(f"Body: {body[:100]}")

    print("\nWaiting 10s then closing...")
    await asyncio.sleep(10)
    await context.close()
    await browser.close()
    await pw.stop()
    print("DONE — HAR saved to /home/ronexa/session_nav_test.har")


asyncio.run(main())
