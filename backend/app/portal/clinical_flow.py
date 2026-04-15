"""Clinical Exam Flow — orchestrates the ClinicalFacade API sequence.

After provider search + fax, the portal shows the "ENTER EXAMS" page.
This module drives the full clinical sequence via the ClinicalFacade JSON API:

  1. Initialize (GetClientMessages, GetCase)
  2. Setup exam (CPT code, body side/part, contrast)
  3. Enter ICD diagnosis
  4. Select clinical pathway
  5. Answer clinical questions (iterative — LLM-driven)
  6. Process determination + finalize

Exam State Machine:
  NEW=6 → IN_ICD=3 → IN_DIAGNOSIS=2 → IN_QUESTIONNAIRE=8 → DONE=1
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Callable

from app.portal.clinical_client import ClinicalFacadeClient
from app.portal.session import PlaywrightPortalSession

logger = logging.getLogger(__name__)


def _ok(data: dict | None = None) -> dict:
    return {"ok": True, "message": None, "data": data or {}}


def _fail(message: str, data: dict | None = None) -> dict:
    return {"ok": False, "message": message, "data": data or {}}


class ClinicalExamFlow:
    """Orchestrates the ClinicalFacade API sequence for exam entry.

    Each method returns {"ok": bool, "message": str|None, "data": {}}.
    The flow captures all portal responses — intelligence grows from volume.
    """

    def __init__(self, session: PlaywrightPortalSession):
        self.session = session
        self.client = ClinicalFacadeClient(session)

        # State accumulated during the flow
        self.case_data: dict = {}
        self.available_cpts: list[dict] = []
        self.cpt_group_id: int | None = None
        self.exam: dict = {}
        self.diagnosis: dict = {}
        self.pathway: dict = {}
        self.answers: list[dict] = []
        self.determination: dict = {}

        # Algorithm recommendation from last GPAWV response
        # RecommendationType: 3=Approve, 6=InProgress, others=Deny/Pend
        self._algorithm_recommendation: int | None = None
        self._exam_result: int | None = None
        self._exam_outcome: str | None = None

    # ==================================================================
    # Step 1: Initialize Clinical Session
    # ==================================================================

    async def initialize(self) -> dict:
        """Load clinical session state: messages, case data, CPT availability.

        Must be called first. Extracts CptGroupId from GetCase response
        which is required for all subsequent CPT-related API calls.
        """
        logger.info("--- CLINICAL: Initialize ---")

        try:
            # Load UI messages
            messages = await self.client.get_client_messages()
            logger.info(f"Client messages loaded: {type(messages)}")

            # Load case data — contains AvailableCptCodes
            case_data = await self.client.get_case()
            self.case_data = case_data
            # Debug: dump raw response structure to diagnose 0 CPT issue
            import json
            try:
                raw_str = json.dumps(case_data, default=str)[:500]
                logger.info(f"Case data raw (first 500): {raw_str}")
            except Exception:
                logger.info(f"Case data type={type(case_data)} keys={list(case_data.keys()) if isinstance(case_data, dict) else 'N/A'}")

            # Extract AvailableCptCodes for group ID lookup
            # Response structure: {"d": {"HasException": false, "Data": {..., "AvailableCptCodes": [...]}}}
            d = case_data.get("d", case_data)
            if isinstance(d, dict):
                data = d.get("Data", d)
                if isinstance(data, str):
                    import json
                    try:
                        data = json.loads(data)
                    except Exception:
                        data = d
                if isinstance(data, dict):
                    self.available_cpts = data.get("AvailableCptCodes", [])
                    logger.info(f"Case Data keys: {list(data.keys())[:15]}")
                else:
                    self.available_cpts = d.get("AvailableCptCodes", [])

            logger.info(f"Available CPT codes: {len(self.available_cpts)}")

            # Guard: if GetCase returned no CPT codes, the fax modal may still
            # be blocking the clinical SPA. Retry once after a brief delay.
            if not self.available_cpts:
                logger.warning(
                    "GetCase returned 0 CPTs — retrying after 3s "
                    "(fax modal may be blocking SPA initialization)"
                )
                import asyncio
                await asyncio.sleep(3)
                case_data = await self.client.get_case()
                self.case_data = case_data
                d = case_data.get("d", case_data)
                if isinstance(d, dict):
                    data = d.get("Data", d)
                    if isinstance(data, str):
                        try:
                            data = json.loads(data)
                        except Exception:
                            data = d
                    if isinstance(data, dict):
                        self.available_cpts = data.get("AvailableCptCodes", [])
                    else:
                        self.available_cpts = d.get("AvailableCptCodes", [])
                logger.info(f"GetCase retry: {len(self.available_cpts)} CPT codes")

                if not self.available_cpts:
                    return _fail(
                        "Clinical SPA returned no AvailableCptCodes — "
                        "fax modal may be blocking initialization"
                    )

            for cpt in self.available_cpts[:5]:
                logger.info(f"  CPT: {cpt}")

            # UI state checks
            await self.client.primary_cpt_search_box_enable()
            await self.client.should_withdraw_button_displayed()

            return _ok({
                "available_cpts": self.available_cpts,
                "case_data_keys": list(d.keys()) if isinstance(d, dict) else [],
            })

        except Exception as e:
            logger.error(f"Clinical init failed: {e}")
            return _fail(f"Clinical initialization failed: {e}")

    # ==================================================================
    # Step 2: Setup Exam (CPT Code Entry)
    # ==================================================================

    async def setup_exam(
        self,
        cpt_code: str,
        date_of_service: str | None = None,
        contrast: str = "Without Contrast",
        contrast_id: int = 0,
        body_side: str = "",
        body_part: str = "",
    ) -> dict:
        """Setup and add an exam for the given CPT code.

        Flow:
          1. Look up CptGroupId from GetCase's AvailableCptCodes
          2. GetCptCodeBodySidePart — get body side/part options
          3. GetCptCode — resolve CPT with grouper
          4. ValidateExam — check exam is valid
          5. AddExam — add to the case
          6. ValidateAllExams + ProcessSelectedExams + FindNextExam

        Args:
            cpt_code: The CPT code (e.g. "74178")
            date_of_service: DOS in M/D/YYYY format (defaults to today)
            contrast: Contrast description (default "Without Contrast")
            contrast_id: Contrast capture ID (default 0)
            body_side: Body side code (empty if N/A)
            body_part: Body part code (empty if N/A)
        """
        logger.info(f"--- CLINICAL: Setup Exam CPT={cpt_code} ---")

        if not date_of_service:
            date_of_service = date.today().strftime("%-m/%-d/%Y")

        # Derive contrast from CPT code if not explicitly provided
        if contrast_id == 0 and contrast == "Without Contrast":
            derived_id, derived_desc = self.derive_contrast_from_cpt(cpt_code)
            if derived_id != 0:
                contrast_id = derived_id
                contrast = derived_desc
                logger.info(
                    f"Contrast derived from CPT {cpt_code}: "
                    f"{contrast} (id={contrast_id})"
                )

        # 1. Find CptGroupId from available CPTs
        self.cpt_group_id = self._find_cpt_group_id(cpt_code)
        if self.cpt_group_id is None:
            return _fail(
                f"CPT {cpt_code} not found in AvailableCptCodes",
                data={"available": self.available_cpts},
            )
        logger.info(f"CPT group ID for {cpt_code}: {self.cpt_group_id}")

        try:
            # 2. Get body side/part options
            body_side_part = await self.client.get_cpt_code_body_side_part(
                cpt_code, date_of_service
            )
            logger.info(f"Body side/part options: {body_side_part}")

            # Parse body side/part from response
            bsp_data = body_side_part.get("d", body_side_part)
            body_side_code, body_side_desc = self._parse_body_side(bsp_data, body_side)
            body_part_code, body_part_desc = self._parse_body_part(bsp_data, body_part)

            # 3. Resolve CPT code with grouper
            cpt_info = await self.client.get_cpt_code(
                cpt_code, date_of_service, self.cpt_group_id
            )
            logger.info(f"CPT code info: {cpt_info}")

            # Parse exam name and contrast from response
            cpt_data = cpt_info.get("d", cpt_info)
            if isinstance(cpt_data, dict):
                cpt_data = cpt_data.get("Data", cpt_data)
            exam_name = f"Exam {cpt_code}"
            if isinstance(cpt_data, dict):
                exam_name = cpt_data.get("Name", exam_name)
                # Cross-reference contrast with API response
                api_contrast_id = cpt_data.get("ContrastCaptureId")
                if api_contrast_id is not None:
                    contrast_map = {
                        0: "Without Contrast",
                        1: "With Contrast",
                        2: "With Contrast",
                        3: "With and Without Contrast",
                    }
                    if contrast_id != 0 and api_contrast_id != contrast_id:
                        # CPT-derived and API disagree — trust the API (Carelon's source)
                        logger.warning(
                            f"Contrast mismatch: CPT-derived={contrast} (id={contrast_id}), "
                            f"API={contrast_map.get(api_contrast_id, '?')} (id={api_contrast_id}). "
                            f"Using API value."
                        )
                    if api_contrast_id != 0 or contrast_id == 0:
                        # Use API if it has a non-zero value, or if we have no derivation
                        contrast_id = api_contrast_id
                        contrast = contrast_map.get(contrast_id, contrast)
            logger.info(f"Exam: {exam_name}, Contrast: {contrast} (id={contrast_id})")

            # 4. Build exam object
            self.exam = {
                "__type": "AIM.ClinicalIntake.Model.Domain.Exam",
                "ProductID": 1,
                "InstanceId": 0,
                "Name": exam_name,
                "CPTGroupID": self.cpt_group_id,
                "CPTCode": cpt_code,
                "ContrastCaptureID": contrast_id,
                "ContrastCaptureDesc": contrast,
                "BodySideCode": body_side_code,
                "BodySideDesc": body_side_desc,
                "BodyPartCode": body_part_code,
                "BodyPartDesc": body_part_desc,
                "ExamState": 6,
                "IsPrimaryCPT": True,
                "IsCombo": False,
                "HasBeenToExamInfo": False,
                "ExamType": 0,
                "IsClaimeSubmitted": None,
                "BodySideAndPartId": None,
                "GoldCardLevel": 0,
                "ExamInfoTextBoxTitleText": None,
                "ExamInfoTextBoxValue": None,
                "IsExamAddTextBoxUnitsEnabled": False,
                "BypassClinicalCriteria": False,
                "UserName": "",
                "Revisions": [{
                    "__type": "AIM.ClinicalIntake.Model.Domain.ExamRevision",
                    "Id": 1,
                    "SuggestedDiagnoses": [],
                    "SelectedDiagnosis": None,
                    "Questions": [],
                    "Recommendations": [],
                }],
                "CurrentRevision": {
                    "__type": "AIM.ClinicalIntake.Model.Domain.ExamRevision",
                    "Id": 1,
                    "SuggestedDiagnoses": [],
                    "SelectedDiagnosis": None,
                    "Questions": [],
                    "Recommendations": [],
                },
                "ParentExamInstanceId": 0,
                "Feedback": [],
                "AddtlProcOption": None,
            }

            # 5. Validate exam
            validate_result = await self.client.validate_exam(self.exam)
            logger.info(f"Validate exam: {validate_result}")

            # 6. Add exam
            add_result = await self.client.add_exam(self.exam, username="")
            logger.info(f"Add exam: {add_result}")

            # 7. Validate all + process + find next
            await self.client.validate_all_exams([cpt_code])
            await self.client.process_selected_exams()
            next_exam = await self.client.find_next_exam()
            logger.info(f"Find next exam: {next_exam}")

            return _ok({
                "cpt_code": cpt_code,
                "cpt_group_id": self.cpt_group_id,
                "exam_name": exam_name,
                "body_side": body_side_desc,
                "body_part": body_part_desc,
                "contrast": contrast,
            })

        except Exception as e:
            logger.error(f"Exam setup failed: {e}")
            return _fail(f"Exam setup failed: {e}")

    # ==================================================================
    # Step 3: Enter ICD Diagnosis
    # ==================================================================

    async def enter_diagnosis(self, icd_code: str) -> dict:
        """Search for and select an ICD-10 diagnosis code.

        Flow:
          1. UpdateCurrentExamState → 3 (IN_ICD)
          2. ShouldShowUnkownICD
          3. GetMatchingDiagnoses — search by ICD code
          4. IsBypassAndGoldCardStateForExam
          5. SetSelectedDiagnosis — select the matched diagnosis
        """
        logger.info(f"--- CLINICAL: Enter Diagnosis ICD={icd_code} ---")

        try:
            # Transition to ICD state
            await self.client.update_current_exam_state(3)
            await self.client.should_show_unknown_icd()

            # Search for ICD code
            search_result = await self.client.get_matching_diagnoses(icd_code)
            logger.info(f"ICD search result: {search_result}")

            # Parse diagnoses from response
            d = search_result.get("d", search_result)
            diagnoses = []
            if isinstance(d, dict):
                diagnoses = d.get("Data", [])
            elif isinstance(d, list):
                diagnoses = d

            if not diagnoses:
                return _fail(f"ICD {icd_code} not found in portal", data={"search_result": d})

            # Use first matching diagnosis
            diag = diagnoses[0]
            icd_text = diag.get("Icd9Text") or diag.get("Name") or icd_code
            logger.info(f"Selected diagnosis: {icd_code} — {icd_text}")

            # Check bypass/gold card
            bypass_result = await self.client.is_bypass_and_gold_card_state()
            logger.info(f"Bypass/gold card: {bypass_result}")

            # Parse gold card state from response
            # Response: {"d": {"Data": {"IsBypass": true, "Level": 2, "WorkFlowType": 0}}}
            self.is_bypass = False
            self.gold_card_level = 0
            try:
                d = bypass_result
                if isinstance(d, dict) and "d" in d:
                    d = d["d"]
                data = d.get("Data", d) if isinstance(d, dict) else d
                if isinstance(data, dict):
                    self.is_bypass = bool(data.get("IsBypass", False))
                    self.gold_card_level = int(data.get("Level", 0))
            except Exception as e:
                logger.warning(f"Could not parse bypass/gold card: {e}")

            if self.is_bypass:
                logger.info(f"🏆 GOLD CARD LEVEL {self.gold_card_level} detected — bypassing clinical questions")
            else:
                logger.info(f"No gold card bypass (level={self.gold_card_level})")

            # Build diagnosis object
            self.diagnosis = {
                "__type": "AIM.ClinicalIntake.Model.Domain.Diagnosis",
                "EnteredTerm": icd_code,
                "Name": icd_text,
                "State": None,
                "ImoId": None,
                "IcdMapDurableId": 0,
                "UserSelectedIcd": None,
                "UserSelectedIcdDescription": None,
                "Icd9Code": diag.get("Icd9Code", icd_code),
                "Icd9Text": icd_text,
                "IcdType": diag.get("IcdType", 1),
                "IcdTypeName": diag.get("IcdTypeName", "Icd10"),
                "IcdConsumerFriendlyDesc": None,
                "Current_ICDCode": icd_code,
                "Current_ICDCodeDesc": icd_text,
                "Description": f"{icd_code}  {icd_text}",
            }

            # Set diagnosis — pass through gold card bypass if portal approved it
            set_result = await self.client.set_selected_diagnosis(
                self.diagnosis,
                bypass_clinical=self.is_bypass,
                gold_card_level=self.gold_card_level,
            )
            logger.info(f"Set diagnosis: {set_result} (bypass={self.is_bypass}, goldCard={self.gold_card_level})")

            return _ok({
                "icd_code": icd_code,
                "description": icd_text,
                "diagnoses_found": len(diagnoses),
            })

        except Exception as e:
            logger.error(f"Diagnosis entry failed: {e}")
            return _fail(f"Diagnosis entry failed: {e}")

    # ==================================================================
    # Step 4: Select Clinical Pathway
    # ==================================================================

    async def select_pathway(
        self,
        preferred_icd: str | None = None,
        preferred_pathway_id: str | None = None,
    ) -> dict:
        """Get available pathways and select the best match.

        Flow:
          1. UpdateCurrentExamState → 2 (IN_DIAGNOSIS)
          2. GetPathwayOptions — get available algorithms
          3. GetRadiotracerOptions + EnableEditForRadioTracerCodes
          4. ShouldICDBeSelected
          5. SetPathway — select the best matching pathway
          6. UpdateCurrentExamState → 8 (IN_QUESTIONNAIRE)

        Selection logic:
          - If preferred_pathway_id set (rerun), select that pathway directly
          - Try exact ICD code match first
          - Then try IsmatchingAlgorithm=true
          - Fallback to first pathway
        """
        logger.info("--- CLINICAL: Select Pathway ---")

        try:
            # Transition to pathway selection state
            await self.client.update_current_exam_state(2)

            # Get pathway options
            pathways_result = await self.client.get_pathway_options()
            logger.info(f"Pathway options: {pathways_result}")

            # Parse pathways from response
            d = pathways_result.get("d", pathways_result)
            algorithms = []
            if isinstance(d, dict):
                data = d.get("Data", d)
                if isinstance(data, dict):
                    algorithms = data.get("Algorithms", [])
                elif isinstance(data, list):
                    algorithms = data
            elif isinstance(d, list):
                algorithms = d

            if not algorithms:
                return _fail("No clinical pathways available", data={"raw": d})

            logger.info(f"Found {len(algorithms)} pathway(s):")
            for alg in algorithms:
                logger.info(
                    f"  [{alg.get('Id', '?')[:8]}...] {alg.get('DisplayName', '?')} "
                    f"ICD={alg.get('ICDCode', '?')} matching={alg.get('IsmatchingAlgorithm', '?')}"
                )

            # Select best pathway
            selected = None

            # Priority 1: Rep's chosen pathway (rerun with pathway change)
            if preferred_pathway_id:
                for alg in algorithms:
                    if alg.get("Id") == preferred_pathway_id:
                        selected = alg
                        logger.info(f"RE-RUN: using rep's pathway: {alg.get('DisplayName', '?')}")
                        break
                if not selected:
                    logger.warning(
                        f"RE-RUN: rep's pathway_id={preferred_pathway_id} not found "
                        f"in {len(algorithms)} options — falling back to ICD match"
                    )

            # Priority 2: ICD match + LLM
            if not selected:
                diag_desc = self.diagnosis.get("Icd9Text") or self.diagnosis.get("Current_ICDCodeDesc")
                selected = await self._select_pathway_with_llm(algorithms, preferred_icd, diag_desc)
            self.pathway = selected
            logger.info(f"Selected pathway: {selected.get('DisplayName', '?')}")

            # Radiotracer checks (nuclear medicine — usually returns empty)
            await self.client.get_radiotracer_options()
            await self.client.enable_edit_for_radiotracer_codes()

            # ICD selection check
            icd_code = preferred_icd or self.diagnosis.get("Current_ICDCode", "")
            if icd_code:
                await self.client.should_icd_be_selected(icd_code)

            # Set pathway
            set_result = await self.client.set_pathway(selected)
            logger.info(f"Set pathway result: {set_result}")

            # Transition to questionnaire state
            await self.client.update_current_exam_state(8)

            # HAR-proven: ShouldWithdrawButtonDisplayed called after state transition
            # All approved HARs include this call — may set server-side session state
            await self.client.should_withdraw_button_displayed()

            # Stash ICD + CPT group for the RetrieveMessageFromPRE + char limit calls
            self._pathway_icd = icd_code
            self._pathway_cpt_group_id = self.cpt_group_id if hasattr(self, 'cpt_group_id') else 0

            # Build pathway options for review (so rep can see what was available)
            pathway_options = [
                {"id": alg.get("Id", ""), "text": alg.get("DisplayName", "")}
                for alg in algorithms
            ]

            return _ok({
                "pathway_name": selected.get("DisplayName", ""),
                "pathway_id": selected.get("Id", ""),
                "icd_code": selected.get("ICDCode", ""),
                "total_pathways": len(algorithms),
                "pathway_options": pathway_options,
                "pathway_selected_id": selected.get("Id", ""),
            })

        except Exception as e:
            logger.error(f"Pathway selection failed: {e}")
            return _fail(f"Pathway selection failed: {e}")

    # ==================================================================
    # Step 5: Clinical Questions Loop
    # ==================================================================

    async def get_questions(self, group_id: int | None = None) -> dict:
        """Get the next batch of clinical questions.

        Portal returns questions inside Data.Assets (NOT Data.Questions).
        Asset structure:
          - Type=1: Question, Type=2: Recommendation
          - ForDisplay=True: needs human/LLM answer
          - ForDisplay=False: auto-answered by portal (Client ID, DOB)
          - QuestionType: 2=numeric/date, 3=single choice, 4=multi choice
          - Options[]: {Id, Text: {Base: "..."}} for choice questions
          - Pre-filled Answers[] captured into self.answers

        Returns:
            {"ok": True, "data": {"questions": [...], "group_id": N, "done": bool}}
        """
        logger.info(f"--- CLINICAL: Get Questions (group={group_id}) ---")

        try:
            result = await self.client.get_pathway_assets_with_validation(
                self.answers, group_id
            )
            logger.info(f"Questions result keys: {list(result.keys()) if isinstance(result, dict) else type(result)}")

            # Parse Assets from response
            # Structure: {"d": {"Data": {"Assets": [...], "Answers": [...], ...}}}
            d = result.get("d", result)
            data = d
            if isinstance(d, dict):
                data = d.get("Data", d)

            assets = []
            pre_filled_answers = []
            current_group_id = group_id

            if isinstance(data, dict):
                assets = data.get("Assets", [])
                pre_filled_answers = data.get("Answers", [])
            elif isinstance(data, list):
                assets = data

            # ── Capture algorithm recommendation from response ──
            if isinstance(data, dict):
                import re as _re
                _data_str = str(data)
                _rec_matches = _re.findall(r"'RecommendationType':\s*(\d+)", _data_str)
                rec_type = int(_rec_matches[0]) if _rec_matches else data.get("RecommendationType")
                exam_result = data.get("ExamResult")
                exam_outcome = data.get("ExamOutcome")
                if rec_type is not None:
                    self._algorithm_recommendation = rec_type  # 3 = Approve, 6 = In Progress
                    self._exam_result = exam_result
                    self._exam_outcome = exam_outcome
                    logger.info(
                        f"Algorithm state: RecommendationType={rec_type} "
                        f"ExamResult={exam_result} ExamOutcome={exam_outcome}"
                    )

            # ── RAW ASSET DUMP (diagnostic) ──
            logger.info(f"RAW RESPONSE: {len(assets)} assets, {len(pre_filled_answers)} pre-filled answers")
            for i, asset in enumerate(assets):
                a_type = asset.get("Type", 0)
                a_display = asset.get("ForDisplay", None)
                a_group = asset.get("GroupId")
                a_label = asset.get("Label", "") or ""
                a_qtype = asset.get("QuestionType")
                a_seq = asset.get("Sequence")
                text_obj = asset.get("Text", {})
                a_text = text_obj.get("Base", "") if isinstance(text_obj, dict) else str(text_obj)
                opts = asset.get("Options", [])
                opt_strs = []
                for o in opts[:8]:
                    ot = o.get("Text", "")
                    if isinstance(ot, dict):
                        ot = ot.get("Base", "")
                    opt_strs.append(str(ot)[:60])
                logger.info(
                    f"  ASSET[{i}] Type={a_type} ForDisplay={a_display} GroupId={a_group} "
                    f"QType={a_qtype} Seq={a_seq} | {a_label[:100]}"
                )
                if a_text and a_text != a_label:
                    logger.info(f"    Text: {a_text[:100]}")
                if opt_strs:
                    logger.info(f"    Options: {opt_strs}")
            for i, ans in enumerate(pre_filled_answers[:10]):
                qid = str(ans.get("QuestionId", ""))[:20]
                val = ans.get("Value", "")
                sels = ans.get("SelectedAnswers", [])
                sel_texts = []
                for s in sels[:3]:
                    st = s.get("Text", "")
                    if isinstance(st, dict):
                        st = st.get("Base", "")
                    sel_texts.append(str(st)[:60])
                logger.info(f"  PRE-FILLED[{i}]: QId={qid} Value=\"{val}\" Selected={sel_texts}")

            # Capture pre-filled answers for accumulation — NORMALIZED to request format.
            # Portal response Answers[] may lack GroupId/QuestionType/Sequence.
            # We cross-reference with Assets to build proper request-format answers.
            if pre_filled_answers and not self.answers:
                self.answers = self._normalize_prefills(pre_filled_answers, assets)
                logger.info(f"Captured {len(self.answers)} normalized pre-filled answer(s) into self.answers")

            # Extract questions — include ALL Type=1 questions (ForDisplay True AND False)
            # Group 1 is the clinical pathway root question; we must not skip it
            questions = []
            recommendations = []
            for asset in assets:
                asset_type = asset.get("Type", 0)
                for_display = asset.get("ForDisplay", False)

                if asset_type == 1:
                    # Parse ALL questions regardless of ForDisplay
                    q = self._parse_asset_question(asset)
                    if q:
                        q["ForDisplay"] = for_display  # tag it so caller knows
                        questions.append(q)
                        if current_group_id is None:
                            current_group_id = asset.get("GroupId")
                    if not for_display:
                        label = asset.get("Label") or ""
                        logger.info(f"ForDisplay=False question INCLUDED: {label[:100]}")
                elif asset_type == 2:
                    # Recommendation — capture for logging
                    rec_text = asset.get("Label") or ""
                    if not rec_text:
                        text_obj = asset.get("Text", {})
                        if isinstance(text_obj, dict):
                            rec_text = text_obj.get("Base", "")
                    if rec_text:
                        recommendations.append(rec_text)

            # Split into displayable (need LLM answer) vs hidden (ForDisplay=False)
            #
            # Trust the portal: ForDisplay=False means the portal auto-handled it.
            # We do NOT override this — promoting hidden questions creates denial
            # risk by fabricating questions the portal never intended for review.
            display_questions = []
            hidden_questions = []

            for q in questions:
                if q.get("ForDisplay", True):
                    display_questions.append(q)
                else:
                    hidden_questions.append(q)

            done = len(questions) == 0 and len(assets) > 0

            if hidden_questions:
                logger.info(f"ForDisplay=False auto-fill questions ({len(hidden_questions)}):")
                for q in hidden_questions:
                    logger.info(f"  HIDDEN Q: {q.get('Text', '?')} [type={q.get('QuestionType', '?')}] GroupId={q.get('GroupId')}")
                    for opt in q.get("Options", []):
                        logger.info(f"    - [{opt.get('Id', '?')[:8]}...] {opt.get('Text', '?')}")

            if display_questions:
                logger.info(f"Got {len(display_questions)} display question(s) needing answers (group={current_group_id}):")
                for q in display_questions:
                    logger.info(f"  Q: {q.get('Text', '?')} [type={q.get('QuestionType', '?')}]")
                    for opt in q.get("Options", []):
                        logger.info(f"    - [{opt.get('Id', '?')[:8]}...] {opt.get('Text', '?')}")
            elif done:
                logger.info("No more questions — clinical questionnaire complete")
            else:
                logger.info("No assets returned — may need to retry")

            if recommendations:
                logger.info(f"Recommendations ({len(recommendations)}):")
                for rec in recommendations:
                    logger.info(f"  >> {rec}")

            return _ok({
                "questions": display_questions,
                "hidden_questions": hidden_questions,
                "group_id": current_group_id,
                "done": done,
                "recommendations": recommendations,
                "total_assets": len(assets),
                "pre_filled_answers": len(pre_filled_answers),
            })

        except Exception as e:
            logger.error(f"Get questions failed: {e}")
            return _fail(f"Get questions failed: {e}")

    def _parse_asset_question(self, asset: dict) -> dict | None:
        """Parse a question from an Assets entry into a clean format.

        Asset structure:
          {
            "Id": "guid", "Label": "Question text",
            "Text": {"Base": "Question text"},
            "QuestionType": 3,  # 2=numeric/date, 3=single, 4=multi
            "Options": [{"Id": "guid", "Text": {"Base": "Option text"}, ...}],
            "GroupId": 1, "Sequence": 1,
            "ForDisplay": true, "Type": 1,
          }
        """
        question_id = asset.get("Id", "")
        question_type = asset.get("QuestionType", 0)

        # Get question text — Text.Base first (per-question), Label fallback (section heading)
        text = ""
        text_obj = asset.get("Text", {})
        if isinstance(text_obj, dict):
            text = text_obj.get("Base", "")
        if not text:
            text = asset.get("Label") or ""

        if not text:
            return None

        # Parse options for choice questions (type 3=single, 4=multi)
        options = []
        for opt in asset.get("Options", []):
            opt_id = opt.get("Id", "")
            opt_text = ""
            opt_text_obj = opt.get("Text", {})
            if isinstance(opt_text_obj, dict):
                opt_text = opt_text_obj.get("Base", "")
            elif isinstance(opt_text_obj, str):
                opt_text = opt_text_obj
            if opt_text:
                options.append({"Id": opt_id, "Text": opt_text})

        return {
            "Id": question_id,
            "Text": text,
            "QuestionType": question_type,
            "Options": options,
            "GroupId": asset.get("GroupId"),
            "Sequence": asset.get("Sequence", 0),
            "MeasureUnitId": asset.get("MeasureUnitId", ""),
        }

    def _normalize_prefills(self, prefills: list[dict], assets: list[dict]) -> list[dict]:
        """Normalize portal response pre-fills to portal request format.

        Portal RESPONSE Answers[] may contain:
            {QuestionId, Value, SelectedAnswers, SelectedValues, ...}
        Portal REQUEST answers[] requires:
            {QuestionId, QuestionType, GroupId, Sequence, Values}

        We cross-reference each pre-fill with the Assets array (which has the
        full question metadata) to build proper request-format answers.
        """
        # Build asset lookup by question ID
        asset_by_id: dict[str, dict] = {}
        for asset in assets:
            aid = str(asset.get("Id", ""))
            if aid:
                asset_by_id[aid] = asset

        normalized = []
        for pf in prefills:
            qid = str(pf.get("QuestionId", ""))
            if not qid:
                continue

            # If it already has GroupId + QuestionType + Values → already normalized
            if pf.get("GroupId") and pf.get("QuestionType") and pf.get("Values"):
                normalized.append(pf)
                logger.info(f"Pre-fill already normalized: GroupId={pf['GroupId']} QId={qid[:12]}...")
                continue

            # Find matching asset for metadata
            asset = asset_by_id.get(qid)
            if not asset:
                logger.warning(f"Pre-fill QId={qid[:12]}... has no matching asset — skipping")
                continue

            # Extract values from pre-fill (multiple possible field names)
            values = pf.get("Values")
            if not values:
                val = pf.get("Value")
                if val:
                    values = [val]
                else:
                    # Try SelectedAnswers or SelectedValues
                    selected = pf.get("SelectedAnswers") or pf.get("SelectedValues") or []
                    values = [str(s.get("Id", "")) for s in selected if s.get("Id")]
            if not values:
                values = []

            answer = {
                "QuestionId": qid,
                "QuestionType": asset.get("QuestionType", 0),
                "GroupId": asset.get("GroupId"),
                "Sequence": asset.get("Sequence", 0),
                "Values": values,
            }

            # Carry over measure unit fields (DOB/date questions)
            mu_id = pf.get("MeasureUnitId") or asset.get("MeasureUnitId")
            if mu_id:
                answer["MeasureUnitId"] = mu_id
            mu_vals = pf.get("MeasureUnitValues")
            if mu_vals:
                answer["MeasureUnitValues"] = mu_vals

            normalized.append(answer)
            logger.info(
                f"Normalized pre-fill: GroupId={answer['GroupId']} "
                f"QType={answer['QuestionType']} QId={qid[:12]}... "
                f"Values={values}"
            )

        return normalized

    async def answer_questions(self, question_answers: list[dict]) -> dict:
        """Submit answers for the current batch of questions.

        Accumulates answers and calls GetPathwayAssetsWithValidation
        with the full answer set to get the next batch.

        Args:
            question_answers: List of answer dicts, each with:
                {
                    "QuestionId": "guid",
                    "QuestionType": 3,  # 2=numeric, 3=single, 4=multi
                    "GroupId": 1,
                    "Sequence": 1,
                    "Values": ["answer-guid"]
                }

        Returns next batch of questions (same as get_questions).
        """
        logger.info(f"--- CLINICAL: Answer {len(question_answers)} question(s) ---")

        # Accumulate answers
        self.answers.extend(question_answers)
        logger.info(f"Total accumulated answers: {len(self.answers)}")
        for a in question_answers:
            logger.info(
                f"  ANS: GroupId={a.get('GroupId')} QuestionId={str(a.get('QuestionId', ''))[:8]}... "
                f"Values={a.get('Values', [])}"
            )

        # Get next batch with accumulated answers
        return await self.get_questions()

    async def run_clinical_questions_loop(
        self,
        answer_fn: Callable[[list[dict]], list[dict]] | None = None,
        max_rounds: int = 20,
    ) -> dict:
        """Run the full clinical questions loop.

        If answer_fn is provided, it's called with each batch of questions
        and must return the corresponding answers. This is where the LLM
        plugs in.

        If answer_fn is None, returns after getting the first batch of questions
        (useful for testing / inspection).

        Args:
            answer_fn: async callable(questions) -> answers
            max_rounds: Safety limit on iterations

        Returns:
            {"ok": True, "data": {"rounds": N, "total_answers": N, "all_questions": [...]}}
        """
        logger.info("--- CLINICAL: Questions Loop ---")

        all_questions = []
        rounds = 0

        # Get first batch
        result = await self.get_questions()
        if not result["ok"]:
            return result

        # HAR-proven: these two calls happen right after the first GetPathwayAssetsWithValidation
        # and BEFORE any answers are submitted. All approved HARs include them.
        # They may set server-side session flags that ProcessAccepted/IsExamAutoApproved check.
        try:
            await self.client.retrieve_algorithm_limit_messages()
            icd_code = getattr(self, '_pathway_icd', '') or (self.diagnosis or {}).get("Current_ICDCode", "")
            cpt_gid = getattr(self, '_pathway_cpt_group_id', 0) or self.cpt_group_id or 0
            if cpt_gid and icd_code:
                await self.client.get_additional_info_char_limit(cpt_gid, icd_code)
            logger.info(f"Post-initial GPAWV calls done (icd={icd_code}, cptGroup={cpt_gid})")
        except Exception as e:
            logger.warning(f"Post-initial GPAWV calls failed (non-fatal): {e}")

        questions = result["data"].get("questions", [])
        all_questions.extend(questions)

        if not answer_fn:
            # No answer function — return questions for manual/LLM processing
            logger.info("No answer_fn provided — returning questions for external processing")
            return _ok({
                "rounds": 0,
                "total_answers": 0,
                "all_questions": all_questions,
                "needs_answers": True,
            })

        while questions and rounds < max_rounds:
            rounds += 1
            logger.info(f"Question round {rounds}: {len(questions)} question(s)")

            # Get answers from the callback (LLM)
            answers = await answer_fn(questions)
            if not answers:
                return _fail("Answer function returned empty answers", data={
                    "round": rounds,
                    "questions": questions,
                })

            # Submit answers and get next batch
            result = await self.answer_questions(answers)
            if not result["ok"]:
                return result

            questions = result["data"].get("questions", [])
            all_questions.extend(questions)

            if result["data"].get("done"):
                break

        logger.info(f"Questions loop complete: {rounds} round(s), {len(self.answers)} total answers")

        return _ok({
            "rounds": rounds,
            "total_answers": len(self.answers),
            "all_questions": all_questions,
            "needs_answers": False,
        })

    # ==================================================================
    # Step 6: Process Determination + Finalize
    # ==================================================================

    async def finalize(
        self,
        first_name: str = "",
        last_name: str = "",
        phone: str = "",
        fax: str = "",
    ) -> dict:
        """Process the clinical determination (questions done → approval check).

        HAR-proven flow on the exam-entry SPA page:
          1. GetAlgorithmAttemptLimitCount
          2. ProcessAccepted — process the clinical decision
          3. IsExamAutoApproved — check if auto-approved
          4. IsExamApprovedClinicalDecisionOverride
          5. AddFeedback — provider contact info
          6. AddRadioTracersIfEligible

        IMPORTANT: DoneWithExam + FindNextExam happen AFTER the hdnAction=20
        postback transitions to the exam summary page. See exam_summary_review().
        """
        logger.info("--- CLINICAL: Finalize (determination) ---")

        try:
            # Algorithm limits
            limits = await self.client.get_algorithm_attempt_limit_count()
            logger.info(f"Algorithm attempt limits: {limits}")

            # Process clinical determination
            process_result = await self.client.process_accepted()
            logger.info(f"Process accepted: {process_result}")

            # Check approval status — NOTE: this flag controls UI branching only,
            # NOT whether the case will ultimately be approved. The reference
            # implementation discards this value and always calls all APIs.
            auto_approved = await self.client.is_exam_auto_approved()
            logger.info(f"Auto approved response: {auto_approved}")
            is_auto = self._parse_bool_response(auto_approved)

            # CDO check — ALWAYS call (reference impl calls unconditionally)
            cdo_approved = await self.client.is_exam_approved_clinical_decision_override()
            logger.info(f"CDO approved: {cdo_approved}")
            is_cdo = self._parse_bool_response(cdo_approved)

            # Add feedback (provider contact) — ALWAYS call (reference impl calls unconditionally)
            if first_name or last_name:
                feedback = await self.client.add_feedback(
                    first_name=first_name,
                    last_name=last_name,
                    phone=phone,
                    fax=fax,
                )
                logger.info(f"Add feedback: {feedback}")

            # Algorithm recommendation is the REAL approval signal
            # RecommendationType 3 = Approve (from last GPAWV response)
            algorithm_approved = self._algorithm_recommendation == 3

            self.determination = {
                "auto_approved": is_auto,
                "cdo_approved": is_cdo,
                "algorithm_approved": algorithm_approved,
                "algorithm_recommendation": self._algorithm_recommendation,
                "exam_result": self._exam_result,
                "exam_outcome": self._exam_outcome,
            }

            logger.info(
                f"Determination: algorithm_approved={algorithm_approved} "
                f"(RecType={self._algorithm_recommendation}), "
                f"is_exam_auto_approved={is_auto}, cdo={is_cdo}"
            )

            # Radiotracer check (always called)
            await self.client.add_radio_tracers_if_eligible()

            return _ok({
                "auto_approved": is_auto,
                "cdo_approved": is_cdo,
                "algorithm_approved": algorithm_approved,
                "algorithm_recommendation": self._algorithm_recommendation,
                "exam_result": self._exam_result,
                "exam_outcome": self._exam_outcome,
            })

        except Exception as e:
            logger.error(f"Finalize failed: {e}")
            return _fail(f"Finalize failed: {e}")

    async def bypass_finalize(
        self,
        icd_code: str = "",
        cpt_group_id: int = 0,
        first_name: str = "",
        last_name: str = "",
        phone: str = "",
        fax: str = "",
    ) -> dict:
        """Gold card bypass finalize — skip pathway + questions entirely.

        HAR-proven flow from Tamayo Eric auto-approval:
          1. UpdateCurrentExamState → 8 (IN_QUESTIONNAIRE)
          2. ShouldWithdrawButtonDisplayed
          3. GetPathwayAssetsWithValidation(answers=[], groupId=null) — returns empty
          4. RetrieveMessageFromPRE (AlgorithmLimitMessages)
          5. GetAdditionalInfoCharacterLimitForExam
          6. GetAlgorithmAttemptLimitCount
          7. ProcessAccepted
          8. IsExamAutoApproved
          9. AddRadioTracersIfEligible

        CRITICAL: Does NOT call GetPathwayOptions, SetPathway, AddFeedback,
        or IsExamApprovedClinicalDecisionOverride — those break the bypass.
        """
        logger.info("--- CLINICAL: BYPASS FINALIZE (gold card) ---")

        try:
            # Jump directly to questionnaire state (skip pathway selection)
            await self.client.update_current_exam_state(8)
            await self.client.should_withdraw_button_displayed()

            # Single GetPathwayAssetsWithValidation with empty answers
            # Portal returns empty assets (no questions to answer)
            result = await self.client.get_pathway_assets_with_validation([], None)
            logger.info(f"Bypass questions check: {result}")

            # Tamayo's HAR calls these before ProcessAccepted
            try:
                await self.client.retrieve_algorithm_limit_messages()
            except Exception as e:
                logger.debug(f"RetrieveMessageFromPRE: {e}")

            if cpt_group_id and icd_code:
                try:
                    await self.client.get_additional_info_char_limit(cpt_group_id, icd_code)
                except Exception as e:
                    logger.debug(f"GetAdditionalInfoCharacterLimitForExam: {e}")

            # Algorithm limits
            limits = await self.client.get_algorithm_attempt_limit_count()
            logger.info(f"Bypass: algorithm limits: {limits}")

            # Process clinical determination
            process_result = await self.client.process_accepted()
            logger.info(f"Bypass: process accepted: {process_result}")

            # Check approval status
            auto_approved = await self.client.is_exam_auto_approved()
            logger.info(f"Bypass: auto approved: {auto_approved}")

            # Parse approval boolean
            is_auto = self._parse_bool_response(auto_approved)

            self.determination = {
                "auto_approved": is_auto,
                "cdo_approved": False,
            }

            # Radiotracer check (no AddFeedback for bypass path)
            await self.client.add_radio_tracers_if_eligible()

            return _ok({
                "auto_approved": is_auto,
                "cdo_approved": False,
                "bypass": True,
                "gold_card_level": self.gold_card_level,
            })

        except Exception as e:
            logger.error(f"Bypass finalize failed: {e}")
            return _fail(f"Bypass finalize failed: {e}")

    async def probe_approval(self) -> dict:
        """Check auto-approval without committing feedback/radiotracers.

        Lightweight version of finalize() that calls only the approval-check
        APIs. Safe to call after questions are answered on the clinical SPA.
        The order is abandoned afterward (navigate home).

        Does NOT call AddFeedback or AddRadioTracersIfEligible — those mutate
        portal state we don't need during a first-pass probe.
        """
        logger.info("--- CLINICAL: Probe approval (lightweight finalize) ---")

        try:
            # Algorithm limits
            limits = await self.client.get_algorithm_attempt_limit_count()
            logger.info(f"Probe: algorithm attempt limits: {limits}")

            # Process clinical determination
            process_result = await self.client.process_accepted()
            logger.info(f"Probe: process accepted: {process_result}")

            # Check approval status
            auto_approved = await self.client.is_exam_auto_approved()
            logger.info(f"Probe: auto approved: {auto_approved}")

            cdo_approved = await self.client.is_exam_approved_clinical_decision_override()
            logger.info(f"Probe: CDO approved: {cdo_approved}")

            # Parse approval booleans
            is_auto = self._parse_bool_response(auto_approved)
            is_cdo = self._parse_bool_response(cdo_approved)

            # Algorithm recommendation is the REAL approval signal
            algorithm_approved = self._algorithm_recommendation == 3

            self.determination = {
                "auto_approved": is_auto,
                "cdo_approved": is_cdo,
                "algorithm_approved": algorithm_approved,
                "algorithm_recommendation": self._algorithm_recommendation,
                "exam_result": self._exam_result,
                "exam_outcome": self._exam_outcome,
            }

            logger.info(
                f"Probe result: algorithm_approved={algorithm_approved} "
                f"(RecType={self._algorithm_recommendation}), "
                f"is_exam_auto_approved={is_auto}, cdo={is_cdo}, "
                f"gold_card_level={getattr(self, 'gold_card_level', 0)}"
            )

            return _ok({
                "auto_approved": is_auto,
                "cdo_approved": is_cdo,
                "algorithm_approved": algorithm_approved,
                "algorithm_recommendation": self._algorithm_recommendation,
                "exam_result": self._exam_result,
                "exam_outcome": self._exam_outcome,
            })

        except Exception as e:
            logger.warning(f"Approval probe failed (non-fatal): {e}")
            return _ok({"auto_approved": None, "cdo_approved": None, "algorithm_approved": None})

    async def exam_summary_review(self, cpt_code: str = "", cpt_group: int | None = None) -> dict:
        """Run the exam summary page API calls after hdnAction=20 postback.

        HAR-proven flow (second clinical SPA load on summary page):
          1. GetClientMessages + GetCase (SPA re-init)
          2. EnableEditForRadioTracerCodes + ShouldWithdrawButtonDisplayed
          3. GetCptCodeTable (if cpt_code provided)
          4. GetProcedureSubstitutions (if cpt_group provided)
          5. RetrieveMessageFromPRE (ExamSummaryStep3Messaging)
          6. CheckIfAdditionalDocRequired (x3 per HAR)
          7. DoneWithExam(examState=1)
          8. FindNextExam
          9. ShouldWithdrawButtonDisplayed

        Returns determination + doc_required info.
        """
        logger.info("--- CLINICAL: Exam Summary Review ---")

        try:
            # SPA re-init on summary page
            await self.client.get_client_messages()
            case_data = await self.client.get_case()
            logger.info(f"Summary GetCase: keys={list(case_data.get('d', {}).get('Data', {}).keys()) if isinstance(case_data.get('d'), dict) else 'n/a'}")

            # Review checks (fire-and-forget, matching HAR sequence)
            try:
                await self.client._call("EnableEditForRadioTracerCodes", {})
            except Exception:
                pass
            try:
                await self.client._call("ShouldWithdrawButtonDisplayed", {})
            except Exception:
                pass

            # CPT table + procedure substitutions (optional — depends on case)
            if cpt_code:
                try:
                    await self.client.get_cpt_code_table(cpt_code)
                except Exception:
                    pass
            if cpt_group is not None:
                try:
                    await self.client.get_procedure_substitutions(cpt_group)
                except Exception:
                    pass

            # Exam summary messaging
            try:
                await self.client.retrieve_exam_summary_messaging()
            except Exception:
                pass

            # Additional doc check (called 3x per HAR)
            doc_required = None
            for i in range(3):
                doc_required = await self.client.check_if_additional_doc_required()
            logger.info(f"Additional doc required: {doc_required}")

            # Done with exam — this is the real one on the summary page
            done_result = await self.client.done_with_exam()
            logger.info(f"Done with exam: {done_result}")

            # Find next exam (should indicate no more for single-exam cases)
            next_exam = await self.client.find_next_exam()
            logger.info(f"Find next exam: {next_exam}")

            # Final withdraw check
            try:
                await self.client._call("ShouldWithdrawButtonDisplayed", {})
            except Exception:
                pass

            return _ok({
                "doc_required": self._parse_bool_response(doc_required) if doc_required else False,
                "done_with_exam": True,
            })

        except Exception as e:
            logger.error(f"Exam summary review failed: {e}")
            return _fail(f"Exam summary review failed: {e}")

    # ==================================================================
    # Helper Methods
    # ==================================================================

    def _find_cpt_group_id(self, cpt_code: str) -> int | None:
        """Find CptGroupId from AvailableCptCodes returned by GetCase."""
        for cpt in self.available_cpts:
            code = cpt.get("CptCode") or cpt.get("cptCode") or ""
            if str(code).strip() == str(cpt_code).strip():
                return cpt.get("CptGroup") or cpt.get("cptGroup") or cpt.get("CptGroupId")
        return None

    @staticmethod
    def derive_contrast_from_cpt(cpt_code: str) -> tuple[int, str]:
        """Derive contrast selection from CPT code.

        CPT codes encode contrast in their last digit for most DI modalities:
          - *6 = Without Contrast (e.g., 74176, 70486, 72192)
          - *7 = With Contrast (e.g., 74177, 70487, 72193)
          - *8 = With and Without Contrast (e.g., 74178, 70488, 72194)

        This is a well-known CPT convention for CT/MRI imaging.
        Returns (contrast_capture_id, contrast_description).
        """
        # Known CPT → contrast mappings (grouped by modality)
        # Format: CPT code → (ContrastCaptureId, Description)
        EXPLICIT_MAP: dict[str, tuple[int, str]] = {
            # CT Abdomen/Pelvis
            "74176": (0, "Without Contrast"),
            "74177": (1, "With Contrast"),
            "74178": (3, "With and Without Contrast"),
            # CT Head/Brain
            "70450": (0, "Without Contrast"),
            "70460": (1, "With Contrast"),
            "70470": (3, "With and Without Contrast"),
            # CT Chest
            "71250": (0, "Without Contrast"),
            "71260": (1, "With Contrast"),
            "71270": (3, "With and Without Contrast"),
            # CT Sinus/Face
            "70486": (0, "Without Contrast"),
            "70487": (1, "With Contrast"),
            "70488": (3, "With and Without Contrast"),
            # CT Spine - Cervical
            "72125": (0, "Without Contrast"),
            "72126": (1, "With Contrast"),
            "72127": (3, "With and Without Contrast"),
            # CT Spine - Thoracic
            "72128": (0, "Without Contrast"),
            "72129": (1, "With Contrast"),
            "72130": (3, "With and Without Contrast"),
            # CT Spine - Lumbar
            "72131": (0, "Without Contrast"),
            "72132": (1, "With Contrast"),
            "72133": (3, "With and Without Contrast"),
            # CT Pelvis
            "72192": (0, "Without Contrast"),
            "72193": (1, "With Contrast"),
            "72194": (3, "With and Without Contrast"),
            # MRI Brain
            "70551": (0, "Without Contrast"),
            "70552": (1, "With Contrast"),
            "70553": (3, "With and Without Contrast"),
            # MRI Spine - Cervical
            "72141": (0, "Without Contrast"),
            "72142": (1, "With Contrast"),
            "72156": (3, "With and Without Contrast"),
            # MRI Spine - Thoracic
            "72146": (0, "Without Contrast"),
            "72147": (1, "With Contrast"),
            "72157": (3, "With and Without Contrast"),
            # MRI Spine - Lumbar
            "72148": (0, "Without Contrast"),
            "72149": (1, "With Contrast"),
            "72158": (3, "With and Without Contrast"),
            # MRI Abdomen
            "74181": (0, "Without Contrast"),
            "74182": (1, "With Contrast"),
            "74183": (3, "With and Without Contrast"),
            # MRI Pelvis
            "72195": (0, "Without Contrast"),
            "72196": (1, "With Contrast"),
            "72197": (3, "With and Without Contrast"),
            # MRI Joint - Knee/Hip/Shoulder/etc.
            "73721": (0, "Without Contrast"),
            "73722": (1, "With Contrast"),
            "73723": (3, "With and Without Contrast"),
            # MRI Upper Extremity (non-joint)
            "73218": (0, "Without Contrast"),
            "73219": (1, "With Contrast"),
            "73220": (3, "With and Without Contrast"),
            # MRI Lower Extremity (non-joint)
            "73718": (0, "Without Contrast"),
            "73719": (1, "With Contrast"),
            "73720": (3, "With and Without Contrast"),
            # CTA Head
            "70496": (1, "With Contrast"),
            # CTA Neck
            "70498": (1, "With Contrast"),
            # CTA Chest
            "71275": (1, "With Contrast"),
            # CTA Abdomen/Pelvis
            "74174": (1, "With Contrast"),
            # CTA Heart
            "75571": (0, "Without Contrast"),  # CT heart calcium scoring
            "75572": (1, "With Contrast"),     # CTA heart w/o 3D
            "75573": (3, "With and Without Contrast"),  # CTA heart w/ & w/o
            "75574": (1, "With Contrast"),     # CTA heart w/ 3D image postprocessing
            # MRA Head
            "70544": (0, "Without Contrast"),
            "70545": (1, "With Contrast"),
            "70546": (3, "With and Without Contrast"),
            # MRA Neck
            "70547": (0, "Without Contrast"),
            "70548": (1, "With Contrast"),
            "70549": (3, "With and Without Contrast"),
        }

        cpt = str(cpt_code).strip()

        # 1. Check explicit map
        if cpt in EXPLICIT_MAP:
            return EXPLICIT_MAP[cpt]

        # 2. If not in map, return default (API will override if it knows)
        logger.info(
            f"CPT {cpt} not in contrast map — defaulting to Without Contrast "
            f"(will use API ContrastCaptureId if available)"
        )
        return (0, "Without Contrast")

    def _parse_body_side(self, bsp_data: Any, preferred: str = "") -> tuple[str, str]:
        """Parse body side from GetCptCodeBodySidePart response."""
        if not isinstance(bsp_data, dict):
            return ("", "")

        sides = bsp_data.get("BodySides", [])
        if not sides:
            return ("", "")

        if preferred:
            for s in sides:
                if s.get("Code", "").lower() == preferred.lower():
                    return (s.get("Code", ""), s.get("Description", ""))

        # Default to first (or empty if not required)
        return ("", "")

    def _parse_body_part(self, bsp_data: Any, preferred: str = "") -> tuple[str, str]:
        """Parse body part from GetCptCodeBodySidePart response."""
        if not isinstance(bsp_data, dict):
            return ("", "")

        parts = bsp_data.get("BodyParts", [])
        if not parts:
            return ("", "")

        if preferred:
            for p in parts:
                if p.get("Code", "").lower() == preferred.lower():
                    return (p.get("Code", ""), p.get("Description", ""))

        # Default to first (or empty if not required)
        return ("", "")

    def _select_best_pathway(
        self, algorithms: list[dict], preferred_icd: str | None = None
    ) -> dict:
        """Select the best clinical pathway from Carelon's options.

        Priority:
          1. Exact ICD code match (what Carelon mapped)
          2. First matching algorithm flag
          3. First pathway (fallback)

        Note: when there's no exact match and all pathways are 'matching',
        use select_pathway_with_llm() for smarter selection.
        """
        if not algorithms:
            return {}

        # 1. Exact ICD match — use what Carelon gives us
        if preferred_icd:
            for alg in algorithms:
                if alg.get("ICDCode", "").upper() == preferred_icd.upper():
                    return alg

        # 2. First matching algorithm
        for alg in algorithms:
            if alg.get("IsmatchingAlgorithm"):
                return alg

        # 3. Absolute fallback
        return algorithms[0]

    async def _select_pathway_with_llm(
        self,
        algorithms: list[dict],
        preferred_icd: str | None = None,
        diagnosis_desc: str | None = None,
    ) -> dict:
        """Use LLM to select the best pathway when exact ICD match fails.

        Treats pathway selection like a clinical question: given the diagnosis,
        which scenario from Carelon's list is the correct match?
        """
        if not algorithms:
            return {}

        # First try exact ICD match — no LLM needed
        if preferred_icd:
            for alg in algorithms:
                if alg.get("ICDCode", "").upper() == preferred_icd.upper():
                    logger.info(f"Pathway exact ICD match: {alg.get('DisplayName')}")
                    return alg

        # Build options list for the LLM (same format as clinical questions)
        options = []
        for i, alg in enumerate(algorithms):
            display = alg.get("DisplayName", f"Option {i+1}")
            icd = alg.get("ICDCode", "")
            options.append({"id": alg.get("Id", str(i)), "text": f"{display} (ICD: {icd})"})

        # Build the question for the LLM
        diag_label = f"{preferred_icd}"
        if diagnosis_desc:
            diag_label = f"{preferred_icd} — {diagnosis_desc}"

        from app.intelligence.models import PortalObservation
        from app.intelligence.evaluator import decide_answer

        observation = PortalObservation(
            question_id="pathway_selection",
            group_id=0,
            question_text=(
                f"The patient's diagnosis is {diag_label}. "
                f"Select the clinical scenario that best matches this diagnosis."
            ),
            question_type=3,  # single-select
            options=options,
            cpt_code="",
            sequence=0,
            icd1=preferred_icd,
        )

        try:
            decision = await decide_answer(
                observation=observation,
                clinical_context={},
                multi_select=False,
                rag_examples="",
            )
            selected_id = decision.answer_value
            logger.info(
                f"LLM pathway selection ({decision.confidence:.0f}%): "
                f"{decision.reasoning}"
            )

            # Find the matching algorithm by ID
            for alg in algorithms:
                if alg.get("Id") == selected_id:
                    return alg

            logger.warning(f"LLM returned unknown pathway ID: {selected_id}")
        except Exception as e:
            logger.warning(f"LLM pathway selection failed: {e}")

        # Fallback if LLM fails
        for alg in algorithms:
            if alg.get("IsmatchingAlgorithm"):
                return alg
        return algorithms[0]

    def _parse_bool_response(self, response: dict) -> bool:
        """Parse a boolean from API response (handles wrapped formats).

        Portal returns varied formats:
          - {"d": true}                          → bool directly
          - {"d": {"Data": "N", ...}}            → string "N"/"Y" in Data
          - {"d": {"Data": false, ...}}          → bool in Data
          - {"d": "N"}                           → string at top level
        """
        d = response.get("d", response)
        if isinstance(d, bool):
            return d
        if isinstance(d, dict):
            val = d.get("Data", d.get("data", False))
            if isinstance(val, bool):
                return val
            if isinstance(val, str):
                return val.upper() in ("Y", "YES", "TRUE", "1")
            return bool(val)
        if isinstance(d, str):
            return d.upper() in ("Y", "YES", "TRUE", "1")
        return bool(d)


# ==================================================================
# Inline Validation Helpers (pure Python — no LLM)
# ==================================================================


def check_eligibility(effective_date_str: str, term_date_str: str | None = None) -> dict:
    """Verify member is currently eligible. Pure date comparison.

    Args:
        effective_date_str: "MM/DD/YYYY" from portal eligibility page
        term_date_str: "MM/DD/YYYY" or "12/31/9999" (no term)

    Returns:
        {"eligible": bool, "effective": str, "term": str, "reason": str|None}
    """
    from datetime import datetime

    today = date.today()

    try:
        eff = datetime.strptime(effective_date_str, "%m/%d/%Y").date()
    except (ValueError, TypeError):
        return {
            "eligible": False,
            "effective": effective_date_str,
            "term": term_date_str,
            "reason": f"Cannot parse effective date: {effective_date_str}",
        }

    # Term date — "12/31/9999" means no termination
    term = date(9999, 12, 31)
    if term_date_str and term_date_str != "12/31/9999":
        try:
            term = datetime.strptime(term_date_str, "%m/%d/%Y").date()
        except (ValueError, TypeError):
            pass  # Treat unparseable term as no-term (still eligible)

    eligible = eff <= today <= term
    reason = None
    if not eligible:
        if today < eff:
            reason = f"Coverage not yet effective (starts {effective_date_str})"
        elif today > term:
            reason = f"Coverage terminated on {term_date_str}"

    return {
        "eligible": eligible,
        "effective": effective_date_str,
        "term": term_date_str or "12/31/9999",
        "reason": reason,
    }


def check_duplicate_auth(
    existing_auths: list[dict],
    cpt_code: str,
    cpt_search_text: str | None = None,
) -> dict:
    """Check if there's an existing authorization for the same CPT code.

    The portal grid has: order_id, order_status, date_of_service,
    exam_description, ordering_provider, outcome, reason.

    Portal grid does NOT show CPT codes — it shows exam descriptions like
    "Brain (Includes IACs, Pituitary) - MRI". These map to the CPT catalog's
    SearchText body-part portion:
        Catalog: "70551 MRI - Brain (Includes IACs, Pituitary) - MRI"
        Grid:    "Brain (Includes IACs, Pituitary) - MRI"

    We match by comparing the body-part description from the catalog against
    each auth's exam_description, plus an active-status check.

    Args:
        existing_auths: List of auth dicts from extract_existing_auths()
        cpt_code: CPT code for the current case (e.g. "70551")
        cpt_search_text: Full SearchText from CPT catalog (e.g.
            "70551 MRI - Brain (Includes IACs, Pituitary) - MRI").
            Used for description-based matching.

    Returns:
        {"duplicate": bool, "matching_auths": list, "reason": str|None}
    """
    # Parse body-part description from SearchText.
    # Format: "{CPT} {modality} - {body_part_description}"
    # e.g. "70551 MRI - Brain (Includes IACs, Pituitary) - MRI"
    #   → exam_label = "brain (includes iacs, pituitary) - mri"
    exam_label = None
    if cpt_search_text:
        parts = cpt_search_text.split(" - ", 1)
        if len(parts) > 1:
            exam_label = parts[1].strip().lower()

    # Two-tier detection:
    #   Tier 1 (≤7 days): duplicate=True — high risk, almost certainly same exam
    #   Tier 2 (8-30 days): recent_auth_warning=True — informational, portal may pend
    matching_7d = []
    warning_30d = []

    for auth in existing_auths:
        exam_desc = (auth.get("exam_description") or "").strip().lower()
        outcome = (auth.get("outcome") or "").strip().lower()
        order_status = (auth.get("order_status") or "").strip().lower()
        dos_str = (auth.get("date_of_service") or "").strip()

        # Match by description (primary) or CPT code in description (fallback)
        has_match = False
        if exam_label and exam_desc:
            has_match = (exam_label == exam_desc) or (exam_label in exam_desc)
        if not has_match:
            has_match = cpt_code.lower() in exam_desc

        # Check if the auth is active/approved (not denied/withdrawn)
        is_active = (
            outcome in ("approved", "authorized", "active", "open")
            or order_status in ("authorized", "open", "active", "in process")
        )

        if not (has_match and is_active):
            continue

        # Parse date and bucket into 7-day or 30-day tier
        days_ago = None
        if dos_str:
            try:
                from datetime import datetime, timedelta
                for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
                    try:
                        dos_date = datetime.strptime(dos_str, fmt).date()
                        days_ago = (date.today() - dos_date).days
                        break
                    except ValueError:
                        continue
            except Exception:
                pass

        if days_ago is None:
            # Can't parse date — treat as 7-day match (err on side of caution)
            matching_7d.append(auth)
        elif days_ago <= 7:
            matching_7d.append(auth)
        elif days_ago <= 30:
            warning_30d.append(auth)

    # Tier 1: ≤7 days — flag as duplicate
    if matching_7d:
        auth_ids = ", ".join(a.get("order_id", "?") for a in matching_7d)
        return {
            "duplicate": True,
            "matching_auths": matching_7d,
            "reason": f"Existing active auth(s) for CPT {cpt_code}: {auth_ids}",
        }

    # Tier 2: 8-30 days — informational warning, don't block
    if warning_30d:
        auth_ids = ", ".join(a.get("order_id", "?") for a in warning_30d)
        return {
            "duplicate": False,
            "matching_auths": [],
            "recent_auth_warning": True,
            "warning_auths": warning_30d,
            "reason": f"Recent auth(s) within 30 days for CPT {cpt_code}: {auth_ids} — portal may pend",
        }

    return {"duplicate": False, "matching_auths": [], "reason": None}
