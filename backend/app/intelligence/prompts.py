"""All prompt templates for the intelligence layer.

Templates are stored in the DB (system_settings) and rendered with Jinja2.
If no DB template exists, falls back to hardcoded defaults in llm_config.py.
"""
import json

from app.intelligence.llm_config import get_system_prompt, render_prompt, DEFAULT_TEMPLATES

# Keep the hardcoded constant for backward compat (tests, direct imports)
EVALUATION_SYSTEM = DEFAULT_TEMPLATES["prompt_eval_system"]


async def get_evaluation_system_prompt(order_mode: bool = False) -> str:
    """Get the evaluation system prompt (from DB or default).

    Args:
        order_mode: If True, use order-specific prompt template.
    """
    key = "prompt_eval_order_system" if order_mode else "prompt_eval_system"
    return await get_system_prompt(key)


async def get_match_system_prompt() -> str:
    """Get the submission answer-matching system prompt (from DB or default)."""
    return await get_system_prompt("prompt_match_system")


async def build_match_prompt(
    question_id: str,
    question_text: str,
    question_type: int,
    options: list[dict],
    saved_answers: list[dict],
) -> str:
    """Build the submission answer-matching prompt using Jinja2 template from DB."""
    return await render_prompt("prompt_match_user", {
        "question_id": question_id,
        "question_text": question_text,
        "question_type": question_type,
        "options": options,
        "saved_answers": saved_answers,
    })


async def build_evaluation_prompt(
    observation: dict,
    clinical_context: dict,
    rag_examples: str,
    multi_select: bool = False,
    rep_answers: list[dict] | None = None,
    changed_group_id: int | None = None,
    pathway_name: str = "",
    previous_answers: list[dict] | None = None,
    order_mode: bool = False,
    signature_answers: list[dict] | None = None,
) -> str:
    """Build the evaluation prompt using the Jinja2 template from DB.

    For reruns: rep_answers contains auth rep's approved answers up to
    changed_group_id. The prompt includes a RE-RUN CONTEXT section that
    instructs the LLM to trust and select the rep's answer for this question.

    Args:
        order_mode: If True, use order-specific prompt template and
                    frame clinical_context as order form evidence.
    """

    # Pre-compute sections that the template consumes
    rag_section = ""
    if rag_examples:
        rag_section = (
            "\n--- APPROVAL PATTERNS FROM SIMILAR CASES ---\n"
            "These are real prior auth cases with known outcomes. "
            "Approved patterns are strong signals — the portal algorithm accepted these answers.\n\n"
            f"{rag_examples}\n"
            "--- END APPROVAL PATTERNS ---\n"
        )

    clinical_section = ""
    if order_mode and clinical_context:
        # Order form data — frame as physician's order evidence
        display_ctx = {
            k: v for k, v in clinical_context.items()
            if k not in ("_meta", "error") and v is not None
        }
        if display_ctx:
            clinical_section = (
                "\n--- PHYSICIAN'S ORDER FORM ---\n"
                f"{json.dumps(display_ctx, indent=2, default=str)}\n"
                "--- END ORDER ---\n\n"
                "CONTEXT: You have the physician's order form with diagnosis, indication, "
                "and exam details. Use this as clinical evidence. The ordering physician has "
                "determined this exam is medically necessary for the stated indication. "
                "No separate clinical notes are available.\n"
            )
        else:
            clinical_section = (
                "\n--- NO ORDER FORM DATA AVAILABLE ---\n"
                "No order form data could be extracted for this case.\n"
                "Set confidence to 20 for all answers and note the gap.\n"
            )
    elif clinical_context:
        display_ctx = {
            k: v for k, v in clinical_context.items()
            if k not in ("_meta", "error") and v is not None
        }
        if display_ctx:
            clinical_section = (
                "\n--- CLINICAL DOCUMENTATION (extracted from patient's clinical notes) ---\n"
                f"{json.dumps(display_ctx, indent=2, default=str)}\n"
                "--- END CLINICAL DOCUMENTATION ---\n\n"
                "Use this documentation as primary evidence. You may also reason from the ICD/CPT codes "
                "and standard clinical knowledge to strengthen the case.\n"
            )
        else:
            clinical_section = (
                "\n--- NO CLINICAL DOCUMENTATION AVAILABLE ---\n"
                "No clinical notes have been extracted for this case.\n"
                "Set confidence to 0 for all answers and note the gap.\n"
            )
    else:
        clinical_section = (
            "\n--- NO CLINICAL DOCUMENTATION AVAILABLE ---\n"
            "No clinical notes have been extracted for this case.\n"
            "Set confidence to 0 for all answers and note the gap.\n"
        )

    # Build RE-RUN CONTEXT section when rep_answers are provided
    rerun_section = ""
    if rep_answers:
        current_group_id = observation.get("group_id")
        # Find the rep's answer for THIS specific question's group
        rep_answer_for_this = None
        for ra in rep_answers:
            ra_group = ra.get("group_id") or ra.get("GroupId")
            if ra_group is not None and current_group_id is not None:
                if int(ra_group) == int(current_group_id):
                    rep_answer_for_this = ra
                    break

        if rep_answer_for_this:
            rep_value = rep_answer_for_this.get("answer_value") or rep_answer_for_this.get("Values")
            rep_text = rep_answer_for_this.get("answer_text") or rep_answer_for_this.get("question_text", "")
            rerun_section = (
                "\n--- RE-RUN CONTEXT ---\n"
                "This is a RE-RUN. An authorization rep has reviewed and approved specific answers.\n"
                f"The rep's approved answer for this question: {json.dumps(rep_value, default=str)}\n"
            )
            if rep_text:
                rerun_section += f"Rep's answer text: {rep_text}\n"
            rerun_section += (
                "\nINSTRUCTION: You MUST select the rep's approved answer above. "
                "Set confidence to 100 and reasoning to 'Rep-approved answer (re-run)'. "
                "Do NOT override the rep's choice with your own clinical judgment.\n"
                "--- END RE-RUN CONTEXT ---\n"
            )
        else:
            # This question is BEYOND the change point — evaluate fresh
            if changed_group_id is not None:
                rerun_section = (
                    "\n--- RE-RUN CONTEXT ---\n"
                    "This is a RE-RUN. Earlier questions were changed by the auth rep.\n"
                    "This question is DOWNSTREAM of the change — evaluate it fresh "
                    "from clinical documentation as if it were a first pass.\n"
                    "--- END RE-RUN CONTEXT ---\n"
                )

    # Build ALGORITHM SIGNATURE section — proven answers from past approved cases
    signature_section = ""
    if signature_answers:
        current_question_id = observation.get("question_id") or ""
        current_question_text = (observation.get("question_text") or "").strip().lower()
        current_group_id = observation.get("group_id")

        # Match signature Q&A to current question by question_id or question_text
        sig_match = None
        for sa in signature_answers:
            sa_qid = sa.get("question_id", "")
            if sa_qid and sa_qid == current_question_id:
                sig_match = sa
                break
        if not sig_match:
            # Fallback: match by group_id
            for sa in signature_answers:
                sa_group = sa.get("group_id") or sa.get("GroupId")
                if sa_group is not None and current_group_id is not None:
                    if int(sa_group) == int(current_group_id):
                        sig_match = sa
                        break
        if not sig_match:
            # Last resort: match by question text similarity
            for sa in signature_answers:
                sa_text = (sa.get("question_text") or "").strip().lower()
                if sa_text and sa_text == current_question_text:
                    sig_match = sa
                    break

        if sig_match:
            sig_value = sig_match.get("answer_value") or sig_match.get("Values", "")
            sig_text = sig_match.get("answer_text") or ""
            signature_section = (
                "\n--- ALGORITHM SIGNATURE (Previously Approved for this CPT+ICD) ---\n"
                "This exact CPT+ICD combination was previously approved by the portal algorithm.\n"
                f"The approved answer for this question was: {json.dumps(sig_value, default=str)}\n"
            )
            if sig_text:
                signature_section += f"Answer text: {sig_text}\n"
            signature_section += (
                "\nThis is a STRONG signal — the portal accepted this answer before for the same "
                "procedure and diagnosis. Use this as your primary guidance when selecting an answer. "
                "Only deviate if the clinical documentation explicitly contradicts this answer.\n"
                "--- END ALGORITHM SIGNATURE ---\n"
            )

    # Render Jinja2 template — order mode uses separate template
    template_key = "prompt_eval_order_user" if order_mode else "prompt_eval_user"
    return await render_prompt(template_key, {
        "cpt_code": observation.get("cpt_code", "N/A"),
        "icd1": observation.get("icd1", "N/A"),
        "carrier_id": observation.get("carrier_id", "N/A"),
        "pathway_name": pathway_name or "N/A",
        "question_type": observation.get("question_type"),
        "question_text": observation.get("question_text", ""),
        "multi_select": multi_select,
        "options": [
            {"id": opt.get("id"), "text": opt.get("text")}
            for opt in observation.get("options", [])
        ],
        "previous_answers": previous_answers or [],
        "rag_section": rag_section,
        "clinical_section": clinical_section,
        "rerun_section": rerun_section,
        "signature_section": signature_section,
    })
