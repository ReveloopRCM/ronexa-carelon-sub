"""Local nav test — auto-login, auto-run case 1, pause for exploration.

Opens visible browser, records HAR the entire time.
Auto: login → case 1 (full clinical flow)
Pause: you explore navigation
Auto: case 2 if nav works

Run from terminal (needs input): cd backend && python3 local_nav_test.py
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://ronexa:mvXQqJOBvshxcCYOEYdv@ronexa-pg.postgres.database.azure.com:5432/ronexa?ssl=require",
)

SCREENSHOTS_DIR = "/tmp/nav_test_screenshots"
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

CASE_EXAM_IDS = ["17155099", "17153393"]


async def screenshot(page, name):
    path = f"{SCREENSHOTS_DIR}/{name}.png"
    await page.screenshot(path=path)
    print(f"  [screenshot: {name}.png]")


async def inspect(page, label):
    url = page.url
    title = await page.title()
    print(f"\n--- {label} ---")
    print(f"  URL: {url}")
    print(f"  Title: {title}")

    has_search = await page.evaluate("() => !!document.querySelector('#asPrimary_ctl00_BtnSearch')")
    has_home = await page.evaluate("() => !!document.getElementById('asPrimary_ctl00_btnGoToHomepage')")
    has_form = await page.evaluate("() => !!document.querySelector('form[action*=\"Default.aspx\"]')")
    has_vs = await page.evaluate("() => !!document.querySelector('#__VIEWSTATE')")
    is_login = await page.evaluate("() => (document.body?.innerText || '').includes('User Confirmation') || (document.body?.innerText || '').includes('User Login')")

    print(f"  Search btn: {has_search} | Home btn: {has_home} | Form: {has_form} | ViewState: {has_vs} | Login page: {is_login}")

    nav_links = await page.evaluate("""
        () => Array.from(document.querySelectorAll('a'))
            .filter(a => a.offsetParent !== null)
            .map(a => ({text: a.textContent?.trim(), href: (a.href||'').substring(0,60), id: a.id}))
            .filter(l => l.text && l.text.length > 1 && l.text.length < 40)
            .slice(0, 15)
    """)
    print(f"  Visible links ({len(nav_links)}):")
    for l in nav_links:
        print(f"    '{l['text']}' → {l['href']} [{l['id'][:20] if l['id'] else '-'}]")


async def main():
    from playwright.async_api import async_playwright

    # Clean old screenshots
    for f in os.listdir(SCREENSHOTS_DIR):
        os.remove(os.path.join(SCREENSHOTS_DIR, f))

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False, slow_mo=50)
    context = await browser.new_context(
        record_har_path="/tmp/nav_test.har",
        record_har_mode="full",
        viewport={"width": 1920, "height": 1080},
    )
    page = await context.new_page()

    # ── AUTO LOGIN ──
    print("\n=== AUTO LOGIN ===")
    from app.auth.okta_login import okta_login
    from app.core.settings import settings
    from app.portal.behavior_engine import BehaviorEngine

    behavior = BehaviorEngine(page)
    await okta_login(page, behavior, settings.CARELON_USERNAME, settings.CARELON_PASSWORD)
    print("Login complete!")
    await screenshot(page, "01_logged_in")

    # Wait for search form
    try:
        await page.wait_for_selector("#asPrimary_ctl00_BtnSearch", state="visible", timeout=20000)
        print("Search button ready!")
    except Exception:
        print("Search button not visible — check browser")
        await screenshot(page, "01b_no_search")
        await inspect(page, "Post-login")
        input("Fix in browser if needed, then press Enter...")

    # ── LOAD CASES ──
    from app.db.database import async_session_factory
    from app.db.models import Case, CaseState, ClinicalNote
    from sqlalchemy import select

    case_dicts = []
    async with async_session_factory() as db:
        for eid in CASE_EXAM_IDS:
            c = (await db.execute(select(Case).where(Case.exam_id == eid))).scalar_one_or_none()
            if not c:
                print(f"Case {eid} not found!")
                continue
            note = (await db.execute(select(ClinicalNote).where(ClinicalNote.case_id == c.id).limit(1))).scalar_one_or_none()
            case_dicts.append({
                "id": c.id, "exam_id": c.exam_id,
                "first_name": c.first_name, "last_name": c.last_name,
                "dob": c.dob, "policy_num": c.policy_num,
                "patient_zip": c.patient_zip, "center_npi": c.center_npi,
                "center_abbr": c.center_abbr, "cpt_code": c.cpt_code,
                "icd1": c.icd1, "icd2": c.icd2, "icd3": c.icd3,
                "icd4": c.icd4, "icd5": c.icd5,
                "referring_npi": c.referring_npi, "referring_fax": c.referring_fax,
                "carrier_id": c.carrier_id, "is_stat": c.is_stat,
                "raw_data": c.raw_data or {},
                "clinical_context": note.structured if note else None,
            })

    # ── CASE 1: AUTO RUN ──
    print(f"\n=== CASE 1: {case_dicts[0]['first_name']} {case_dicts[0]['last_name']} ===")
    print(f"CPT {case_dicts[0]['cpt_code']}, ICD {case_dicts[0]['icd1']}")

    from app.compiler.portal_compiler import load_compiler
    from app.portal.session import PlaywrightPortalSession

    session = PlaywrightPortalSession(context=context, page=page, center_npi="local-test")
    session.provider_id = None
    session.client_id = None
    compiler = load_compiler("carelon_provider_portal")

    print("Running compiler...")
    result1 = await compiler.execute(
        case=case_dicts[0], session=session,
        clinical_context=case_dicts[0].get("clinical_context"),
    )

    status1 = "review" if result1.get("answers") else result1.get("case_state", "unknown")
    print(f"\nCase 1 DONE: {status1}")
    if result1.get("answers"):
        print(f"  {len(result1['answers'])} questions")
        for a in result1["answers"]:
            print(f"    Q: {a['question_text'][:60]}  → {a['confidence']}%")
    if result1.get("hold_reason"):
        print(f"  HOLD: {result1['hold_reason']}")

    await screenshot(page, "02_after_case1")
    await inspect(page, "AFTER CASE 1 — THIS IS THE KEY PAGE")

    # ── PAUSE FOR EXPLORATION ──
    print("\n" + "=" * 60)
    print("CASE 1 COMPLETE — browser is on the post-clinical page")
    print("HAR is recording everything")
    print("")
    print("Options:")
    print("  1 = page.goto('Default.aspx')")
    print("  2 = JS click any Home/My Homepage link")
    print("  3 = Browser back")
    print("  4 = Reload")
    print("  5 = YOU click something, then Enter")
    print("  6 = Run case 2 now")
    print("  i = Inspect page again")
    print("  q = Quit + save HAR")
    print("=" * 60)

    while True:
        choice = input("\nChoice: ").strip()

        if choice == "1":
            await page.goto("https://www.providerportal.com/Default.aspx", wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(3)
            await screenshot(page, f"03_goto_{int(time.time())}")
            await inspect(page, "After goto Default.aspx")

        elif choice == "2":
            r = await page.evaluate("""
                () => {
                    let btn = document.getElementById('asPrimary_ctl00_btnGoToHomepage');
                    if (btn) { btn.click(); return 'clicked #btnGoToHomepage tag=' + btn.tagName; }
                    for (const a of document.querySelectorAll('a')) {
                        const t = (a.textContent||'').trim();
                        if (t === 'My Homepage' || t === 'Home') { a.click(); return 'clicked: ' + t; }
                    }
                    return 'nothing found';
                }
            """)
            print(f"  JS result: {r}")
            await asyncio.sleep(5)
            await screenshot(page, f"03_jshome_{int(time.time())}")
            await inspect(page, "After JS home click")

        elif choice == "3":
            try:
                await page.go_back(wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(3)
                await screenshot(page, f"03_back_{int(time.time())}")
                await inspect(page, "After back")
            except Exception as e:
                print(f"  Back failed: {e}")

        elif choice == "4":
            await page.reload(wait_until="domcontentloaded")
            await asyncio.sleep(3)
            await screenshot(page, f"03_reload_{int(time.time())}")
            await inspect(page, "After reload")

        elif choice == "5":
            input("  Click in browser, then Enter...")
            await asyncio.sleep(2)
            await screenshot(page, f"03_manual_{int(time.time())}")
            await inspect(page, "After your click")

        elif choice == "6":
            break

        elif choice == "i":
            await inspect(page, "Current page state")

        elif choice == "q":
            await context.close()
            await browser.close()
            await pw.stop()
            print(f"\nHAR: /tmp/nav_test.har")
            print(f"Screenshots: {SCREENSHOTS_DIR}/")
            return

        # Check readiness
        try:
            await page.wait_for_selector("#asPrimary_ctl00_BtnSearch", state="visible", timeout=3000)
            print("\n  SEARCH BUTTON FOUND!")
            go = input("  Ready for case 2? (y/n): ").strip()
            if go == "y":
                break
        except Exception:
            print("  (search button not visible yet)")

    # ── CASE 2 ──
    print(f"\n=== CASE 2: {case_dicts[1]['first_name']} {case_dicts[1]['last_name']} ===")
    print(f"CPT {case_dicts[1]['cpt_code']}, ICD {case_dicts[1]['icd1']}")
    await screenshot(page, "04_before_case2")

    print("Running compiler...")
    result2 = await compiler.execute(
        case=case_dicts[1], session=session,
        clinical_context=case_dicts[1].get("clinical_context"),
    )

    status2 = "review" if result2.get("answers") else result2.get("case_state", "unknown")
    print(f"\nCase 2 DONE: {status2}")
    if result2.get("answers"):
        print(f"  {len(result2['answers'])} questions")
    if result2.get("hold_reason"):
        print(f"  HOLD: {result2['hold_reason']}")

    await screenshot(page, "05_after_case2")
    await inspect(page, "After Case 2")

    # ── SUMMARY ──
    print("\n" + "=" * 60)
    print("TEST COMPLETE")
    print(f"  Case 1: {status1}")
    print(f"  Case 2: {status2}")
    print("=" * 60)

    input("Press Enter to close + save HAR...")
    await context.close()
    await browser.close()
    await pw.stop()
    print(f"\nHAR: /tmp/nav_test.har")
    print(f"Screenshots: {SCREENSHOTS_DIR}/")


if __name__ == "__main__":
    asyncio.run(main())
