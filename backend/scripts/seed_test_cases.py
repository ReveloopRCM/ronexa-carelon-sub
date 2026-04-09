"""Seed 5 test cases from production for local pipeline testing.

2 with clinical_blob_key (should → NOTES_UPLOADED via extraction)
3 with file_key only (should → ORDER_READY via new extraction path)

Usage:
    cd backend
    python scripts/seed_test_cases.py
"""
import asyncio
import json
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env
from dotenv import load_dotenv
load_dotenv()

CASES = [
    # === WITH CLINICALS (2) ===
    {"id": "431c444d-157e-43ac-8ab4-e7f8e2ce3e51", "exam_id": "17243241", "first_name": "Samantha", "last_name": "Harley", "dob": "10/14/1991", "policy_num": "AOI824252071", "patient_zip": "75158", "center_npi": "1710731138", "center_abbr": "RKW", "cpt_code": "73721", "icd1": "S93.411A", "icd2": "S93.491A", "referring_npi": "1104164896", "carrier_id": "14675", "file_key": "a3d2f0a5-ac8d-411e-af9d-a275e298336e.pdf", "attachment_id": "a3d2f0a5-ac8d-411e-af9d-a275e298336e", "is_stat": False, "clinical_blob_key": "b06a45e5-79c8-49c4-a146-c29edf209347", "referring_fax": "9722688777", "patient_phone": "2145467197", "scheduled_dt": "2026-04-08T13:19:49"},
    {"id": "535494a9-b6f4-45ab-b05e-796a96b1bc63", "exam_id": "17241760", "first_name": "Danial", "last_name": "Ore", "dob": "07/05/1970", "policy_num": "YUP810149349", "patient_zip": "77148", "center_npi": "1548590524", "center_abbr": "KEL", "cpt_code": "72149", "icd1": "M54.17", "referring_npi": "1477751774", "carrier_id": "23523", "file_key": "f3b26184-4a0c-4880-8264-6c580dac3f6f.pdf", "attachment_id": "f3b26184-4a0c-4880-8264-6c580dac3f6f", "is_stat": False, "clinical_blob_key": "3f02b996-7dee-4ecc-bfbe-f2f618cd9252", "referring_fax": "8177413908", "patient_phone": "8174562514", "scheduled_dt": "2026-04-08T11:49:49"},

    # === ORDER ONLY (3) — file_key but no clinical_blob_key ===
    {"id": "55818866-df19-4fd4-8d55-9e371832548f", "exam_id": "17242554", "first_name": "Donyell", "last_name": "Roseman", "dob": "05/24/1976", "policy_num": "BCS850092304", "patient_zip": "76063", "center_npi": "1932520673", "center_abbr": "NAR", "cpt_code": "73721", "icd1": None, "referring_npi": "1336350040", "carrier_id": "23523", "file_key": "c06385bc-0425-44c2-a12c-b8e118597e73.pdf", "attachment_id": "c06385bc-0425-44c2-a12c-b8e118597e73", "is_stat": False, "clinical_blob_key": None, "referring_fax": "8172658568", "patient_phone": "2145792915", "scheduled_dt": "2026-04-08T13:19:49"},
    {"id": "5a418988-189c-4f26-b2d5-946ed80108c1", "exam_id": "17242144", "first_name": "Debra", "last_name": "Ploof", "dob": "08/02/1964", "policy_num": "LCB858507727", "patient_zip": "800032745", "center_npi": "1821493511", "center_abbr": "HCR", "cpt_code": "70486", "icd1": "J32.9", "referring_npi": "1730853581", "carrier_id": "23520", "file_key": "7943f73e-4be6-4d42-9263-4d30a255f7be.pdf", "attachment_id": "7943f73e-4be6-4d42-9263-4d30a255f7be", "is_stat": False, "clinical_blob_key": None, "referring_fax": "3034460300", "patient_phone": "3035643201", "scheduled_dt": "2026-04-08T13:19:49"},
    {"id": "ff240b8d-e9d7-4055-b699-3c87bdeeb249", "exam_id": "17240779", "first_name": "Esca", "last_name": "Koegelenberg", "dob": "09/01/1998", "policy_num": "T2U814053724", "patient_zip": "75204", "center_npi": "1508628306", "center_abbr": "EPL", "cpt_code": "70553", "icd1": "G90.2", "referring_npi": None, "carrier_id": "14675", "file_key": "07cadf05-16c3-45cf-bea3-420d6bc5a38b.pdf", "attachment_id": "07cadf05-16c3-45cf-bea3-420d6bc5a38b", "is_stat": True, "clinical_blob_key": None, "referring_fax": "9723537104", "patient_phone": "9039185008", "scheduled_dt": "2026-04-08T13:19:49"},
]


async def main():
    from app.db.database import async_session_factory
    from app.db.models import Case, CaseState, SubmissionJob, JobStatus
    from app.db.queue import compute_priority
    from sqlalchemy import select, text
    from datetime import datetime
    import uuid

    async with async_session_factory() as db:
        # Check for ORDER_READY enum value
        try:
            r = await db.execute(text("SELECT 'ORDER_READY'::casestate"))
            print("✓ ORDER_READY enum value exists in Postgres")
        except Exception:
            print("✗ ORDER_READY enum value NOT found — run: alembic upgrade head")
            return

        inserted = 0
        skipped = 0

        for cd in CASES:
            # Check if case already exists
            existing = await db.execute(select(Case).where(Case.id == cd["id"]))
            if existing.scalar_one_or_none():
                print(f"  SKIP  {cd['first_name']} {cd['last_name']} (already exists)")
                skipped += 1
                continue

            has_clinical = bool(cd.get("clinical_blob_key"))
            case_type = "CLINICAL" if has_clinical else "ORDER-ONLY"

            case = Case(
                id=cd["id"],
                exam_id=cd["exam_id"],
                first_name=cd["first_name"],
                last_name=cd["last_name"],
                dob=cd["dob"],
                policy_num=cd["policy_num"],
                patient_zip=cd.get("patient_zip"),
                center_npi=cd["center_npi"],
                center_abbr=cd.get("center_abbr"),
                cpt_code=cd["cpt_code"],
                icd1=cd.get("icd1"),
                icd2=cd.get("icd2"),
                referring_npi=cd.get("referring_npi"),
                carrier_id=cd.get("carrier_id"),
                file_key=cd.get("file_key"),
                attachment_id=cd.get("attachment_id"),
                clinical_blob_key=cd.get("clinical_blob_key"),
                referring_fax=cd.get("referring_fax"),
                patient_phone=cd.get("patient_phone"),
                is_stat=cd.get("is_stat", False),
                scheduled_dt=datetime.fromisoformat(cd["scheduled_dt"]) if cd.get("scheduled_dt") else None,
                state=CaseState.PENDING_NOTES,  # Start in PENDING_NOTES — extraction will route
                sort_priority=cd.get("sort_priority", 2),
                raw_data={},
            )
            db.add(case)
            await db.flush()

            # Create submission job
            job = SubmissionJob(
                id=str(uuid.uuid4()),
                case_id=case.id,
                job_type="FIRST_PASS",  # Extraction will update to ORDER for order-only
                status=JobStatus.QUEUED,
                priority=compute_priority(case),
                is_stat=case.is_stat,
            )
            db.add(job)

            print(f"  INSERT {cd['first_name']} {cd['last_name']} | CPT {cd['cpt_code']} | {case_type} | blob={'yes' if has_clinical else 'no'} | file_key={'yes' if cd.get('file_key') else 'no'}")
            inserted += 1

        await db.commit()
        print(f"\n✓ Seeded {inserted} cases, skipped {skipped}")

        # Show current state
        r = await db.execute(text("""
            SELECT c.id, c.first_name, c.last_name, c.cpt_code, c.state,
                   c.clinical_blob_key IS NOT NULL AS has_clinical,
                   c.file_key IS NOT NULL AS has_file_key,
                   j.job_type, j.status
            FROM cases c
            LEFT JOIN submission_jobs j ON j.case_id = c.id
            WHERE c.id IN :ids
            ORDER BY c.first_name
        """), {"ids": tuple(cd["id"] for cd in CASES)})
        print("\n--- Current state ---")
        print(f"{'Name':<25} {'CPT':<8} {'State':<18} {'Clinical':<10} {'FileKey':<10} {'JobType':<15} {'JobStatus'}")
        for row in r.fetchall():
            print(f"{row[1]} {row[2]:<18} {row[3]:<8} {row[4]:<18} {'yes' if row[5] else 'no':<10} {'yes' if row[6] else 'no':<10} {row[7] or 'N/A':<15} {row[8] or 'N/A'}")


if __name__ == "__main__":
    asyncio.run(main())
