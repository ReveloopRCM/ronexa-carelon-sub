"""Algorithm Approval Validation — Run 6 cases through clinical questions + finalize.

Tests that the clinical algorithm returns RecommendationType=3 (Approve) for each case.
Reuses a single browser session across all cases to minimize login overhead.

Usage:
    python3 -m tests.test_algorithm_validation
"""
import asyncio
import logging
import json
from datetime import datetime

from app.core.settings import settings
from app.db.database import async_session_factory
from app.db.models import Case
from sqlalchemy import select

logger = logging.getLogger("test_algorithm_validation")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

# ── Test cases: member IDs to validate ──
TEST_MEMBER_IDS = [
    "T2G944507278",   # Timothy George — 72148, M47.817 (lumbar MRI, spondylosis)
    "DOM537W24577",    # Heather Manley — 72158, S32.009A (lumbar MRI w/contrast, fracture)
    "BVL990621203",    # Robert Melott — 72148, M54.16 (lumbar MRI, radiculopathy)
    "160W08357",       # Melody Gardner — 72131, M54.50 (CT lumbar, low back pain)
    "T3X820178122",    # Kim Adcock — 70480, H90.3 (CT head, hearing loss)
    "GDJ839032123",    # Basiliso Tovar — 75574, R07.9 (cardiac CT, chest pain)
]


async def load_case(member_id: str) -> dict | None:
    """Load case from DB."""
    async with async_session_factory() as db:
        result = await db.execute(
            select(Case).where(
                (Case.policy_num == member_id) | (Case.exam_id == member_id)
            ).order_by(Case.ingested_at.desc()).limit(1)
        )
        c = result.scalar_one_or_none()
        if not c:
            return None

        # Load clinical context from extraction
        clinical_context = {}
        try:
            from app.db.models import ClinicalNote
            note_result = await db.execute(
                select(ClinicalNote).where(ClinicalNote.case_id == c.id)
                .order_by(ClinicalNote.created_at.desc()).limit(1)
            )
            note = note_result.scalar_one_or_none()
            if note and note.extracted_data:
                clinical_context = note.extracted_data if isinstance(note.extracted_data, dict) else json.loads(note.extracted_data)
        except Exception as e:
            logger.debug(f"No clinical notes for {member_id}: {e}")

        return {
            "id": c.id,
            "first_name": c.first_name,
            "last_name": c.last_name,
            "dob": c.dob,
            "policy_num": c.policy_num,
            "cpt_code": c.cpt_code,
            "icd1": c.icd1,
            "carrier_id": c.carrier_id,
            "center_npi": c.center_npi,
            "referring_npi": c.referring_npi,
            "referring_fax": c.referring_fax or "7322088110",
            "raw_data": c.raw_data or {},
            "clinical_context": clinical_context,
        }


async def run_single_case(page, session, case_data: dict, case_num: int, total: int) -> dict:
    """Run one case: member search → DI → start order → auths → provider → clinical → finalize."""
    from app.portal.webforms_client import WebFormsClient
    from app.portal.clinical_flow import ClinicalExamFlow
    from app.intelligence.answer_bridge import build_answer_fn

    name = f"{case_data['first_name']} {case_data['last_name']}"
    cpt = case_data["cpt_code"]
    icd = case_data["icd1"] or ""
    logger.info(f"\n{'='*60}")
    logger.info(f"CASE {case_num}/{total}: {name} — CPT {cpt}, ICD {icd}")
    logger.info(f"{'='*60}")

    result = {
        "member_id": case_data["policy_num"],
        "name": name,
        "cpt": cpt,
        "icd": icd,
        "algorithm_approved": None,
        "algorithm_recommendation": None,
        "exam_result": None,
        "exam_outcome": None,
        "is_exam_auto_approved": None,
        "questions_answered": 0,
        "rounds": 0,
        "pathway": None,
        "error": None,
    }

    try:
        # Update session center_npi for this case
        session.center_npi = case_data.get("center_npi") or "test"
        wf = WebFormsClient(session)

        # ── Navigate home ──
        await page.goto(f"{settings.CARELON_BASE_URL}/Default.aspx", wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(2)

        # Click My Homepage to reset
        try:
            home = page.locator("text=My Homepage")
            if await home.count() > 0:
                await home.first.click()
                await asyncio.sleep(2)
        except:
            pass

        # ── Member Search ──
        dob = case_data["dob"]
        if dob and hasattr(dob, "strftime"):
            dob = dob.strftime("%m/%d/%Y")
        dob = str(dob or "")

        r = await wf.search_member(
            first_name=case_data["first_name"],
            last_name=case_data["last_name"],
            dob=dob,
            policy_num=case_data["policy_num"],
        )
        if not r["ok"]:
            result["error"] = f"Member search: {r['message']}"
            return result
        logger.info(f"Member found")

        # ── Select DI ──
        r = await wf.select_diagnostic_imaging()
        if not r["ok"]:
            result["error"] = f"DI select: {r['message']}"
            return result

        await asyncio.sleep(1)

        # ── Start Order ──
        r = await wf.start_order_request()
        if not r["ok"]:
            result["error"] = f"Start order: {r['message']}"
            return result

        # ── Existing Auths → Next ──
        r = await wf.click_next_after_auths()
        if not r["ok"]:
            # Try extracting first, then click next
            try:
                await wf.extract_existing_auths()
                r = await wf.click_next_after_auths()
            except:
                pass
            if not r["ok"]:
                result["error"] = f"Next after auths: {r['message']}"
                return result

        # ── Provider Search ──
        if case_data.get("referring_npi"):
            fax = case_data.get("referring_fax", "7322088110")
            r = await wf.search_provider(
                referring_npi=case_data["referring_npi"],
                state="TX",
                fax=fax,
            )
            if not r["ok"]:
                logger.warning(f"Provider search issue: {r['message']}")
                # Non-fatal — continue

        # ── Clinical Init ──
        clinical = ClinicalExamFlow(session)
        r = await clinical.initialize()
        if not r["ok"]:
            result["error"] = f"Clinical init: {r['message']}"
            return result

        # ── Exam Setup ──
        r = await clinical.setup_exam(cpt_code=cpt)
        if not r["ok"]:
            result["error"] = f"Exam setup: {r['message']}"
            return result

        # ── Diagnosis ──
        if icd:
            r = await clinical.set_diagnosis(icd_code=icd)
            if not r["ok"]:
                result["error"] = f"Diagnosis: {r['message']}"
                return result

        # ── Pathway ──
        r = await clinical.select_pathway(icd_code=icd)
        if not r["ok"]:
            result["error"] = f"Pathway: {r['message']}"
            return result
        pathway_name = r.get("data", {}).get("pathway_name", "")
        result["pathway"] = pathway_name
        logger.info(f"Pathway: {pathway_name}")

        # ── Clinical Questions ──
        answer_fn = build_answer_fn(
            cpt_code=cpt,
            icd1=icd,
            carrier_id=case_data.get("carrier_id"),
            clinical_context=case_data.get("clinical_context", {}),
            pathway_name=pathway_name,
        )

        q_result = await clinical.run_clinical_questions_loop(
            answer_fn=answer_fn,
            max_rounds=20,
        )
        if not q_result["ok"]:
            result["error"] = f"Questions: {q_result['message']}"
            return result

        result["questions_answered"] = q_result["data"].get("total_answers", 0)
        result["rounds"] = q_result["data"].get("rounds", 0)

        # ── Algorithm recommendation from GPAWV ──
        result["algorithm_recommendation"] = clinical._algorithm_recommendation
        result["exam_result"] = clinical._exam_result
        result["exam_outcome"] = clinical._exam_outcome
        result["algorithm_approved"] = clinical._algorithm_recommendation == 3

        logger.info(
            f"ALGORITHM: RecType={clinical._algorithm_recommendation} "
            f"ExamResult={clinical._exam_result} ExamOutcome={clinical._exam_outcome} "
            f"→ {'✅ APPROVED' if result['algorithm_approved'] else '❌ NOT APPROVED'}"
        )

        # ── Finalize ──
        fin = await clinical.finalize(
            first_name=case_data.get("first_name", ""),
            last_name=case_data.get("last_name", ""),
            phone="",
            fax=case_data.get("referring_fax", "7322088110"),
        )
        if fin["ok"]:
            fd = fin["data"]
            result["is_exam_auto_approved"] = fd.get("auto_approved")
            result["algorithm_approved"] = fd.get("algorithm_approved")
            result["algorithm_recommendation"] = fd.get("algorithm_recommendation")
            logger.info(
                f"FINALIZE: algorithm_approved={fd.get('algorithm_approved')}, "
                f"is_exam_auto_approved={fd.get('auto_approved')}, cdo={fd.get('cdo_approved')}"
            )

        return result

    except Exception as e:
        result["error"] = str(e)
        logger.error(f"Case {name} EXCEPTION: {e}", exc_info=True)
        return result


async def main():
    from playwright.async_api import async_playwright
    from app.auth.okta_login import okta_login
    from app.portal.session import PlaywrightPortalSession
    from app.portal.behavior_engine import BehaviorEngine

    results = []
    start_time = datetime.now()

    logger.info(f"\n{'#'*60}")
    logger.info(f"# ALGORITHM VALIDATION — {len(TEST_MEMBER_IDS)} cases")
    logger.info(f"# Started: {start_time.isoformat()}")
    logger.info(f"{'#'*60}\n")

    # ── Load cases ──
    cases = []
    for mid in TEST_MEMBER_IDS:
        cd = await load_case(mid)
        if cd:
            cases.append(cd)
            logger.info(f"Loaded: {cd['first_name']} {cd['last_name']} — CPT {cd['cpt_code']}, ICD {cd.get('icd1','')}")
        else:
            logger.warning(f"NOT FOUND in DB: {mid}")
            results.append({"member_id": mid, "name": "NOT FOUND", "error": "Not in DB",
                          "algorithm_approved": None, "algorithm_recommendation": None})

    if not cases:
        logger.error("No cases — aborting")
        return

    # ── Browser + Login ──
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
    page = await ctx.new_page()

    logger.info("\n--- LOGIN ---")
    behavior = BehaviorEngine(page)
    try:
        provider_id, client_id = await okta_login(page, behavior)
        logger.info(f"Login SUCCESS. Provider: {provider_id}, Client: {client_id}\n")
    except Exception as e:
        logger.error(f"Login FAILED: {e}")
        await browser.close()
        await pw.stop()
        return

    # Agree to terms
    from app.portal.webforms_client import WebFormsClient
    session = PlaywrightPortalSession(
        context=ctx, page=page, center_npi="test",
        provider_id=provider_id, client_id=client_id,
    )
    try:
        wf_init = WebFormsClient(session)
        await wf_init.agree_to_terms()
    except:
        pass

    # ── Run each case ──
    for i, cd in enumerate(cases):
        try:
            r = await run_single_case(page, session, cd, i + 1, len(cases))
            results.append(r)
        except Exception as e:
            logger.error(f"Case {i+1} crashed: {e}", exc_info=True)
            results.append({
                "member_id": cd["policy_num"],
                "name": f"{cd['first_name']} {cd['last_name']}",
                "error": str(e),
                "algorithm_approved": None,
                "algorithm_recommendation": None,
            })
        await asyncio.sleep(2)

    await browser.close()
    await pw.stop()

    # ── Summary ──
    elapsed = (datetime.now() - start_time).total_seconds()
    logger.info(f"\n{'#'*60}")
    logger.info(f"# RESULTS — {elapsed:.0f}s total ({elapsed/max(len(results),1):.0f}s/case)")
    logger.info(f"{'#'*60}")

    approved = sum(1 for r in results if r.get("algorithm_approved"))
    not_approved = sum(1 for r in results if r.get("algorithm_approved") is False)
    errored = sum(1 for r in results if r.get("error"))

    for r in results:
        if r.get("error"):
            tag = "❌ ERROR"
        elif r.get("algorithm_approved"):
            tag = "✅ APPROVED"
        else:
            tag = "⚠️  NOT APPR"

        logger.info(
            f"  {tag:14s} | {r.get('name','?'):<22s} | CPT {r.get('cpt','?'):<6s} | "
            f"ICD {r.get('icd','?'):<10s} | RecType={r.get('algorithm_recommendation')} | "
            f"Pathway: {r.get('pathway','?')}"
        )
        if r.get("error"):
            logger.info(f"{'':16s}   └─ {r['error'][:80]}")

    logger.info(f"\n  ✅ APPROVED: {approved}/{len(results)}  |  ⚠️  NOT APPROVED: {not_approved}/{len(results)}  |  ❌ ERRORS: {errored}/{len(results)}")

    with open("/tmp/algorithm_validation_results.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    logger.info(f"  Results saved to /tmp/algorithm_validation_results.json")


if __name__ == "__main__":
    asyncio.run(main())
