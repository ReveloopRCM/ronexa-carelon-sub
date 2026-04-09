"""Test extraction on the 5 seeded cases.

Verifies:
- Cases with clinical_blob_key → NOTES_UPLOADED + FIRST_PASS
- Cases with file_key only → ORDER_READY + ORDER

Usage:
    cd backend
    .venv/bin/python scripts/test_extraction.py
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

CASE_IDS = [
    "431c444d-157e-43ac-8ab4-e7f8e2ce3e51",  # Samantha Harley — clinical
    "535494a9-b6f4-45ab-b05e-796a96b1bc63",  # Danial Ore — clinical
    "55818866-df19-4fd4-8d55-9e371832548f",  # Donyell Roseman — order only
    "5a418988-189c-4f26-b2d5-946ed80108c1",  # Debra Ploof — order only
    "ff240b8d-e9d7-4055-b699-3c87bdeeb249",  # Esca Koegelenberg — order only
]


async def main():
    from app.workflow.extraction_service import _extract_single_case

    print("=" * 60)
    print("EXTRACTION TEST — 5 seeded cases")
    print("=" * 60)

    for case_id in CASE_IDS:
        print(f"\nExtracting {case_id[:12]}...")
        result = await _extract_single_case(case_id)
        status = "✓" if result.get("success") else "✗"
        print(f"  {status} Result: {result}")

    # Verify final states
    print("\n" + "=" * 60)
    print("FINAL STATES")
    print("=" * 60)

    from app.db.database import async_session_factory
    from sqlalchemy import text

    async with async_session_factory() as db:
        r = await db.execute(text("""
            SELECT c.first_name || ' ' || c.last_name AS name,
                   c.cpt_code, c.state,
                   CASE WHEN c.clinical_blob_key IS NOT NULL THEN 'CLINICAL' ELSE 'ORDER-ONLY' END AS type,
                   j.job_type, j.status AS job_status,
                   n.document_type
            FROM cases c
            LEFT JOIN submission_jobs j ON j.case_id = c.id
            LEFT JOIN clinical_notes n ON n.case_id = c.id
            WHERE c.id = ANY($1)
            ORDER BY c.first_name
        """), [CASE_IDS])

        print(f"\n{'Name':<22} {'CPT':<8} {'State':<18} {'Type':<12} {'JobType':<12} {'JobStatus':<10} {'DocType'}")
        print("-" * 100)
        for row in r.fetchall():
            print(f"{row[0]:<22} {row[1]:<8} {row[2]:<18} {row[3]:<12} {str(row[4]):<12} {str(row[5]):<10} {str(row[6])}")

    # Validation
    print("\n" + "=" * 60)
    print("VALIDATION")
    print("=" * 60)

    async with async_session_factory() as db:
        r = await db.execute(text("""
            SELECT c.first_name || ' ' || c.last_name,
                   c.state, j.job_type,
                   CASE WHEN c.clinical_blob_key IS NOT NULL THEN 'CLINICAL' ELSE 'ORDER-ONLY' END
            FROM cases c
            LEFT JOIN submission_jobs j ON j.case_id = c.id
            WHERE c.id = ANY($1)
        """), [CASE_IDS])

        all_pass = True
        for row in r.fetchall():
            name, state, job_type, case_type = row
            if case_type == "CLINICAL":
                expected_state = "NOTES_UPLOADED"
                expected_job = "FIRST_PASS"
            else:
                expected_state = "ORDER_READY"
                expected_job = "ORDER"

            state_ok = state == expected_state
            job_ok = job_type == expected_job
            status = "✓" if (state_ok and job_ok) else "✗"
            if not (state_ok and job_ok):
                all_pass = False
            print(f"  {status} {name}: state={state} (expected {expected_state}), job={job_type} (expected {expected_job})")

        print(f"\n{'✓ ALL TESTS PASSED' if all_pass else '✗ SOME TESTS FAILED'}")


if __name__ == "__main__":
    asyncio.run(main())
