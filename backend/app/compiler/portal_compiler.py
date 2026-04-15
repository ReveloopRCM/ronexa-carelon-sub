"""PortalCompiler — reads PortalDNA, drives submission through all phases.

Portal-agnostic. Carelon-specific logic belongs in the PortalDNA descriptor,
not here. Dispatches phases by type: API_SEQUENCE, WEBFORM, RECURSIVE_STATE_MACHINE.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.accumulator.answer_accumulator import AnswerAccumulator
from app.compiler.portal_dna import NavigationPhase, PhaseStep, PortalDNA
from app.core.settings import settings
from app.portal.clinical_flow import ClinicalExamFlow
from app.portal.session import PlaywrightPortalSession
from app.portal.webforms_client import WebFormsClient

logger = logging.getLogger(__name__)

PORTAL_REGISTRY: dict[str, str] = {
    "carelon_provider_portal": "portals/carelon_provider_portal.json",
}

# Full state name → 2-letter abbreviation (used by provider_search + facility_search)
_STATE_MAP = {
    "TEXAS": "TX", "COLORADO": "CO", "OKLAHOMA": "OK",
    "LOUISIANA": "LA", "UTAH": "UT", "FLORIDA": "FL",
    "CALIFORNIA": "CA", "NEW YORK": "NY", "ARIZONA": "AZ",
    "NEVADA": "NV", "NEW MEXICO": "NM", "KANSAS": "KS",
    "ARKANSAS": "AR", "MISSOURI": "MO", "TENNESSEE": "TN",
    "GEORGIA": "GA", "ALABAMA": "AL", "MISSISSIPPI": "MS",
    "ILLINOIS": "IL", "OHIO": "OH", "PENNSYLVANIA": "PA",
    "MICHIGAN": "MI", "INDIANA": "IN", "WISCONSIN": "WI",
    "MINNESOTA": "MN", "IOWA": "IA", "NEBRASKA": "NE",
    "NORTH CAROLINA": "NC", "SOUTH CAROLINA": "SC",
    "VIRGINIA": "VA", "WEST VIRGINIA": "WV", "MARYLAND": "MD",
    "NEW JERSEY": "NJ", "CONNECTICUT": "CT", "MASSACHUSETTS": "MA",
}


class PortalCompiler:
    """Reads PortalDNA and executes submission phases."""

    def __init__(self, dna: PortalDNA):
        self.dna = dna

    async def execute(
        self,
        case: dict,
        session: PlaywrightPortalSession,
        clinical_context: dict | None = None,
        resume_answers: list[dict] | None = None,
        changed_group_id: int | None = None,
        dry_run: bool = False,
        order_mode: bool = False,
    ) -> dict:
        """Execute all navigation phases for a case.

        Returns dict with outcome fields (auth_number, denial_reason, etc.)
        If clinical questions need rep review, returns {"answers": [...], "review_round": N}
        and the caller (workflow) handles the Restate awakeable/suspend.

        Args:
            resume_answers: Pre-approved answers from rep review. When provided,
                the question loop fast-forwards using these answers instead of
                calling the LLM — no early return, continues to finalize.
            changed_group_id: If set, the GroupId the rep edited. The resume
                path will backtrack (DeleteAssetsByGroupId for downstream groups)
                and re-process from this point. None = approved (no backtrack).
            dry_run: If True, stop after facility_search — don't submit.
                Use for testing the full flow without creating a real submission.
            order_mode: If True, use order-specific prompt templates for LLM
                evaluation. The clinical_context contains order form data.
        """
        result: dict[str, Any] = {}
        context_vars: dict[str, Any] = {
            "case": case,
            "provider_id": session.provider_id,
            "client_id": session.client_id,
            "order_mode": order_mode,
        }

        # Shared clinical flow instance — initialized once, used by all clinical phases
        clinical_flow = ClinicalExamFlow(session)
        context_vars["_clinical_flow"] = clinical_flow

        for phase_id in self.dna.state_machine.phases:
            # dry_run: stop before submission — everything up to facility is safe
            if dry_run and phase_id == "submit_and_extract":
                logger.info("DRY RUN — stopping before submit_and_extract")
                result["case_state"] = "DRY_RUN_COMPLETE"
                return result

            phase = self.dna.get_phase(phase_id)
            logger.info(f"Executing phase: {phase_id} ({phase.type}) — page URL: {session.page.url}")

            if phase.type == "API_SEQUENCE":
                phase_result = await self._run_api_sequence(
                    phase, case, session, context_vars, clinical_flow,
                    resume_answers=resume_answers,
                )
            elif phase.type == "WEBFORM":
                phase_result = await self._run_webform(
                    phase, case, session, context_vars
                )
            elif phase.type == "RECURSIVE_STATE_MACHINE":
                phase_result = await self._run_question_loop(
                    phase, case, session, clinical_context, None, context_vars,
                    resume_answers=resume_answers,
                    changed_group_id=changed_group_id,
                )
            else:
                raise ValueError(f"Unknown phase type: {phase.type}")

            # Check for terminal states — stop phase loop immediately
            if phase_result.get("case_state") in ("HOLD", "NO_AUTH_REQUIRED"):
                return phase_result

            # If answers need review, return for workflow:
            # - First pass (no resume_answers): always return for review
            # - Rerun (resume_answers + changed_group_id): return NEW downstream answers for review
            # - Submit (resume_answers, no changed_group_id): skip review, continue to finalize
            if phase_result.get("answers"):
                is_rerun = resume_answers and changed_group_id is not None
                if not resume_answers or is_rerun:
                    result.update(phase_result)

                    # ── Probe portal for auto-approval verdict before abandoning ──
                    # Calls ProcessAccepted + IsExamAutoApproved on the clinical
                    # SPA (no AddFeedback, no AddRadioTracers, no DoneWithExam).
                    # Non-fatal: if probe fails, answers are still returned.
                    try:
                        probe = await clinical_flow.probe_approval()
                        if probe.get("ok"):
                            probe_data = probe.get("data", {})
                            # algorithm_approved is the REAL signal (RecommendationType=3)
                            # auto_approved is just a UI flag (IsExamAutoApproved)
                            result["auto_approved"] = probe_data.get("algorithm_approved")
                            result["cdo_approved"] = probe_data.get("cdo_approved")
                            result["algorithm_recommendation"] = probe_data.get("algorithm_recommendation")
                            result["is_exam_auto_approved"] = probe_data.get("auto_approved")
                        else:
                            result["auto_approved"] = None
                            result["cdo_approved"] = None
                    except Exception as probe_err:
                        logger.warning(f"Approval probe failed (non-fatal): {probe_err}")
                        result["auto_approved"] = None
                        result["cdo_approved"] = None

                    # Propagate gold card level + bypass flag (set during clinical_diagnosis phase)
                    result["gold_card_level"] = getattr(clinical_flow, "gold_card_level", 0)
                    result["is_bypass"] = getattr(clinical_flow, "is_bypass", False)

                    return result

            # Merge output fields
            result.update(phase_result)
            context_vars.update(phase_result)

        return result

    async def _run_api_sequence(
        self,
        phase: NavigationPhase,
        case: dict,
        session: PlaywrightPortalSession,
        context_vars: dict,
        clinical_flow: ClinicalExamFlow,
        resume_answers: list[dict] | None = None,
    ) -> dict:
        """Delegate API_SEQUENCE phases to ClinicalExamFlow.

        Clinical phases use the proven ClinicalExamFlow methods instead of
        replaying raw API calls — they handle multi-step logic, contrast
        detection, body side selection, etc.
        """
        result: dict[str, Any] = {}
        phase_id = phase.id if hasattr(phase, "id") else ""

        if phase_id == "clinical_exam_setup":
            # Initialize clinical SPA
            r = await clinical_flow.initialize()
            if not r["ok"]:
                return {"case_state": "HOLD", "hold_reason": r["message"]}

            # ── Early duplicate auth check ──────────────────────────
            # Now that we have the CPT catalog (from initialize), check
            # if any existing auth matches our CPT before spending time
            # on exam setup, diagnosis, pathway, and LLM questions.
            existing_auths = context_vars.get("existing_auths", [])
            cpt_code = case.get("cpt_code", "")
            if existing_auths and cpt_code:
                # Look up our CPT's SearchText from the catalog
                cpt_search_text = None
                for cpt_entry in (r.get("data", {}).get("available_cpts") or []):
                    if cpt_entry.get("CptCode") == cpt_code:
                        cpt_search_text = cpt_entry.get("SearchText")
                        break

                from app.portal.clinical_flow import check_duplicate_auth
                dup_check = check_duplicate_auth(
                    existing_auths, cpt_code, cpt_search_text
                )
                if dup_check["duplicate"]:
                    logger.warning(
                        f"Duplicate auth detected early: {dup_check['reason']}"
                    )
                    return {
                        "case_state": "HOLD",
                        "hold_reason": dup_check["reason"],
                        "existing_auths": existing_auths,
                        "duplicate_check": dup_check,
                        "cpt_search_text": cpt_search_text,
                    }

                # Tier 2: 8-30 day warning — log but don't block
                if dup_check.get("recent_auth_warning"):
                    logger.info(
                        f"Recent auth warning (8-30 days): {dup_check['reason']}"
                    )
                    result["recent_auth_warning"] = dup_check["reason"]

                # Store for downstream use (workflow safety-net check)
                result["cpt_search_text"] = cpt_search_text
            # ────────────────────────────────────────────────────────

            # Derive contrast from CPT
            contrast_id, contrast_label = ClinicalExamFlow.derive_contrast_from_cpt(cpt_code)

            # Setup exam (CPT + body side + contrast + validate + add)
            r = await clinical_flow.setup_exam(
                cpt_code=cpt_code,
                contrast=contrast_label,
                contrast_id=contrast_id,
            )
            if not r["ok"]:
                return {"case_state": "HOLD", "hold_reason": r["message"]}
            result["contrast"] = contrast_label
            result.update(r.get("data", {}))

        elif phase_id == "clinical_diagnosis":
            icd_code = case.get("icd1", "")
            r = await clinical_flow.enter_diagnosis(icd_code)
            if not r["ok"]:
                return {"case_state": "HOLD", "hold_reason": r["message"]}
            result.update(r.get("data", {}))

        elif phase_id == "clinical_pathway":
            icd_code = case.get("icd1", "")

            # For reruns: if the rep changed the clinical scenario (GroupId=0),
            # use their chosen pathway_id instead of auto-selecting by ICD.
            rep_pathway_id = None
            if resume_answers:
                for ans in resume_answers:
                    qid = ans.get("QuestionId") or ans.get("question_id", "")
                    gid = ans.get("GroupId")
                    if gid is None:
                        gid = ans.get("group_id")
                    if str(qid) == "pathway_selection" or gid == 0:
                        # Rep's chosen pathway — could be in Values or answer_value
                        vals = ans.get("Values") or ans.get("answer_value")
                        if isinstance(vals, list) and vals:
                            rep_pathway_id = vals[0]
                        elif isinstance(vals, str) and vals:
                            rep_pathway_id = vals
                        logger.info(f"RE-RUN: using rep's pathway_id={rep_pathway_id}")
                        break

            r = await clinical_flow.select_pathway(
                preferred_icd=icd_code,
                preferred_pathway_id=rep_pathway_id,
            )
            if not r["ok"]:
                return {"case_state": "HOLD", "hold_reason": r["message"]}
            pathway_data = r.get("data", {})
            result.update(pathway_data)

            # Store pathway decision for inclusion in review questions.
            # This becomes "Question 0" — the clinical scenario selection.
            # ALWAYS store it when pathway succeeds — the rep needs to see
            # which clinical scenario was selected, regardless of option count.
            pathway_options = pathway_data.get("pathway_options", [])
            pathway_name = pathway_data.get("pathway_name", "")
            pathway_selected_id = pathway_data.get("pathway_selected_id", "")

            if not pathway_options:
                # Fallback: build a single-option list from the selected pathway
                # so the question still appears in review
                logger.warning(
                    f"clinical_pathway: pathway_options empty — "
                    f"building fallback from selected pathway: {pathway_name}"
                )
                if pathway_selected_id:
                    pathway_options = [{"id": pathway_selected_id, "text": pathway_name}]

            context_vars["_pathway_decision"] = {
                "question_id": "pathway_selection",
                "group_id": 0,
                "question_text": (
                    f"Select the clinical scenario that best matches "
                    f"diagnosis {icd_code} for CPT {case.get('cpt_code', '')}."
                ),
                "question_type": 3,  # single-select
                "options": pathway_options,
                "selected_id": pathway_selected_id,
                "selected_name": pathway_name,
            }
            logger.info(
                f"clinical_pathway: stored _pathway_decision "
                f"({len(pathway_options)} options, selected={pathway_name})"
            )

        elif phase_id == "clinical_complete":
            # HAR-proven flow: finalize determination on exam-entry SPA, then
            # hdnAction=20 postback to exam summary, then summary review APIs,
            # then hdnAction=6 postback to facility search.
            import asyncio as _asyncio

            wf = WebFormsClient(session)

            # Step 1: Finalize determination (ProcessAccepted → AddRadioTracers)
            # This runs on the exam-entry SPA page. Does NOT call DoneWithExam.
            r = await clinical_flow.finalize(
                first_name=case.get("first_name", ""),
                last_name=case.get("last_name", ""),
                phone=context_vars.get("patient_phone", ""),
                fax=case.get("raw_data", {}).get("ReferringProviderFax", "") if case.get("raw_data") else "",
            )
            if not r["ok"]:
                return {"case_state": "HOLD", "hold_reason": r["message"]}
            result.update(r.get("data", {}))

            # Propagate gold card level + bypass flag from clinical flow for approval tracking
            if hasattr(clinical_flow, "gold_card_level"):
                result["gold_card_level"] = clinical_flow.gold_card_level
                result["is_bypass"] = getattr(clinical_flow, "is_bypass", False)

            # Step 2: hdnAction=20 — transition from clinical SPA to exam summary page
            logger.info("Transitioning: clinical SPA → exam summary (hdnAction=20)")
            r = await wf.postback_hdnaction(20, timeout_ms=20000)
            if not r["ok"]:
                logger.warning(f"hdnAction=20 postback issue: {r['message']}")
                # Non-fatal — page may have loaded despite the warning

            # Brief extra settle for clinical SPA to initialize on summary page
            await _asyncio.sleep(3)

            try:
                await session.page.screenshot(path="/tmp/ronexa_exam_summary.png")
            except Exception:
                pass

            # Step 3: Exam summary review APIs (GetCase, DoneWithExam, FindNextExam, etc.)
            cpt_code = case.get("cpt_code", "")
            cpt_group = clinical_flow._find_cpt_group_id(cpt_code) if cpt_code else None
            r = await clinical_flow.exam_summary_review(
                cpt_code=cpt_code,
                cpt_group=cpt_group,
            )
            if not r["ok"]:
                return {"case_state": "HOLD", "hold_reason": r["message"]}
            result.update(r.get("data", {}))

            # Step 4: hdnAction=6 — transition from exam summary to facility search page
            # HAR shows this transition can take 25-35 seconds — use 60s timeout
            # Retry once if the page doesn't render (flaky portal transition)
            max_hdn6_attempts = 2
            for hdn6_attempt in range(1, max_hdn6_attempts + 1):
                logger.info(
                    f"Transitioning: exam summary → facility search (hdnAction=6) "
                    f"[attempt {hdn6_attempt}/{max_hdn6_attempts}]"
                )
                r = await wf.postback_hdnaction(6, timeout_ms=60000)
                if r["ok"]:
                    break
                if hdn6_attempt < max_hdn6_attempts:
                    logger.warning(
                        f"hdnAction=6 attempt {hdn6_attempt} failed: {r['message']} — retrying"
                    )
                    await _asyncio.sleep(3)
                    continue
                return {"case_state": "HOLD", "hold_reason": f"Failed to transition to facility search: {r['message']}"}

            await _asyncio.sleep(2)

            try:
                await session.page.screenshot(path="/tmp/ronexa_facility_page.png")
            except Exception:
                pass

            # Diagnose the page we landed on
            page_state = await session.page.evaluate("""() => ({
                url: window.location.href,
                bodySnippet: document.body.innerText.substring(0, 300),
                hasFacilitySearch: document.body.innerText.toLowerCase().includes('facility') ||
                                   !!document.querySelector('[id*="lbProviderSearchAdvanced"]'),
                hasAdvancedSearch: !!document.querySelector('[id*="lbProviderSearchAdvanced"]'),
            })""")
            logger.info(f"After hdnAction=6: {page_state}")

        else:
            # Fallback: raw API calls for unknown phases
            for step in phase.steps:
                if step.type == "api_call":
                    payload = self._resolve_template(step.payload_template, case, context_vars)
                    response = await session.api(step.endpoint, payload)
                    if step.extract_to and step.extract_path:
                        value = self._extract_path(response, step.extract_path)
                        context_vars[step.extract_to] = value
                        result[step.extract_to] = value
                elif step.type == "wait":
                    import asyncio
                    await asyncio.sleep((step.wait_ms or 1000) / 1000)

        return result

    async def _run_webform(
        self,
        phase: NavigationPhase,
        case: dict,
        session: PlaywrightPortalSession,
        context_vars: dict,
    ) -> dict:
        """Delegate WEBFORM phases to proven WebFormsClient methods.

        The PortalDNA defines WHAT to do, but the WebFormsClient knows HOW —
        handling auto-select, portal messages, existing auths, fax modals, etc.
        """
        wf = WebFormsClient(session)
        result: dict[str, Any] = {}
        phase_id = phase.id if hasattr(phase, "id") else ""

        if phase_id == "member_search":
            r = await wf.search_member(
                first_name=case.get("first_name", ""),
                last_name=case.get("last_name", ""),
                dob=case.get("dob", ""),
                policy_num=case.get("policy_num", ""),
            )
            if not r["ok"]:
                return {"case_state": "HOLD", "hold_reason": r["message"]}
            result.update(r.get("data", {}))

            from app.portal.clinical_flow import check_eligibility

            # ── Multi-plan eligibility: iterate grid rows to find an active plan ──
            results_count = r.get("data", {}).get("results_count", 1)
            auto_selected = r.get("data", {}).get("auto_selected", False)
            grid_visible = r.get("data", {}).get("grid_visible", False)

            if auto_selected:
                # Single result — portal auto-selected, extract eligibility directly
                elig = await wf.extract_eligibility_details()
                if elig.get("data"):
                    result["eligibility"] = elig["data"]
                    eff_date = elig["data"].get("effective_date")
                    term_date = elig["data"].get("term_date")
                    if eff_date:
                        elig_check = check_eligibility(eff_date, term_date)
                        result["eligibility_check"] = elig_check
                        if not elig_check["eligible"]:
                            return {
                                "case_state": "HOLD",
                                "hold_reason": f"Eligibility check failed: {elig_check['reason']}",
                                "eligibility": elig["data"],
                                "eligibility_check": elig_check,
                            }
                    if elig["data"].get("di_requires_auth") is False:
                        logger.info("DI does not require pre-auth for this member — stopping early")
                        blob_key = await _capture_no_auth_summary(session.page, case)
                        return {
                            "case_state": "NO_AUTH_REQUIRED",
                            "hold_reason": "DI does not require pre-authorization for this member's plan",
                            "no_auth_screenshot_key": blob_key,
                        }
            elif grid_visible and results_count > 0:
                # Multiple results — iterate each row looking for an eligible plan
                last_elig_check = None
                found_eligible = False

                for idx in range(results_count):
                    logger.info(f"Trying member result {idx + 1}/{results_count}")
                    sel_r = await wf.select_member_at_index(idx)
                    if not sel_r["ok"]:
                        logger.warning(f"Could not select member at index {idx}: {sel_r.get('message')}")
                        continue

                    # Extract eligibility for this plan
                    elig = await wf.extract_eligibility_details()
                    if not elig.get("data"):
                        logger.warning(f"No eligibility data for result {idx}")
                        # Navigate back to try next
                        if idx < results_count - 1:
                            back_r = await wf.navigate_back_to_member_results()
                            if not back_r["ok"]:
                                logger.warning("Cannot navigate back — using last result")
                                break
                        continue

                    eff_date = elig["data"].get("effective_date")
                    term_date = elig["data"].get("term_date")

                    if eff_date:
                        elig_check = check_eligibility(eff_date, term_date)
                        last_elig_check = elig_check
                        logger.info(
                            f"Plan {idx + 1}: eff={eff_date}, term={term_date}, "
                            f"eligible={elig_check['eligible']}"
                        )

                        if elig_check["eligible"]:
                            # Found an active plan — use it
                            result["eligibility"] = elig["data"]
                            result["eligibility_check"] = elig_check
                            result["selected_plan_index"] = idx
                            found_eligible = True
                            logger.info(f"Selected eligible plan at index {idx}")
                            break
                    else:
                        logger.warning(f"No effective date found for result {idx}")

                    # Not eligible — go back and try next
                    if idx < results_count - 1:
                        back_r = await wf.navigate_back_to_member_results()
                        if not back_r["ok"]:
                            logger.warning("Cannot navigate back — stopping iteration")
                            break

                if not found_eligible:
                    # All plans checked, none eligible
                    reason = last_elig_check["reason"] if last_elig_check else "No eligible plan found"
                    return {
                        "case_state": "HOLD",
                        "hold_reason": f"Eligibility check failed: {reason} (checked {results_count} plan(s))",
                        "eligibility": elig.get("data") if elig else {},
                        "eligibility_check": last_elig_check,
                    }

                # Gate: check if DI requires pre-auth for this member's plan
                if result.get("eligibility", {}).get("di_requires_auth") is False:
                    logger.info("DI does not require pre-auth for this member — stopping early")
                    blob_key = await _capture_no_auth_summary(session.page, case)
                    return {
                        "case_state": "NO_AUTH_REQUIRED",
                        "hold_reason": "DI does not require pre-authorization for this member's plan",
                        "no_auth_screenshot_key": blob_key,
                    }

        elif phase_id == "start_order":
            r = await wf.select_diagnostic_imaging()
            if not r["ok"]:
                return {"case_state": "HOLD", "hold_reason": r["message"]}

            # Step 1: Extract phone from portal (primary source)
            import re
            phone_r = await wf.extract_patient_phone()
            phone_val = phone_r.get("data", {}).get("phone", "")

            # Step 2: If portal phone is blank/invalid, use case data as fallback
            if not phone_val or not re.search(r'\d{3}', phone_val):
                case_phone = case.get("patient_phone", "")
                if case_phone and re.search(r'\d{3}', case_phone):
                    phone_val = case_phone
                    logger.info(f"Portal phone blank — using case phone: {phone_val}")

            # Step 3: If still no valid phone, HOLD for cure
            if phone_val and re.search(r'\d{3}', phone_val):
                result["patient_phone"] = phone_val
            else:
                logger.warning(f"Phone missing or invalid: '{phone_val}'")
                return {
                    "case_state": "HOLD",
                    "hold_reason": f"Phone number missing or invalid: '{phone_val}'",
                }

            r = await wf.start_order_request()
            if not r["ok"]:
                return {"case_state": "HOLD", "hold_reason": r["message"]}

        elif phase_id == "check_existing_auths":
            r = await wf.extract_existing_auths()
            auths_data = r.get("data", {})

            # Portal says no auth required for this member/procedure
            if auths_data.get("no_auth_required"):
                blob_key = await _capture_no_auth_summary(session.page, case)
                return {
                    "case_state": "NO_AUTH_REQUIRED",
                    "hold_reason": f"No auth required per portal: {auths_data.get('portal_message', '')[:150]}",
                    "no_auth_screenshot_key": blob_key,
                }

            if auths_data.get("auths"):
                result["existing_auths"] = auths_data["auths"]

            # Click Next only if we're actually on the auths page (not skipped)
            if auths_data.get("skipped") or auths_data.get("already_on_provider"):
                logger.info(f"Skipping Next click — already_on_provider={auths_data.get('already_on_provider')}, skipped={auths_data.get('skipped')}")
            else:
                r = await wf.click_next_after_auths()
                if not r["ok"]:
                    return {"case_state": "HOLD", "hold_reason": r["message"]}

        elif phase_id == "provider_search":
            raw = case.get("raw_data", {}) or {}
            referring_npi = case.get("referring_npi") or ""
            if not referring_npi:
                return {"case_state": "HOLD", "hold_reason": "Referring provider NPI is missing"}
            fax = raw.get("ReferringProviderFax", "") or case.get("referring_fax", "")
            match_address = raw.get("ReferringProviderAddress", "")
            match_name = f"{raw.get('ReferringProviderFirstName', '')} {raw.get('ReferringProviderLastName', '')}".strip()

            # Derive state from CenterState (full name → 2-letter code)
            center_state_raw = (raw.get("CenterState") or "").strip().upper()
            provider_state = _STATE_MAP.get(center_state_raw, center_state_raw)
            # If already a 2-letter code, use as-is. If unresolved, leave empty
            # (NPI alone is sufficient — don't hardcode a state for multi-state ops)
            if len(provider_state) != 2:
                provider_state = ""

            r = await wf.search_provider(
                referring_npi=referring_npi,
                state=provider_state,
                fax=fax,
                match_address=match_address,
                match_name=match_name,
            )
            if not r["ok"]:
                return {"case_state": "HOLD", "hold_reason": r["message"]}
            result.update(r.get("data", {}))

        elif phase_id == "facility_search":
            # We land here after hdnAction=6 postback (from clinical_complete).
            # The page should already be the facility search page.
            wf = WebFormsClient(session)
            page = session.page
            raw = case.get("raw_data", {}) or {}

            try:
                await page.screenshot(path="/tmp/ronexa_pre_facility.png")
            except Exception:
                pass

            # Extract center/facility data from MongoDB raw_data
            center_npi = case.get("center_npi", "")
            center_name = raw.get("CenterDesc", "")        # e.g. "Envision Imaging Flower Mound"
            center_address = raw.get("CenterAddress", "")   # e.g. "4640 Long Prairie Rd - Ste 310"
            fax = raw.get("ReferringProviderFax", "") or case.get("referring_fax", "")
            zip_code = raw.get("CenterZipCode", "")

            # State: full name → 2-letter abbreviation
            center_state_raw = (raw.get("CenterState") or "").strip().upper()
            state = _STATE_MAP.get(center_state_raw, center_state_raw)
            # If already 2-letter, use as-is. If unresolved, leave empty
            # (NPI alone is sufficient — don't hardcode a state for multi-state ops)
            if len(state) != 2:
                state = ""

            logger.info(
                f"Facility search: NPI={center_npi}, name={center_name}, "
                f"state={state}, zip={zip_code}, address={center_address}"
            )

            r = await wf.search_facility(
                center_npi=center_npi,
                state=state,
                facility_name=center_name,
                match_address=center_address,
                zip_code=zip_code,
                fax=fax,
            )
            if not r["ok"]:
                try:
                    await page.screenshot(path="/tmp/ronexa_facility_fail.png")
                except Exception:
                    pass
                return {"case_state": "HOLD", "hold_reason": r["message"]}
            result.update(r.get("data", {}))

        elif phase_id == "submit_and_extract":
            r = await wf.submit_request()
            if not r["ok"]:
                return {"case_state": "HOLD", "hold_reason": r["message"]}
            data = r.get("data", {})
            # Map portal confirmation fields to workflow-expected keys:
            #   order_id → portal_case_id, auth_number
            #   status → determination_status
            if data.get("order_id"):
                data["portal_case_id"] = data["order_id"]
                data["auth_number"] = data["order_id"]
            if data.get("status"):
                data["determination_status"] = data["status"]
                # Detect denial/pend from portal determination status
                status_lower = data["status"].lower()
                if "denied" in status_lower or "denial" in status_lower:
                    data["denial_reason"] = data["status"]
                elif "pend" in status_lower or "in progress" in status_lower or "review" in status_lower:
                    data["pend_reason"] = data["status"]
            result.update(data)
            logger.info(
                f"Submission result: order_id={data.get('order_id')}, "
                f"status={data.get('status')}, "
                f"valid_from={data.get('valid_from')}, "
                f"valid_through={data.get('valid_through')}"
            )

            # Capture confirmation page screenshot (always — for audit trail)
            try:
                order_id = data.get("order_id", "unknown")
                last = (case.get("last_name") or "UNKNOWN").upper()
                first = (case.get("first_name") or "UNKNOWN").upper()
                screenshot_path = f"/tmp/ronexa_confirmation_{last}_{first}_{order_id}.png"
                await session.page.screenshot(path=screenshot_path, full_page=True)
                logger.info(f"Confirmation screenshot saved: {screenshot_path}")

                # Upload screenshot to blob storage
                from app.ingest.blob_fetcher import upload_auth_pdf  # reuse blob upload
                from datetime import date
                screenshot_bytes = open(screenshot_path, "rb").read()
                blob_key = f"auth-confirmations/{date.today().isoformat()}/{last}_{first}_{order_id}.png"
                from app.ingest.blob_fetcher import _get_container
                from azure.storage.blob import ContentSettings
                container = _get_container()
                container.get_blob_client(blob_key).upload_blob(
                    screenshot_bytes, overwrite=True,
                    content_settings=ContentSettings(content_type="image/png"),
                )
                result["confirmation_screenshot_key"] = blob_key
                logger.info(f"Confirmation screenshot uploaded: {blob_key}")
            except Exception as e:
                logger.warning(f"Confirmation screenshot failed (non-fatal): {e}")

            # Download authorization PDF from confirmation page
            try:
                pdf_bytes = await wf.download_auth_pdf()
                if pdf_bytes and data.get("order_id"):
                    from app.ingest.blob_fetcher import upload_auth_pdf
                    blob_key = upload_auth_pdf(pdf_bytes, case, data["order_id"])
                    result["auth_pdf_blob_key"] = blob_key
                    logger.info(f"Auth PDF saved: {blob_key}")
            except Exception as e:
                logger.warning(f"Auth PDF download/upload failed (non-fatal): {e}")

            # For PENDED cases: flag for fax validation gate in workflow
            # (fax is now sent from case_workflow.py after optional rep validation)
            if data.get("pend_reason"):
                result["fax_needed"] = True

            # For PENDED cases: update Mongo with auth status
            if data.get("pend_reason"):
                try:
                    from app.ingest.mongo_poller import update_mongo_auth_status
                    cpt = case.get("cpt_code", "")
                    order_id = data.get("order_id", "")
                    det_date = data.get("valid_from", "")
                    exam_id = case.get("exam_id")
                    note = (
                        f"Per Carelon, CPT {cpt} pending; "
                        f"clinical notes faxed. "
                        f"Case {order_id} "
                        f"Determination date {det_date}"
                    )
                    import asyncio as _aio
                    await _aio.to_thread(update_mongo_auth_status,
                        exam_id=exam_id,
                        auth_state_desc="Auth Pending",
                        auth_state_sub_desc="Waiting On carrier",
                        auth_state_id=3,
                        workflow_note=note,
                        auth_number=order_id,
                    )
                    logger.info(f"Mongo updated for pended case: exam_id={exam_id}")
                except Exception as e:
                    logger.warning(f"Mongo update failed (non-fatal): {e}")

        else:
            logger.warning(f"Unknown WEBFORM phase '{phase_id}', falling back to raw DOM steps")
            for step in phase.steps:
                if step.type == "dom_action":
                    if step.action == "click":
                        await session.behavior.click(step.selector)
                    elif step.action == "type":
                        value = self._resolve_value(step.value_template, case, context_vars)
                        await session.behavior.type_text(step.selector, value)
                elif step.type == "wait":
                    await session.behavior.think("pageLoad")

        return result

    async def _run_question_loop(
        self,
        phase: NavigationPhase,
        case: dict,
        session: PlaywrightPortalSession,
        clinical_context: dict | None,
        restate_ctx: Any,
        context_vars: dict,
        resume_answers: list[dict] | None = None,
        changed_group_id: int | None = None,
    ) -> dict:
        """CLINICAL_TREE execution — batch review with backtrack.

        Uses ClinicalExamFlow for portal API calls (proven question parsing),
        with AnswerAccumulator for backtrack support.

        Flow (first pass — resume_answers is None):
          1. LLM answers ALL questions in one pass (no per-question pause)
          2. Return answers → workflow saves to DB + suspends at awakeable
          3. Rep reviews all, approves or edits

        Flow (resume — resume_answers provided, changed_group_id is None):
          1. Pre-load all approved answers into accumulator
          2. Submit to portal in one shot → portal fast-forwards through tree
          3. Verify "done" signal → continue to finalize

        Flow (backtrack — resume_answers provided, changed_group_id set):
          1. Add answers for GroupId < changed_group_id to accumulator
          2. Call accumulator.change() for the changed GroupId — deletes downstream
          3. Submit to portal → portal re-processes from changed point
          4. New downstream questions → LLM answers → return for review
        """
        from app.intelligence.evaluator import decide_answer
        from app.intelligence.models import PortalObservation

        clinical_flow = context_vars.get("_clinical_flow")
        if not clinical_flow:
            clinical_flow = ClinicalExamFlow(session)

        accumulator = AnswerAccumulator()
        result: dict[str, Any] = {}
        delete_endpoint = phase.delete_endpoint

        # ---- Resume path: fast-forward or backtrack with approved answers ----
        new_questions_from_backtrack = None  # Set if backtrack produces new questions

        if resume_answers:
            logger.info(
                f"Question loop RESUME: {len(resume_answers)} answers, "
                f"changed_group_id={changed_group_id}"
            )

            # Safety filter: skip any synthetic GroupId=0 / pathway_selection
            # entries from old Restate journal entries (before this fix)
            filtered_answers = []
            for ans in resume_answers:
                qid = ans.get("QuestionId") or ans.get("question_id", "")
                gid = ans.get("GroupId")
                if gid is None:
                    gid = ans.get("group_id")
                if str(qid) == "pathway_selection" or gid == 0:
                    logger.info(f"Safety filter: skipping synthetic answer (qid={qid}, gid={gid})")
                    continue
                filtered_answers.append(ans)
            resume_answers = filtered_answers

            # Normalize answers to portal format:
            #   {QuestionId, QuestionType, GroupId, Sequence, Values}
            portal_answers = []
            for ans in resume_answers:
                # If the answer has portal_answer (from first-pass compiler output), use it
                portal_ans = ans.get("portal_answer") if isinstance(ans, dict) else None
                if portal_ans and isinstance(portal_ans, dict) and "Values" in portal_ans:
                    portal_answers.append(portal_ans)
                elif isinstance(ans, dict) and "Values" in ans:
                    # Already in portal format (from queue.py resolve)
                    portal_answers.append(ans)
                else:
                    # Reconstruct from whatever format we have
                    val = ans.get("Values") or ans.get("Value") or ans.get("answer_value", "")
                    if not isinstance(val, list):
                        val = [val] if val else []
                    portal_answers.append({
                        "QuestionId": ans.get("QuestionId") or ans.get("question_id", ""),
                        "QuestionType": ans.get("QuestionType") or ans.get("question_type", 3),
                        "GroupId": ans.get("GroupId") if ans.get("GroupId") is not None else ans.get("group_id", 1),
                        "Sequence": ans.get("Sequence") or ans.get("sequence", 0),
                        "Values": val,
                    })

            if changed_group_id is not None:
                # ---- BACKTRACK path: rep edited a question ----
                logger.info(f"BACKTRACK: rep edited GroupId={changed_group_id}")

                # Step 1: Add answers BEFORE the changed group to accumulator
                for pa in portal_answers:
                    pa_gid = pa.get("GroupId", 0)
                    if pa_gid < changed_group_id:
                        accumulator.add(pa)
                        logger.info(f"  Backtrack: added GroupId={pa_gid} (unchanged)")

                # Step 2: Use accumulator.change() for the edited group
                # This calls DeleteAssetsByGroupId for all downstream groups
                changed_answer = None
                for pa in portal_answers:
                    if pa.get("GroupId") == changed_group_id:
                        changed_answer = pa
                        break

                if changed_answer:
                    await accumulator.change(changed_answer, session, delete_endpoint)
                    logger.info(
                        f"  Backtrack: changed GroupId={changed_group_id}, "
                        f"downstream deleted, accumulator count={accumulator.count}"
                    )
                else:
                    logger.error(f"  Backtrack: no answer found for changed GroupId={changed_group_id}")
                    return {"case_state": "HOLD", "hold_reason": f"No answer for changed GroupId={changed_group_id}"}

                # Step 3: Submit accumulated answers to portal — portal re-processes
                next_result = await clinical_flow.answer_questions(accumulator.payload)

                if not next_result["ok"]:
                    logger.error(f"Backtrack submission failed: {next_result['message']}")
                    return {"case_state": "HOLD", "hold_reason": next_result["message"]}

                # Step 4: Check for new downstream questions
                remaining = next_result["data"].get("questions", [])
                done = next_result["data"].get("done", False)

                if remaining and not done:
                    logger.info(
                        f"Backtrack produced {len(remaining)} new downstream questions — "
                        f"falling through to LLM path"
                    )
                    new_questions_from_backtrack = remaining
                    # Fall through to LLM while-loop below
                elif done:
                    logger.info("Backtrack complete — portal at done state (no new questions)")
                    return result
                else:
                    logger.info("Backtrack complete — no remaining questions")
                    return result

            else:
                # ---- APPROVED path: step-through with answer matching ----
                # Instead of dumping all answers at once, walk through questions
                # one group at a time (same as first pass) and use match_answer()
                # to confirm each question matches a saved answer. New/unmatched
                # questions → HOLD the case.
                from app.intelligence.evaluator import match_answer

                remaining_saved = list(portal_answers)

                q_result = await clinical_flow.get_questions()
                if not q_result["ok"]:
                    logger.error(f"Submit step-through: initial get_questions failed: {q_result['message']}")
                    return {"case_state": "HOLD", "hold_reason": q_result["message"]}

                questions = q_result["data"].get("questions", [])

                # Process hidden auto-fills (ForDisplay=False) — same as first pass
                hidden = q_result["data"].get("hidden_questions", [])
                pre_filled = clinical_flow.answers
                if hidden:
                    logger.info(f"Submit step-through: {len(hidden)} hidden auto-fill questions")
                    for hq in hidden:
                        hq_id = str(hq.get("Id") or hq.get("QuestionId", ""))
                        hq_gid = hq.get("GroupId")
                        if hq_gid is None:
                            hq_gid = hq.get("group_id", 0)
                        # Check if portal pre-filled an answer for this hidden question
                        pf_answer = None
                        for pf in pre_filled:
                            if str(pf.get("QuestionId", "")) == hq_id:
                                pf_answer = pf
                                break
                        if pf_answer:
                            accumulator.add(pf_answer)

                submit_iteration = 0
                while questions and submit_iteration < 20:
                    submit_iteration += 1
                    questions = accumulator.get_new_groups(questions)
                    if not questions:
                        break

                    logger.info(
                        f"Submit step-through iteration {submit_iteration}: "
                        f"{len(questions)} question(s)"
                    )

                    batch_answers = []
                    for q in questions:
                        # Parse question fields (same as first-pass)
                        q_id = str(q.get("Id") or q.get("QuestionId", ""))
                        group_id = q.get("GroupId")
                        if group_id is None:
                            group_id = q.get("group_id")
                        question_type = q.get("QuestionType") or q.get("question_type") or q.get("Type", 3)
                        question_text = q.get("Text") or q.get("text", "")
                        if isinstance(question_text, dict):
                            question_text = question_text.get("Base", str(question_text))

                        raw_options = q.get("Options") or q.get("options", [])
                        options = []
                        for opt in raw_options:
                            opt_id = opt.get("Id") or opt.get("id", "")
                            opt_text = opt.get("Text", "")
                            if isinstance(opt_text, dict):
                                opt_text = opt_text.get("Base", str(opt_text))
                            options.append({"id": opt_id, "text": opt_text})

                        # MATCH saved answer instead of LLM DECIDE
                        match = await match_answer(
                            question_id=q_id,
                            question_text=question_text,
                            question_type=int(question_type),
                            options=options,
                            saved_answers=remaining_saved,
                        )

                        if not match.matched:
                            logger.warning(
                                f"Submit step-through: UNMATCHED question — "
                                f"Q={q_id[:12]}... text='{question_text[:80]}' "
                                f"reason={match.reasoning}"
                            )
                            return {
                                "case_state": "HOLD",
                                "hold_reason": (
                                    f"Unmatched question during submission: "
                                    f"{question_text[:100]}"
                                ),
                            }

                        logger.info(
                            f"Submit match: {match.match_type} "
                            f"(conf={match.confidence}) "
                            f"Q={q_id[:8]}..."
                        )

                        # Use CURRENT portal QuestionId + saved Values
                        answer_dict = {
                            "QuestionId": q_id,
                            "QuestionType": int(question_type),
                            "GroupId": int(group_id),
                            "Sequence": q.get("Sequence", 0),
                            "Values": match.saved_answer["Values"],
                        }
                        accumulator.add(answer_dict)
                        batch_answers.append(answer_dict)

                    # Submit batch and get next questions
                    next_result = await clinical_flow.answer_questions(batch_answers)
                    if not next_result["ok"]:
                        logger.error(f"Submit step-through: answer_questions failed: {next_result['message']}")
                        return {"case_state": "HOLD", "hold_reason": next_result["message"]}

                    questions = next_result["data"].get("questions", [])
                    if next_result["data"].get("done"):
                        break

                logger.info(
                    f"Submit step-through complete: {submit_iteration} iterations, "
                    f"{accumulator.count} answers"
                )
                return result  # Proceed to finalize

        # ---- Pathway is case metadata, NOT a question ----
        # GetPathwayOptions + SetPathway is a separate API call, not part of
        # the question/answer flow. Store as result metadata for the case record.
        pw_dec = context_vars.get("_pathway_decision")
        if pw_dec:
            result["pathway"] = {
                "name": pw_dec["selected_name"],
                "id": pw_dec["selected_id"],
                "options": pw_dec["options"],
            }
            logger.info(
                f"Pathway metadata: '{pw_dec['selected_name']}' "
                f"from {len(pw_dec['options'])} options"
            )

        # ---- Build rep_answers for LLM context (standardized input) ----
        # For first pass: rep_answers_for_llm is None — LLM evaluates fresh.
        # For reruns: load ALL rep-approved answers so the LLM has full context
        # from the first pass (clinical + RAG + signature + approved Q&A).
        rep_answers_for_llm: list[dict] | None = None
        if resume_answers and changed_group_id is not None:
            rep_answers_for_llm = []
            for ans in resume_answers:
                gid = ans.get("GroupId")
                if gid is None:
                    gid = ans.get("group_id")
                rep_answers_for_llm.append({
                    "group_id": int(gid) if gid is not None else 0,
                    "question_id": ans.get("QuestionId") or ans.get("question_id", ""),
                    "question_text": ans.get("question_text", ""),
                    "answer_value": (ans.get("Values") or ans.get("Value")
                                     or ans.get("answer_value", "")),
                    "answer_text": ans.get("answer_text", ""),
                })
            logger.info(
                f"RE-RUN: {len(rep_answers_for_llm)} rep answers loaded "
                f"(all approved Q&A for LLM context)"
            )

        # ── Load algorithm signature for this CPT+ICD (if exists) ──
        # Provides proven Q&A answers as strong LLM guidance. Both clinical
        # and order-only pipelines benefit. Empty table = no-op.
        signature_answers = None
        try:
            from app.db.database import async_session_factory as sig_session_factory
            from app.db.outcome_db import get_signature_for_case
            async with sig_session_factory() as sig_db:
                sig = await get_signature_for_case(
                    sig_db, case.get("cpt_code"), case.get("icd1"),
                )
                if sig:
                    signature_answers = sig.qa_sequence
                    logger.info(
                        f"Loaded algorithm signature {sig.id[:8]} for "
                        f"{case.get('cpt_code')}/{case.get('icd1')} "
                        f"({len(sig.qa_sequence)} Q&As, pathway={sig.pathway_id})"
                    )
        except Exception as sig_err:
            logger.debug(f"Signature lookup failed (non-fatal): {sig_err}")

        # ---- LLM answers questions (first pass or backtrack new questions) ----
        review_round = 0
        all_decisions: list[tuple] = []

        while True:
            review_round += 1

            # ---- Phase A: LLM answers all questions to completion ----
            all_decisions = []  # Reset for this round
            iteration = 0

            if new_questions_from_backtrack is not None:
                # Backtrack produced new downstream questions — use them directly
                questions = new_questions_from_backtrack
                hidden = []  # Hidden questions already processed in original pass
                new_questions_from_backtrack = None  # Consume once
                logger.info(f"Using {len(questions)} new questions from backtrack")
            else:
                # Normal first pass — fetch questions from portal
                q_result = await clinical_flow.get_questions()
                if not q_result["ok"]:
                    return {"case_state": "HOLD", "hold_reason": q_result["message"]}

                questions = q_result["data"].get("questions", [])
                hidden = q_result["data"].get("hidden_questions", [])

            # ── Capture hidden auto-fills (DOB, CPT text) ──
            # ForDisplay=False questions are trusted as-is (portal auto-filled).
            # They're only QType 1=text, 2=date — DOB confirmation, CPT code echo.
            # These MUST be in the accumulator for cumulative API submission
            # AND in the DB so the resume/finalize path has them.
            pre_filled = clinical_flow.answers  # portal's pre-filled answer objects
            if hidden:
                logger.info(
                    f"Processing {len(hidden)} hidden questions "
                    f"(pre-filled={len(pre_filled)})"
                )
                for hq in hidden:
                    hq_group = hq.get("GroupId")
                    if hq_group is None:
                        hq_group = hq.get("group_id")
                    hq_id = str(hq.get("Id") or hq.get("QuestionId", ""))

                    # Find the pre-filled answer for this question
                    prefill_match = None
                    for pf in pre_filled:
                        if str(pf.get("QuestionId", "")) == hq_id:
                            prefill_match = pf
                            break

                    if prefill_match:
                        # Build normalized answer from hidden question metadata
                        # + pre-fill values. The hidden question asset has GroupId,
                        # QuestionType, Sequence. The pre-fill has the actual values.
                        hq_qtype = int(hq.get("QuestionType", 0))
                        hq_seq = hq.get("Sequence", 0)

                        # Extract values — pre-fills may use different field names
                        pf_values = prefill_match.get("Values")
                        if not pf_values:
                            pf_val = prefill_match.get("Value")
                            if pf_val:
                                pf_values = [pf_val]
                            else:
                                selected = (prefill_match.get("SelectedAnswers")
                                            or prefill_match.get("SelectedValues") or [])
                                pf_values = [str(s.get("Id", "")) for s in selected if s.get("Id")]
                        if not pf_values:
                            pf_values = []

                        normalized_ans = {
                            "QuestionId": hq_id,
                            "QuestionType": hq_qtype,
                            "GroupId": int(hq_group) if hq_group is not None else 0,
                            "Sequence": hq_seq,
                            "Values": pf_values,
                        }
                        # Carry over measure unit fields (DOB/date)
                        mu_id = prefill_match.get("MeasureUnitId")
                        if mu_id:
                            normalized_ans["MeasureUnitId"] = mu_id
                        mu_vals = prefill_match.get("MeasureUnitValues")
                        if mu_vals:
                            normalized_ans["MeasureUnitValues"] = mu_vals

                        accumulator.add(normalized_ans)
                        logger.info(
                            f"Hidden Group {hq_group}: normalized pre-fill → accumulator "
                            f"(QType={hq_qtype} Values={pf_values})"
                        )

                        # NOTE: Hidden auto-fills (DOB, Client Id, CPT Code) are
                        # intentionally NOT added to all_decisions. They're in the
                        # accumulator for portal API submission, but reps don't need
                        # to review portal-managed values. Only real clinical
                        # questions go to the review UI.
                    else:
                        logger.warning(
                            f"Hidden Group {hq_group}: no pre-fill match for QId={hq_id[:12]}..."
                        )

            while questions and iteration < 20:
                iteration += 1
                logger.info(
                    f"Question loop round {review_round}, iteration {iteration}, "
                    f"{len(questions)} question(s)"
                )

                # Filter out already-answered groups (dedup by GroupId)
                questions = accumulator.get_new_groups(questions)
                if not questions:
                    break

                # Build answers for this batch via LLM
                batch_answers = []
                for q in questions:
                    group_id = q.get("GroupId")
                    if group_id is None:
                        group_id = q.get("group_id")
                    question_type = q.get("QuestionType") or q.get("question_type") or q.get("Type", 3)
                    question_text = q.get("Text") or q.get("text", "")

                    # Parse options from ClinicalExamFlow's format
                    raw_options = q.get("Options") or q.get("options", [])
                    options = []
                    for opt in raw_options:
                        opt_id = opt.get("Id") or opt.get("id", "")
                        opt_text = opt.get("Text", "")
                        if isinstance(opt_text, dict):
                            opt_text = opt_text.get("Base", str(opt_text))
                        options.append({"id": opt_id, "text": opt_text})

                    observation = PortalObservation(
                        question_id=str(q.get("Id") or q.get("QuestionId", "")),
                        group_id=int(group_id),
                        question_text=question_text,
                        question_type=int(question_type),
                        options=options,
                        cpt_code=case.get("cpt_code", ""),
                        icd1=case.get("icd1"),
                        carrier_id=case.get("carrier_id"),
                        sequence=q.get("Sequence", 0),
                    )

                    # RAG retrieval — find similar past questions for context
                    rag_examples = ""
                    try:
                        from app.intelligence.rag import retrieve_similar_cases, format_rag_examples
                        from app.db.database import async_session_factory as rag_session_factory
                        async with rag_session_factory() as rag_db:
                            similar = await retrieve_similar_cases(
                                question_text=observation.question_text,
                                cpt_code=case.get("cpt_code", ""),
                                carrier_id=case.get("carrier_id"),
                                db=rag_db,
                            )
                            rag_examples = format_rag_examples(similar)
                            if rag_examples:
                                logger.info(f"RAG: {len(similar)} similar patterns for Q '{observation.question_text[:50]}...'")
                    except Exception as rag_err:
                        logger.debug(f"RAG lookup failed (non-fatal): {rag_err}")

                    # Build chain-of-thought context from prior decisions
                    prev_answers_ctx = []
                    for idx, (prev_obs, prev_dec) in enumerate(all_decisions):
                        answer_text = str(prev_dec.answer_value)
                        # Resolve answer UUID to human-readable option text
                        if isinstance(prev_dec.answer_value, str):
                            for o in prev_obs.options:
                                if o["id"] == prev_dec.answer_value:
                                    answer_text = o["text"]
                                    break
                        elif isinstance(prev_dec.answer_value, list):
                            texts = []
                            for av in prev_dec.answer_value:
                                for o in prev_obs.options:
                                    if o["id"] == av:
                                        texts.append(o["text"])
                                        break
                            if texts:
                                answer_text = "; ".join(texts)
                        prev_answers_ctx.append({
                            "question_number": idx + 1,
                            "question_text": prev_obs.question_text,
                            "answer_text": answer_text,
                            "reasoning": prev_dec.reasoning or "",
                        })

                    # Get pathway name from context
                    pw_decision = context_vars.get("_pathway_decision", {})
                    pw_name = pw_decision.get("selected_name", "")

                    decision = await decide_answer(
                        observation=observation,
                        clinical_context=clinical_context or {},
                        multi_select=(int(question_type) == 4),
                        rag_examples=rag_examples,
                        rep_answers=rep_answers_for_llm,
                        changed_group_id=changed_group_id,
                        pathway_name=pw_name,
                        previous_answers=prev_answers_ctx if prev_answers_ctx else None,
                        order_mode=context_vars.get("order_mode", False),
                        signature_answers=signature_answers,
                    )

                    all_decisions.append((observation, decision))
                    answer_dict = decision.to_portal_answer()
                    accumulator.add(answer_dict)
                    batch_answers.append(answer_dict)

                # Submit answers via ClinicalExamFlow (handles accumulation + next batch)
                next_result = await clinical_flow.answer_questions(batch_answers)
                if not next_result["ok"]:
                    break

                questions = next_result["data"].get("questions", [])
                if next_result["data"].get("done"):
                    break

            logger.info(
                f"All questions answered: round {review_round}, "
                f"{iteration} iterations, {len(all_decisions)} decisions"
            )

            # Settle time — like a rep reviewing the completed questions
            # before navigating away. Gives the portal SPA time to finalize.
            import asyncio, random
            settle = random.gauss(3.0, 0.8)
            settle = max(2.0, min(5.0, settle))
            logger.info(f"Post-questions settle: {settle:.1f}s")
            await asyncio.sleep(settle)

            # ---- Phase B: Return decisions for workflow to handle ----
            if all_decisions:
                # Serialize decisions and return — workflow handles awakeable/suspend
                serializable_decisions = [
                    {
                        "question_id": obs.question_id,
                        "group_id": obs.group_id,
                        "question_text": obs.question_text,
                        "question_type": obs.question_type,
                        "options": obs.options,
                        "portal_answer": dec.to_portal_answer(),
                        "confidence": dec.confidence,
                        "evidence": dec.evidence,
                        "reasoning": dec.reasoning,
                        "gap": dec.gap,
                        # Pathway + chain-of-thought (v2 prompts)
                        "pathway_rationale": dec.pathway_rationale,
                        "chain_coherence": dec.chain_coherence,
                        # Dual-answer: notes path
                        "notes_answer_value": dec.notes_answer_value,
                        "notes_confidence": dec.notes_confidence,
                        "notes_reasoning": dec.notes_reasoning,
                        "approval_gap": dec.approval_gap,
                    }
                    for obs, dec in all_decisions
                ]
                result["answers"] = serializable_decisions
                result["review_round"] = review_round
                # Return to workflow — it will save to DB and create awakeable
                return result

            # No decisions needed — continue
            break

        # Save answer metadata for downstream checks
        result["answers"] = [
            {
                "group_id": obs.group_id,
                "question_text": obs.question_text,
                "ai_confidence": dec.confidence,
                "ai_evidence": dec.evidence,
            }
            for obs, dec in all_decisions
        ]

        return result

    def _resolve_template(
        self,
        template: dict | None,
        case: dict,
        context_vars: dict,
    ) -> dict:
        """Resolve template variables in a payload template."""
        if not template:
            return {}
        result = {}
        for key, value in template.items():
            if isinstance(value, str) and value.startswith("${"):
                var_name = value[2:-1]
                if var_name.startswith("case."):
                    result[key] = case.get(var_name[5:])
                else:
                    result[key] = context_vars.get(var_name)
            elif isinstance(value, dict):
                result[key] = self._resolve_template(value, case, context_vars)
            else:
                result[key] = value
        return result

    def _resolve_value(
        self,
        template: str | None,
        case: dict,
        context_vars: dict,
    ) -> str:
        """Resolve a single template value."""
        if not template:
            return ""
        if template.startswith("${case."):
            return str(case.get(template[7:-1], ""))
        if template.startswith("${"):
            return str(context_vars.get(template[2:-1], ""))
        return template

    def _extract_path(self, data: dict, path: str) -> Any:
        """Extract a value from a dict using dot-notation path."""
        parts = path.split(".")
        current = data
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part)
            else:
                return None
        return current


async def _capture_no_auth_summary(page, case: dict) -> str | None:
    """Capture Order Summary screenshot and upload to blob storage.

    Called when portal determines DI does not require pre-authorization.
    Returns blob key on success, None on failure (non-fatal).
    """
    try:
        from datetime import date

        from azure.storage.blob import ContentSettings

        from app.ingest.blob_fetcher import _get_container

        last = (case.get("last_name") or "UNKNOWN").upper().replace(" ", "_")
        first = (case.get("first_name") or "UNKNOWN").upper().replace(" ", "_")
        exam_id = case.get("exam_id", "unknown")

        screenshot_bytes = await page.screenshot(full_page=True)
        blob_key = f"no-auth-summaries/{date.today().isoformat()}/{last}_{first}_{exam_id}.png"

        container = _get_container()
        container.get_blob_client(blob_key).upload_blob(
            screenshot_bytes,
            overwrite=True,
            content_settings=ContentSettings(content_type="image/png"),
        )
        logger.info(f"NO_AUTH screenshot uploaded: {blob_key} ({len(screenshot_bytes)} bytes)")
        return blob_key
    except Exception as e:
        logger.warning(f"NO_AUTH screenshot capture failed (non-fatal): {e}")
        return None


def load_compiler(portal_id: str) -> PortalCompiler:
    """Load a PortalCompiler from the registry."""
    if portal_id not in PORTAL_REGISTRY:
        raise ValueError(f"Unknown portal: {portal_id}")
    path = Path(PORTAL_REGISTRY[portal_id])
    dna = PortalDNA.load(path)
    return PortalCompiler(dna)


async def _save_question_batch(
    case_id: str,
    decisions: list[dict],
    awakeable_id: str | None = None,
    review_round: int = 1,
    auto_approved: bool | None = None,
    gold_card_level: int | None = None,
    algorithm_recommendation: int | None = None,
    is_rerun: bool = False,
) -> None:
    """Save all questions from a round to DB + transition case to review/submit.

    Called inside restate_ctx.run() for durability. awakeable_id is legacy
    (two-job model doesn't use awakeables — kept for backward compat).

    Args:
        decisions: list of plain dicts (JSON-serializable for Restate journaling),
                   each with keys: question_id, group_id, question_text, question_type,
                   options, portal_answer, confidence, evidence, reasoning, gap.
        awakeable_id: DEPRECATED — ignored in two-job model. Pass None.
    """
    from app.db.database import async_session_factory
    from app.db import repositories as repo
    from app.db.models import CaseState, ReviewState

    async with async_session_factory() as db:
        # Clean up old questions before saving new batch.
        # - review_round > 1: backtrack round — delete stale AI_SUGGESTED only
        # - review_round == 1 with existing questions: re-run — delete ALL old
        #   questions (rep's answers already loaded into rerun_rep_answers payload)
        existing = await repo.get_questions_for_case(db, case_id)
        if existing:
            if review_round > 1:
                for q in existing:
                    if q.review_state == ReviewState.AI_SUGGESTED:
                        await db.delete(q)
            else:
                # Fresh re-run: clear all prior questions to avoid duplicates
                for q in existing:
                    await db.delete(q)
                logger.info(
                    f"_save_question_batch/{case_id}: cleared {len(existing)} "
                    f"old questions before saving {len(decisions)} new ones"
                )
            await db.flush()

        for i, d in enumerate(decisions):
            q_kwargs = dict(
                case_id=case_id,
                portal_question_id=str(d["question_id"]),
                group_id=d["group_id"],
                sequence=i + 1,
                question_type=d["question_type"],
                question_text=d["question_text"],
                options_json=d["options"],
                ai_answer=d["portal_answer"],
                ai_confidence=d["confidence"],
                ai_evidence=d["evidence"],
                ai_reasoning=d["reasoning"],
                ai_gap=d.get("pathway_rationale") or d.get("gap"),
                # Dual-answer: notes path
                ai_notes_answer=d.get("notes_answer_value"),
                ai_notes_confidence=d.get("notes_confidence"),
                ai_notes_reasoning=d.get("chain_coherence") or d.get("notes_reasoning"),
                ai_approval_gap=d.get("approval_gap"),
                review_state=ReviewState.AI_SUGGESTED,
            )
            if awakeable_id:
                q_kwargs["awakeable_id"] = awakeable_id
            await repo.create_question(db, **q_kwargs)

        # Determine review level based on bypass rules + settings
        from app.db.models import SystemSetting, BypassRule
        from sqlalchemy import select

        # Read settings
        l1_setting = await db.get(SystemSetting, "l1_review_enabled")
        l2_setting = await db.get(SystemSetting, "l2_review_enabled")
        bypass_setting = await db.get(SystemSetting, "bypass_enabled")

        l1_enabled = l1_setting.value if l1_setting else True
        l2_enabled = l2_setting.value if l2_setting else True
        bypass_enabled = bypass_setting.value if bypass_setting else False

        # Check bypass rules for this CPT+ICD
        case = await repo.get_case(db, case_id)
        target_state = CaseState.L1_REVIEW  # default

        # Persist approval probe results from first pass (if available)
        if case:
            if auto_approved is not None:
                case.auto_approved = auto_approved
            if gold_card_level is not None:
                case.gold_card_level = gold_card_level
            if algorithm_recommendation is not None:
                case.algorithm_recommendation = algorithm_recommendation
            if (gold_card_level or 0) >= 2:
                case.approval_type = "gold_card"
            elif auto_approved:
                case.approval_type = "algorithm"
            # Don't set "manual" here — final determination is at submit time

        # Reruns always go back to L1_REVIEW — rep changed something,
        # new portal answers need re-validation before any auto-submit
        if not is_rerun:
            if bypass_enabled and case:
                result = await db.execute(
                    select(BypassRule).where(
                        BypassRule.cpt_code == (case.cpt_code or ""),
                        BypassRule.icd_code == (case.icd1 or ""),
                        BypassRule.enabled == True,
                    )
                )
                rule = result.scalar_one_or_none()

                # Calculate average confidence
                avg_conf = 0
                if decisions:
                    confs = [d.get("confidence", 0) or 0 for d in decisions]
                    avg_conf = sum(confs) / len(confs) if confs else 0

                if rule and avg_conf >= rule.min_confidence:
                    if rule.bypass_l1 and rule.bypass_l2:
                        target_state = CaseState.SUBMITTING  # Full auto-bypass
                    elif rule.bypass_l1:
                        target_state = CaseState.L2_REVIEW   # Skip L1
                    # else: stays L1_REVIEW

            # Apply settings overrides
            if not l1_enabled and target_state == CaseState.L1_REVIEW:
                target_state = CaseState.L2_REVIEW  # L1 disabled → go to L2
            if not l2_enabled and target_state == CaseState.L2_REVIEW:
                target_state = CaseState.SUBMITTING  # L2 disabled → auto-submit

        await repo.update_case_state(db, case_id, target_state)

        audit_data = {
            "question_count": len(decisions),
            "review_round": review_round,
            "review_target": target_state.value,
            "bypass_applied": target_state != CaseState.L1_REVIEW,
        }
        if awakeable_id:
            audit_data["awakeable_id"] = awakeable_id

        await repo.create_audit_event(
            db,
            case_id=case_id,
            actor="system",
            action="batch_review_pending",
            data=audit_data,
        )

        # Log billing event: FIRST_PASS completed
        if case:
            await repo.create_execution_log(
                db, event_type="FIRST_PASS", case=case,
                document_type="CLINICAL",
                detail={"question_count": len(decisions), "target_state": target_state.value},
            )

        # If full auto-bypass → SUBMITTING, enqueue SUBMIT job
        if target_state == CaseState.SUBMITTING:
            from app.db.models import SubmissionJob, JobStatus, JobType
            from sqlalchemy import update as sa_update

            await db.execute(
                sa_update(SubmissionJob)
                .where(SubmissionJob.case_id == case_id)
                .values(
                    status=JobStatus.QUEUED,
                    job_type="SUBMIT",
                    claimed_by=None,
                    claimed_at=None,
                    last_error=None,
                    started_at=None,
                    completed_at=None,
                )
            )

        await db.commit()

    logger.info(
        f"Saved {len(decisions)} questions for case {case_id}, "
        f"round {review_round}, target={target_state.value}"
    )

    # Wake submission workers if auto-bypass routed to SUBMITTING
    if target_state == CaseState.SUBMITTING:
        try:
            from app.workflow.worker_loop import wake_sleeping_workers
            await wake_sleeping_workers()
        except Exception:
            pass  # Non-fatal — submission workers wake on 5-min fallback


async def _save_order_question_batch(
    case_id: str,
    decisions: list[dict],
    review_round: int = 1,
    auto_approved: bool | None = None,
    gold_card_level: int | None = None,
    algorithm_recommendation: int | None = None,
) -> None:
    """Save questions from an ORDER-ONLY first pass with order-specific routing.

    Same as _save_question_batch but with different routing for low confidence:
      - High confidence → standard L1/L2 review (same as clinical)
      - Low confidence (avg < 60) → WAITING_CLINICALS (park until clinicals arrive)

    Also tags the case with order_only_first_pass=True so sync_engine knows
    to auto re-run through CaseWorkflow when clinicals arrive.
    """
    from app.db.database import async_session_factory
    from app.db import repositories as repo
    from app.db.models import CaseState, ReviewState

    async with async_session_factory() as db:
        # Clean up old questions before saving new batch
        existing = await repo.get_questions_for_case(db, case_id)
        if existing:
            if review_round > 1:
                for q in existing:
                    if q.review_state == ReviewState.AI_SUGGESTED:
                        await db.delete(q)
            else:
                for q in existing:
                    await db.delete(q)
                logger.info(
                    f"_save_order_question_batch/{case_id}: cleared {len(existing)} "
                    f"old questions before saving {len(decisions)} new ones"
                )
            await db.flush()

        for i, d in enumerate(decisions):
            q_kwargs = dict(
                case_id=case_id,
                portal_question_id=str(d["question_id"]),
                group_id=d["group_id"],
                sequence=i + 1,
                question_type=d["question_type"],
                question_text=d["question_text"],
                options_json=d["options"],
                ai_answer=d["portal_answer"],
                ai_confidence=d["confidence"],
                ai_evidence=d["evidence"],
                ai_reasoning=d["reasoning"],
                ai_gap=d.get("pathway_rationale") or d.get("gap"),
                ai_notes_answer=d.get("notes_answer_value"),
                ai_notes_confidence=d.get("notes_confidence"),
                ai_notes_reasoning=d.get("chain_coherence") or d.get("notes_reasoning"),
                ai_approval_gap=d.get("approval_gap"),
                review_state=ReviewState.AI_SUGGESTED,
            )
            await repo.create_question(db, **q_kwargs)

        # Calculate average confidence across all questions
        avg_conf = 0
        if decisions:
            confs = [d.get("confidence", 0) or 0 for d in decisions]
            avg_conf = sum(confs) / len(confs) if confs else 0

        case = await repo.get_case(db, case_id)

        # Persist approval probe results
        if case:
            if auto_approved is not None:
                case.auto_approved = auto_approved
            if gold_card_level is not None:
                case.gold_card_level = gold_card_level
            if algorithm_recommendation is not None:
                case.algorithm_recommendation = algorithm_recommendation
            if (gold_card_level or 0) >= 2:
                case.approval_type = "gold_card"
            elif auto_approved:
                case.approval_type = "algorithm"

            # Tag as order-only first pass for auto re-run detection
            raw_data = dict(case.raw_data or {})
            raw_data["order_only_first_pass"] = True
            case.raw_data = raw_data

        # ORDER-SPECIFIC ROUTING:
        # Low confidence → WAITING_CLINICALS (park for clinical notes)
        # High confidence → standard L1/L2 review
        if avg_conf < 60 and not auto_approved and (gold_card_level or 0) < 2:
            target_state = CaseState.WAITING_CLINICALS
            logger.info(
                f"_save_order_question_batch/{case_id}: avg_conf={avg_conf:.0f} < 60 "
                f"→ WAITING_CLINICALS (order-only, parking for clinicals)"
            )
        else:
            # High confidence or auto-approved — same routing as clinical cases
            from app.db.models import SystemSetting, BypassRule
            from sqlalchemy import select

            l1_setting = await db.get(SystemSetting, "l1_review_enabled")
            l2_setting = await db.get(SystemSetting, "l2_review_enabled")
            bypass_setting = await db.get(SystemSetting, "bypass_enabled")

            l1_enabled = l1_setting.value if l1_setting else True
            l2_enabled = l2_setting.value if l2_setting else True
            bypass_enabled = bypass_setting.value if bypass_setting else False

            target_state = CaseState.L1_REVIEW

            if bypass_enabled and case:
                result = await db.execute(
                    select(BypassRule).where(
                        BypassRule.cpt_code == (case.cpt_code or ""),
                        BypassRule.icd_code == (case.icd1 or ""),
                        BypassRule.enabled == True,
                    )
                )
                rule = result.scalar_one_or_none()
                if rule and avg_conf >= rule.min_confidence:
                    if rule.bypass_l1 and rule.bypass_l2:
                        target_state = CaseState.SUBMITTING
                    elif rule.bypass_l1:
                        target_state = CaseState.L2_REVIEW

            if not l1_enabled and target_state == CaseState.L1_REVIEW:
                target_state = CaseState.L2_REVIEW
            if not l2_enabled and target_state == CaseState.L2_REVIEW:
                target_state = CaseState.SUBMITTING

        await repo.update_case_state(db, case_id, target_state)

        await repo.create_audit_event(
            db,
            case_id=case_id,
            actor="system",
            action="order_batch_review_pending",
            data={
                "question_count": len(decisions),
                "review_round": review_round,
                "review_target": target_state.value,
                "avg_confidence": round(avg_conf, 1),
                "order_only": True,
            },
        )

        # Log billing event: ORDER_PASS completed
        if case:
            await repo.create_execution_log(
                db, event_type="ORDER_PASS", case=case,
                document_type="ORDER_FORM",
                detail={
                    "question_count": len(decisions),
                    "avg_confidence": round(avg_conf, 1),
                    "target_state": target_state.value,
                },
            )

        # If routed to SUBMITTING, enqueue SUBMIT job
        if target_state == CaseState.SUBMITTING:
            from app.db.models import SubmissionJob, JobStatus
            from sqlalchemy import update as sa_update

            await db.execute(
                sa_update(SubmissionJob)
                .where(SubmissionJob.case_id == case_id)
                .values(
                    status=JobStatus.QUEUED,
                    job_type="SUBMIT",
                    claimed_by=None,
                    claimed_at=None,
                    last_error=None,
                    started_at=None,
                    completed_at=None,
                )
            )

        await db.commit()

    logger.info(
        f"Saved {len(decisions)} order questions for case {case_id}, "
        f"round {review_round}, target={target_state.value}, avg_conf={avg_conf:.0f}"
    )

    # Wake workers if auto-bypass routed to SUBMITTING
    if target_state == CaseState.SUBMITTING:
        try:
            from app.workflow.worker_loop import wake_sleeping_workers
            await wake_sleeping_workers()
        except Exception:
            pass


async def _save_pathway_to_case(case_id: str, pathway: dict) -> None:
    """Save pathway name/id to Case record as metadata.

    The pathway (clinical scenario) comes from GetPathwayOptions + SetPathway —
    a separate API call, NOT a question. Stored on the Case for display.
    """
    from app.db.database import async_session_factory
    from sqlalchemy import update as sa_update
    from app.db.models import Case

    async with async_session_factory() as db:
        await db.execute(
            sa_update(Case).where(Case.id == case_id).values(
                pathway_name=pathway.get("name"),
                pathway_id=pathway.get("id"),
                pathway_options=pathway.get("options"),  # [{id, text}, ...]
            )
        )
        await db.commit()
    logger.info(
        f"Saved pathway metadata for case {case_id}: {pathway.get('name')} "
        f"({len(pathway.get('options', []))} options)"
    )
