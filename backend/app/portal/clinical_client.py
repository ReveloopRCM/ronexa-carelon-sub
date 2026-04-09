"""ClinicalFacade API client — HAR-validated endpoints via in-page fetch().

ALL calls go through session.api() which runs fetch() inside the browser.
Akamai never sees this traffic because it runs in the trusted browser context.
1-second pacing between calls. Akamai WAF retry on challenge responses.

Endpoints and payloads validated from 3 HAR recordings (3,956 entries):
  - Procter Stacey CPT 73721 (knee MRI, approved)
  - Lopez Sergio CPT 73221 (shoulder MRI)
  - Third submission (1,535 entries)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.portal.session import PlaywrightPortalSession, PortalAPIError

logger = logging.getLogger(__name__)

# Pacing between API calls (ms)
# Tuned for senior rep speed — the real SPA fires requests faster than 1s.
# 400ms keeps us well within human range without being unnecessarily slow.
API_PACE_MS = 400
# Max retries on Akamai challenge
MAX_WAF_RETRIES = 3


class ClinicalFacadeClient:
    """Wraps all ClinicalFacade.aspx JSON API endpoints with pacing and WAF retry.

    All endpoint names and payload structures are HAR-validated.
    """

    def __init__(self, session: PlaywrightPortalSession):
        self.session = session

    async def _call(self, endpoint: str, payload: dict[str, Any] | None = None) -> dict:
        """Make an API call with pacing and WAF retry."""
        for attempt in range(1, MAX_WAF_RETRIES + 1):
            try:
                result = await self.session.api(endpoint, payload)
                await asyncio.sleep(API_PACE_MS / 1000)
                return result
            except PortalAPIError as e:
                if e.status == 403 and attempt < MAX_WAF_RETRIES:
                    logger.warning(f"WAF challenge on {endpoint}, retry {attempt}")
                    await asyncio.sleep(2)
                    continue
                raise
        return {}

    # =========================================================================
    # Initialization
    # =========================================================================

    async def get_client_messages(self, codes: list[int] | None = None) -> dict:
        """Load UI messages/labels."""
        return await self._call("GetClientMessages", {
            "messageCodes": codes or [13, 16, 41]
        })

    async def get_case(self, case_id: str = "0") -> dict:
        """Load current case state."""
        return await self._call("GetCase", {"id": case_id})

    async def primary_cpt_search_box_enable(self) -> dict:
        """Check if primary CPT search is enabled."""
        return await self._call("PrimaryCptCodeSearchBoxEnable", {})

    async def should_withdraw_button_displayed(self) -> dict:
        """UI state check for withdraw button."""
        return await self._call("ShouldWithdrawButtonDisplayed", {})

    # =========================================================================
    # Exam Setup (SOP Step 5)
    # =========================================================================

    async def get_cpt_code_body_side_part(self, cpt_code: str, date_of_service: str) -> dict:
        """Get body side/part options for a CPT code.

        Returns available body sides (Left/Right/Bilateral) and body parts.
        """
        return await self._call("GetCptCodeBodySidePart", {
            "cptCode": cpt_code,
            "dateOfService": date_of_service,
        })

    async def retrieve_text_from_pre(self, personalization_type: str, cpt_code: str, cpt_group_id: int = 0) -> dict:
        """Retrieve popover text (body side/part help text)."""
        return await self._call("RetrieveTextFromPRE", {
            "personalizationType": personalization_type,
            "cptCode": cpt_code,
            "cptGroupId": cpt_group_id,
            "ZipCode": None,
        })

    async def get_cpt_code(self, cpt_code: str, date_of_service: str, selected_cpt_grouper_id: int) -> dict:
        """Resolve CPT code with grouper.

        Example: cpt_code="73721", selected_cpt_grouper_id=46 -> Lower Extremity Joint MRI
        """
        return await self._call("GetCptCode", {
            "cptCode": cpt_code,
            "dateOfService": date_of_service,
            "selectedCptGrouperId": selected_cpt_grouper_id,
        })

    async def validate_exam(self, exam: dict) -> dict:
        """Validate exam configuration before adding.

        Exam dict must include: Name, CPTGroupID, CPTCode, ContrastCaptureID,
        ContrastCaptureDesc, BodySideCode, BodySideDesc, BodyPartCode, BodyPartDesc.
        """
        return await self._call("ValidateExam", {"exam": exam})

    async def get_actual_cpt_code(self, cpt_group_id: int, contrast_capture_id: int, body_part_code: str) -> dict:
        """Resolve actual CPT code from group + contrast + body part."""
        return await self._call("GetActualCPTCode", {
            "cptGroupId": cpt_group_id,
            "contrastCaptureId": contrast_capture_id,
            "bodyPartCode": body_part_code,
        })

    async def get_additional_procedure(self, primary_cpt: str, secondary_cpt: str, date_of_service: str, body_part_code: str) -> dict:
        """Check for additional procedures."""
        return await self._call("GetAdditionalProcedure", {
            "primaryCPTCode": primary_cpt,
            "secondaryCPTCode": secondary_cpt,
            "dateOfService": date_of_service,
            "bodyPartCode": body_part_code,
        })

    async def add_exam(self, exam: dict, username: str, bypass_clinical: bool = False) -> dict:
        """Add exam to the case.

        HAR-validated full payload structure:
        {
          "exam": { Name, CPTGroupID, CPTCode, ExamState:6, UserName,
                    BypassClinicalCriteria, IsPrimaryCPT:true, IsCombo:false,
                    ContrastCaptureID, ContrastCaptureDesc,
                    BodySideCode, BodySideDesc, BodyPartCode, BodyPartDesc,
                    ProductID:1, AddtlProcOption: {...} },
          "bypassClinicalCriteria": false,
          "examSelectionNoMriExam": false,
          "isSecondaryExam": false,
          "isPostClaimExam": false
        }
        """
        exam["ExamState"] = 6
        exam["UserName"] = username
        exam["BypassClinicalCriteria"] = bypass_clinical
        exam["IsPrimaryCPT"] = True
        exam["IsCombo"] = False
        exam.setdefault("ProductID", 1)
        exam.setdefault("AddtlProcOption", {
            "__type": "AIM.ClinicalIntake.Model.Domain.ExamAddtlProc",
            "IsAddtlProcRequired": False,
        })

        return await self._call("AddExam", {
            "exam": exam,
            "bypassClinicalCriteria": bypass_clinical,
            "examSelectionNoMriExam": False,
            "isSecondaryExam": False,
            "isPostClaimExam": False,
        })

    async def is_rbm_eoc_cpt_check(self, exam: dict) -> dict:
        """Check RBM episode of care for CPT."""
        return await self._call("IsRbmEocCptCheck", {"exam": exam})

    async def validate_all_exams(self, cpt_codes: list[str]) -> dict:
        """Validate all exams in the case."""
        return await self._call("ValidateAllExams", {"cptCodes": cpt_codes})

    async def process_selected_exams(self) -> dict:
        """Process selected exams."""
        return await self._call("ProcessSelectedExams", {})

    async def find_next_exam(self) -> dict:
        """Navigate to next exam (or indicate no more)."""
        return await self._call("FindNextExam", {})

    # =========================================================================
    # Diagnosis (SOP Step 6a)
    # =========================================================================

    async def update_current_exam_state(self, exam_state: int) -> dict:
        """Transition exam state. Values: 6=CPT, 3=ICD, 2=Pathway, 8=Answering, 1=Done."""
        return await self._call("UpdateCurrentExamState", {"examState": exam_state})

    async def should_show_unknown_icd(self) -> dict:
        """Check if unknown ICD UI should display."""
        return await self._call("ShouldShowUnkownICD", {})

    async def get_matching_diagnoses(self, search_term: str) -> dict:
        """Search for ICD-10 codes by term.

        Example: search_term="M25.562" -> "Pain in left knee"
        """
        return await self._call("GetMatchingDiagnoses", {"searchTerm": search_term})

    async def set_selected_diagnosis(self, diagnosis: dict, bypass_clinical: bool = False, gold_card_level: int = 0) -> dict:
        """Set the selected ICD-10 diagnosis.

        HAR-validated diagnosis structure:
        {
          "__type": "AIM.ClinicalIntake.Model.Domain.Diagnosis",
          "EnteredTerm": "M25.562",
          "Name": "Pain in left knee",
          "Icd9Code": "M25.562",
          "Icd9Text": "Pain in left knee",
          "IcdType": 1,
          "IcdTypeName": "Icd10",
          "Current_ICDCode": "M25.562",
          "Current_ICDCodeDesc": "Pain in left knee",
          "Description": "M25.562  Pain in left knee"
        }
        """
        return await self._call("SetSelectedDiagnosis", {
            "diagnosis": diagnosis,
            "isBypassClinicalQuestions": bypass_clinical,
            "goldCardLevel": gold_card_level,
        })

    async def is_bypass_and_gold_card_state(self) -> dict:
        """Check bypass and gold card state for exam."""
        return await self._call("IsBypassAndGoldCardStateForExam", {})

    # =========================================================================
    # Pathway Selection (SOP Step 6b)
    # =========================================================================

    async def get_pathway_options(self) -> dict:
        """Get available clinical pathways for the current exam + ICD.

        Returns list of pathways like:
        { Id, Name, PermanentId:"MSK", DisplayName:"KNEE: Meniscal tear",
          ICDCode, AlgorithmType:2, AlgorithmCategoryType:1, ClinicalSource:1 }
        """
        return await self._call("GetPathwayOptions", {})

    async def should_icd_be_selected(self, icd_code: str) -> dict:
        """Check ICD selection state."""
        return await self._call("ShouldICDBeSelected", {"ICDCode": icd_code})

    async def set_pathway(self, pathway: dict) -> dict:
        """Select a clinical pathway.

        HAR-validated pathway structure:
        {
          "__type": "AIM.ClinicalIntake.Model.Domain.Algorithm",
          "Id": "uuid",
          "Name": "Lower extremity MRI fracture evaluation Carelon",
          "PermanentId": "MSK",
          "DisplayName": "ALL: Fracture evaluation",
          "ICDCode": "T14.90XA",
          "AlgorithmType": 2,
          "AlgorithmCategoryType": 1,
          "ClinicalSource": 1
        }
        """
        return await self._call("SetPathway", {"pathway": pathway})

    async def get_radiotracer_options(self) -> dict:
        """Get radiotracer options if applicable."""
        return await self._call("GetRadiotracerOptions", {})

    async def enable_edit_for_radiotracer_codes(self) -> dict:
        """Check radiotracer edit state."""
        return await self._call("EnableEditForRadioTracerCodes", {})

    # =========================================================================
    # Clinical Questions (SOP Step 6c) — RECURSIVE_STATE_MACHINE
    # =========================================================================

    async def get_pathway_assets_with_validation(self, answers: list[dict], group_id: int | None = None) -> dict:
        """The core clinical question loop endpoint.

        RULE 1: Send FULL accumulated answer array on every call.
        Returns new questions when available, empty when done.

        Answer object structure:
        {
          "QuestionId": "guid",
          "QuestionType": 3,  // 2=numeric, 3=single, 4=multi
          "GroupId": 1,
          "Sequence": 1,
          "Values": ["answer-guid-1"]
        }
        """
        return await self._call("GetPathwayAssetsWithValidation", {
            "answers": answers,
            "groupId": group_id,
        })

    async def delete_assets_by_group_id(self, group_id: int) -> dict:
        """Backtrack: delete a question group's assets for re-answer."""
        return await self._call("DeleteAssetsByGroupId", {"groupId": group_id})

    async def get_algorithm_attempt_limit_count(self) -> dict:
        """Check algorithm retry limits."""
        return await self._call("GetAlgorithmAttemptLimitCount", {})

    async def get_additional_info_char_limit(self, cpt_group_id: int, icd_code: str) -> dict:
        """Get character limit for additional info text box."""
        return await self._call("GetAdditionalInfoCharacterLimitForExam", {
            "cptGroupId": cpt_group_id,
            "icdCode": icd_code,
        })

    async def retrieve_algorithm_limit_messages(self) -> dict:
        """Get algorithm limit messages."""
        return await self._call("RetrieveMessageFromPRE", {
            "personalizationType": "AlgorithmLimitMessages",
        })

    # =========================================================================
    # Clinical Resolution
    # =========================================================================

    async def process_accepted(self) -> dict:
        """Process the accepted clinical decision."""
        return await self._call("ProcessAccepted", {})

    async def is_exam_auto_approved(self) -> dict:
        """Check if the exam was auto-approved."""
        return await self._call("IsExamAutoApproved", {})

    async def is_exam_approved_clinical_decision_override(self) -> dict:
        """Check clinical decision override approval."""
        return await self._call("IsExamApprovedClinicalDecisionOverride", {})

    async def add_feedback(self, first_name: str, last_name: str, phone: str, fax: str, additional_info: str = "") -> dict:
        """Add provider feedback/contact info (SOP Step 7 — when not auto-approved)."""
        return await self._call("AddFeedback", {
            "feedback": {
                "FirstName": first_name,
                "LastName": last_name,
                "Phone": phone,
                "Fax": fax,
                "PhoneExtension": " ",
                "AdditionalInformation": additional_info,
            }
        })

    async def add_radio_tracers_if_eligible(self) -> dict:
        """Add radio tracers if eligible."""
        return await self._call("AddRadioTracersIfEligible", {})

    # =========================================================================
    # Exam Completion
    # =========================================================================

    async def get_cpt_code_table(self, cpt_code: str) -> dict:
        """Get CPT code table info."""
        return await self._call("GetCptCodeTable", {"cptCode": cpt_code})

    async def get_procedure_substitutions(self, cpt_group: int) -> dict:
        """Check for procedure substitutions."""
        return await self._call("GetProcedureSubstitutions", {"cptGroup": cpt_group})

    async def retrieve_exam_summary_messaging(self) -> dict:
        """Get exam summary step 3 messaging."""
        return await self._call("RetrieveMessageFromPRE", {
            "personalizationType": "ExamSummaryStep3Messaging",
        })

    async def check_if_additional_doc_required(self) -> dict:
        """Check if additional documentation is required."""
        return await self._call("CheckIfAdditionalDocRequired", {})

    async def done_with_exam(self, exam_state: int = 1) -> dict:
        """Mark exam as complete (state 1 = Done)."""
        return await self._call("DoneWithExam", {"examState": exam_state})
