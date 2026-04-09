"""Standalone evaluator test — replay real production questions through new prompts.

Loads exported case data (questions + clinical notes) from JSON files,
runs each question through decide_answer() with chain-of-thought context,
and dumps full LLM output for review.

Usage:
    cd backend && python3 -m tests.test_prompt_eval

No browser needed. No portal login. Just LLM API calls (~30s per case).
"""
import asyncio
import json
import logging
import os
import sys
import time

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("test_prompt_eval")

# Suppress noisy loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("google").setLevel(logging.WARNING)

# Test case files exported from production
TEST_CASES = [
    "/tmp/test_case_stephanie.json",  # Low back pain MRI, 8 Qs, mixed confidence
    "/tmp/test_case_leandro.json",    # Cancer PET scan, 8 Qs, one low confidence
]


async def run_case(case_file: str) -> dict:
    """Replay all questions for a case through the new prompts."""
    from app.intelligence.evaluator import decide_answer
    from app.intelligence.models import PortalObservation

    with open(case_file) as f:
        data = json.load(f)

    case = data["case"]
    questions = data["questions"]
    clinical_context = data["clinical_context"]

    case_label = f"{case['first_name']} {case['last_name']} | CPT={case['cpt_code']} ICD={case['icd1']}"
    pathway_name = case.get("pathway_name", "")

    print("\n" + "=" * 80)
    print(f"CASE: {case_label}")
    print(f"Pathway: {pathway_name}")
    print(f"Questions: {len(questions)}")
    print(f"Clinical notes: {len(clinical_context)} top-level keys")
    print("=" * 80)

    previous_answers = []
    results = []

    for i, q in enumerate(questions):
        q_label = f"Q{i+1}/{len(questions)} (G{q['group_id']} T{q['question_type']})"
        print(f"\n{'─' * 60}")
        print(f"{q_label}: {q['question_text']}")

        options = q.get("options_json", [])
        if not isinstance(options, list):
            logger.warning(f"  Skipping — options not a list: {type(options)}")
            continue

        # Print options
        for opt in options:
            print(f"  [{opt['id'][:8]}...] {opt['text']}")

        # Build observation
        observation = PortalObservation(
            question_id=q["question_id"],
            group_id=q["group_id"],
            question_text=q["question_text"],
            question_type=q["question_type"],
            options=options,
            cpt_code=case["cpt_code"],
            sequence=q.get("sequence", 0),
            icd1=case.get("icd1"),
            carrier_id=case.get("carrier_id"),
        )

        multi_select = q["question_type"] == 4

        # Call LLM evaluator with new prompts + chain-of-thought
        t0 = time.time()
        try:
            decision = await decide_answer(
                observation=observation,
                clinical_context=clinical_context,
                multi_select=multi_select,
                rag_examples="",  # No RAG in standalone test
                pathway_name=pathway_name,
                previous_answers=previous_answers if previous_answers else None,
            )
        except Exception as e:
            print(f"  ERROR: {e}")
            continue
        elapsed = time.time() - t0

        # Resolve answer text
        answer_text = str(decision.answer_value)
        if isinstance(decision.answer_value, str):
            for o in options:
                if o["id"] == decision.answer_value:
                    answer_text = o["text"]
                    break
        elif isinstance(decision.answer_value, list):
            texts = []
            for av in decision.answer_value:
                for o in options:
                    if o["id"] == av:
                        texts.append(o["text"])
                        break
            if texts:
                answer_text = "; ".join(texts)

        # Accumulate for chain-of-thought
        previous_answers.append({
            "question_number": len(previous_answers) + 1,
            "question_text": q["question_text"],
            "answer_text": answer_text,
            "reasoning": decision.reasoning or "",
        })

        # Print new prompt result
        print(f"\n  NEW PROMPT RESULT ({elapsed:.1f}s):")
        print(f"    Answer:             {answer_text}")
        print(f"    Confidence:         {decision.confidence}%")
        print(f"    Reasoning:          {decision.reasoning}")
        print(f"    Pathway Rationale:  {decision.pathway_rationale}")
        print(f"    Chain Coherence:    {decision.chain_coherence}")

        # Parse evidence (may be JSON string with explicit/inferred)
        evidence = decision.evidence
        if evidence:
            try:
                ev_obj = json.loads(evidence) if isinstance(evidence, str) else evidence
                if isinstance(ev_obj, dict):
                    print(f"    Evidence (explicit): {ev_obj.get('explicit', 'N/A')}")
                    print(f"    Evidence (inferred): {ev_obj.get('inferred', 'N/A')}")
                else:
                    print(f"    Evidence:           {evidence[:200]}")
            except (json.JSONDecodeError, TypeError):
                print(f"    Evidence:           {evidence[:200]}")

        print(f"    Notes Answer:       {decision.notes_answer_value}")
        print(f"    Approval Gap:       {decision.approval_gap}")

        # Compare with old (production) result
        old_conf = q.get("ai_confidence")
        old_evidence = q.get("ai_evidence", "")
        old_reasoning = q.get("ai_reasoning", "")
        old_gap = q.get("ai_gap", "")

        # Resolve old answer text from ai_answer
        old_answer = q.get("ai_answer", {})
        old_answer_text = "N/A"
        if isinstance(old_answer, dict):
            old_vals = old_answer.get("Values", [])
            if old_vals:
                old_texts = []
                for ov in old_vals:
                    for o in options:
                        if o["id"] == ov:
                            old_texts.append(o["text"])
                            break
                old_answer_text = "; ".join(old_texts) if old_texts else str(old_vals)

        print(f"\n  OLD PROMPT RESULT (from production):")
        print(f"    Answer:             {old_answer_text}")
        print(f"    Confidence:         {old_conf}")
        print(f"    Reasoning:          {old_reasoning[:150] if old_reasoning else 'N/A'}")
        print(f"    Evidence:           {old_evidence[:150] if old_evidence else 'N/A'}")
        print(f"    Gap:                {old_gap[:150] if old_gap else 'N/A'}")

        # Flag differences
        answer_changed = answer_text != old_answer_text
        conf_delta = (decision.confidence - old_conf) if old_conf is not None else None

        if answer_changed:
            print(f"\n  >>> ANSWER CHANGED: '{old_answer_text}' -> '{answer_text}'")
        if conf_delta is not None and abs(conf_delta) > 10:
            direction = "UP" if conf_delta > 0 else "DOWN"
            print(f"  >>> CONFIDENCE {direction}: {old_conf} -> {decision.confidence} (delta={conf_delta:+.0f})")

        results.append({
            "question": q["question_text"][:60],
            "new_answer": answer_text,
            "old_answer": old_answer_text,
            "new_confidence": decision.confidence,
            "old_confidence": old_conf,
            "answer_changed": answer_changed,
            "has_chain_coherence": bool(decision.chain_coherence),
            "has_pathway_rationale": bool(decision.pathway_rationale),
            "has_structured_evidence": bool(decision.evidence and "{" in str(decision.evidence)),
        })

    # Print summary
    print(f"\n{'=' * 80}")
    print(f"SUMMARY: {case_label}")
    print(f"{'=' * 80}")
    changed = sum(1 for r in results if r["answer_changed"])
    avg_new = sum(r["new_confidence"] for r in results) / len(results) if results else 0
    avg_old = sum(r["old_confidence"] for r in results if r["old_confidence"]) / len([r for r in results if r["old_confidence"]]) if results else 0
    has_chain = sum(1 for r in results if r["has_chain_coherence"])
    has_pathway = sum(1 for r in results if r["has_pathway_rationale"])
    has_struct_ev = sum(1 for r in results if r["has_structured_evidence"])

    print(f"  Answers changed:        {changed}/{len(results)}")
    print(f"  Avg confidence (new):   {avg_new:.0f}%")
    print(f"  Avg confidence (old):   {avg_old:.0f}%")
    print(f"  Chain coherence:        {has_chain}/{len(results)} questions")
    print(f"  Pathway rationale:      {has_pathway}/{len(results)} questions")
    print(f"  Structured evidence:    {has_struct_ev}/{len(results)} questions")

    return {
        "case": case_label,
        "results": results,
        "changed": changed,
        "total": len(results),
        "avg_new_conf": avg_new,
        "avg_old_conf": avg_old,
    }


async def main():
    print("=" * 80)
    print("  PROMPT EVALUATION TEST — New v2 Prompts vs Production (Old)")
    print("  Chain-of-thought + Pathway Alignment + Two-Tier Evidence")
    print("=" * 80)

    # Check test case files exist
    available = []
    for f in TEST_CASES:
        if os.path.exists(f):
            available.append(f)
        else:
            logger.warning(f"Test case file not found: {f}")

    if not available:
        print("\nNo test case files found! Export from production first.")
        return

    all_results = []
    for case_file in available:
        try:
            result = await run_case(case_file)
            all_results.append(result)
        except Exception as e:
            logger.error(f"Case failed: {case_file}: {e}", exc_info=True)

    # Final summary
    print("\n" + "=" * 80)
    print("  FINAL SUMMARY")
    print("=" * 80)
    total_changed = sum(r["changed"] for r in all_results)
    total_qs = sum(r["total"] for r in all_results)
    print(f"  Total questions tested: {total_qs}")
    print(f"  Answers changed:        {total_changed}/{total_qs}")
    for r in all_results:
        print(f"  {r['case']}: {r['changed']}/{r['total']} changed, "
              f"conf {r['avg_old_conf']:.0f}% -> {r['avg_new_conf']:.0f}%")


if __name__ == "__main__":
    asyncio.run(main())
