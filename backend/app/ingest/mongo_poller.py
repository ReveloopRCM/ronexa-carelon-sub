"""Mongo poller — pull new Carelon submissions from Cosmos DB.

Reads from workflowdb.auth-submissions, maps payload → Case model fields,
deduplicates against existing Postgres cases by exam_id.

Two blob keys per record:
  - FileKey             → order PDF (stored as case.file_key)
  - ClinicalAttachments → clinical notes PDF (fetched + extracted separately)

Fully standalone — no imports from excel_parser.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from pymongo import MongoClient

from app.core.settings import settings

logger = logging.getLogger(__name__)

NULL_VALUES = {"NULL", "null", "", None}

# Mongo payload key → Case model field
PAYLOAD_MAP = {
    "ExamId": "exam_id",
    "FirstName": "first_name",
    "LastName": "last_name",
    "dob": "dob",
    "policynum": "policy_num",
    "PatientZipCode": "patient_zip",
    "CenterNPI": "center_npi",
    "CenterAbbr": "center_abbr",
    "cptcode": "cpt_code",
    "ICD1": "icd1",
    "ICD2": "icd2",
    "ICD3": "icd3",
    "ICD4": "icd4",
    "ICD5": "icd5",
    "ReferringNPI": "referring_npi",
    "CarrierId": "carrier_id",
    "FileKey": "file_key",
    "AttachmentId": "attachment_id",
    "IsStat": "is_stat",
    "LastAuthActionDateTime": "scheduled_dt",
    "AuthExamId": "auth_exam_id",
    "OrderRequestID": "order_request_id",
    # New fields from API (not in Excel)
    "ReferringProviderFax": "referring_fax",
    "PatientPhone": "patient_phone",
    "ClinicalAttachments": "clinical_blob_key",
    "ExternalReferenceId": "external_reference_id",
    # ReferringProviderFirstName/LastName kept in raw_data (no DB column)
    # Access via case.raw_data.get("ReferringProviderFirstName") if needed
    # ClinicalHistoryFileKey is a separate system — stored in raw_data for now
    # until we identify its storage location

}


@dataclass
class SyncResult:
    """Result of a Mongo → Postgres sync run."""
    total_fetched: int = 0
    new_cases: int = 0
    duplicates_skipped: int = 0
    errors: int = 0
    cases: list[dict] = field(default_factory=list)


def fetch_carelon_submissions(
    portal_match: str = "Carelon",
    status_filter: str | None = "Submitted",
    limit: int = 500,
    env: str = "uat",
) -> SyncResult:
    """Fetch records from Mongo and map to Case dicts.

    Returns SyncResult with mapped case dicts (not yet inserted into Postgres).

    Args:
        env: Active Mongo environment — "uat" (default) or "prod". Selects
            URI / database / collection / filter envelope. The inner document
            shape (payload.ExamId, FirstName, etc.) is identical between
            environments — only the metadata wrapper differs (UAT uses
            `payload.PortalMatch + status`, prod uses `workflow_id +
            authstatedesc` at root). Caller (sync_engine.run_sync) reads
            the SystemSetting "active_mongo_environment" and passes it in.
    """
    # Pick the right connection + filter based on env
    if env == "prod":
        uri = settings.MONGO_URI_PROD
        db_name = settings.MONGO_DB_PROD
        coll_name = settings.MONGO_COLLECTION_PROD
        # Prod: root-level metadata fields (different wrapper than UAT)
        query: dict = {
            "workflow_id": "carelon",
            "authstatedesc": "Needs Auth",
        }
    else:
        # Default UAT (existing behavior — backwards compatible)
        uri = settings.MONGO_URI
        db_name = settings.MONGO_DB
        coll_name = settings.MONGO_COLLECTION
        query = {"payload.PortalMatch": portal_match}
        if status_filter:
            query["status"] = status_filter

    if not uri:
        logger.warning(
            f"Mongo sync: {env.upper()} URI not configured — skipping (returning 0 records)"
        )
        return SyncResult(total_fetched=0)

    logger.info(
        f"Mongo sync: env={env} db={db_name} coll={coll_name} filter={query}"
    )

    # Cosmos DB uses self-signed certs in the chain
    client = MongoClient(uri, tlsAllowInvalidCertificates=True)
    try:
        db = client[db_name]
        collection = db[coll_name]

        records = list(collection.find(query).limit(limit))
        logger.info(f"Fetched {len(records)} record(s) from Mongo (env={env})")

        result = SyncResult(total_fetched=len(records))

        # Dedup within batch — Mongo may have duplicate ExamIds
        seen_exam_ids: set[str] = set()
        for record in records:
            try:
                case_dict = _map_mongo_record(record)
                if case_dict:
                    eid = case_dict["exam_id"]
                    if eid in seen_exam_ids:
                        result.duplicates_skipped += 1
                        continue
                    seen_exam_ids.add(eid)
                    result.cases.append(case_dict)
            except Exception as e:
                mongo_id = record.get("_id", "unknown")
                logger.error(f"Failed to map Mongo record {mongo_id}: {e}")
                result.errors += 1

        result.new_cases = len(result.cases)

        # Sort: STAT first → scheduled → everything else
        result.cases.sort(key=_sort_key)

        return result
    finally:
        client.close()


def _map_mongo_record(record: dict) -> dict | None:
    """Map a single Mongo document → Case dict.

    Returns None if the record is missing required fields.
    """
    payload = record.get("payload", {})
    if not payload:
        return None

    exam_id = _clean_id(payload.get("ExamId"))
    if not exam_id:
        logger.warning(f"Skipping Mongo record {record.get('_id')}: no ExamId")
        return None

    case: dict = {"id": str(uuid.uuid4())}

    for mongo_key, model_field in PAYLOAD_MAP.items():
        raw_val = payload.get(mongo_key)

        if model_field == "is_stat":
            case[model_field] = _clean_bool(raw_val)
        elif model_field == "scheduled_dt":
            case[model_field] = _clean_datetime(raw_val)
        elif model_field in ("first_name", "last_name"):
            case[model_field] = _clean_name(raw_val)
        elif model_field == "dob":
            case[model_field] = _clean_dob(raw_val)
        elif model_field in (
            "exam_id", "center_npi", "carrier_id", "referring_npi",
            "attachment_id", "cpt_code", "order_request_id", "auth_exam_id",
        ):
            case[model_field] = _clean_id(raw_val)
        else:
            case[model_field] = _clean_str(raw_val)

    # Store full payload as raw_data
    raw_data = {}
    for key, val in payload.items():
        if isinstance(val, datetime):
            raw_data[key] = val.isoformat()
        else:
            raw_data[key] = val

    # Add Mongo-level metadata
    raw_data["_mongo_id"] = str(record.get("_id", ""))
    raw_data["_mongo_status"] = record.get("status")
    raw_data["_source"] = "mongo_api"

    # Data quality flags
    flags = []
    if not case.get("icd1"):
        flags.append("no_icd")
    if not case.get("referring_npi"):
        flags.append("no_referring")
    if not case.get("clinical_blob_key"):
        flags.append("no_clinical_attachment")
    if flags:
        raw_data["_flags"] = flags

    case["raw_data"] = raw_data

    # Classify state
    _classify(case)

    return case


def mark_mongo_records_synced(
    exam_ids: list[str],
    new_status: str = "Synced",
) -> int:
    """Update Mongo records after successful Postgres insert."""
    if not settings.MONGO_URI or not exam_ids:
        return 0

    client = MongoClient(settings.MONGO_URI, tlsAllowInvalidCertificates=True)
    try:
        db = client[settings.MONGO_DB]
        collection = db[settings.MONGO_COLLECTION]

        result = collection.update_many(
            {"payload.ExamId": {"$in": exam_ids}},
            {"$set": {"status": new_status}},
        )

        logger.info(f"Marked {result.modified_count} Mongo records as '{new_status}'")
        return result.modified_count
    finally:
        client.close()


def check_clinical_attachment(exam_id: str) -> str | None:
    """Check Cosmos DB for an updated ClinicalAttachments value.

    Lightweight single-record lookup — used by the Restate workflow retry loop
    when a case was synced without a clinical blob and we need to check if the
    RIS has uploaded it since.

    Returns the blob key string if found, None otherwise.
    """
    if not settings.MONGO_URI:
        return None

    client = MongoClient(settings.MONGO_URI, tlsAllowInvalidCertificates=True)
    try:
        db = client[settings.MONGO_DB]
        collection = db[settings.MONGO_COLLECTION]

        record = collection.find_one(
            {
                "payload.ExamId": exam_id,
                "payload.ClinicalAttachments": {"$nin": [None, "", "NULL", "null"]},
            },
            {"payload.ClinicalAttachments": 1},
        )

        if record:
            blob_key = record.get("payload", {}).get("ClinicalAttachments")
            if blob_key and blob_key not in NULL_VALUES:
                logger.info(f"Clinical attachment found for {exam_id}: {blob_key}")
                return blob_key

        return None
    except Exception as e:
        logger.error(f"Failed to check clinical attachment for {exam_id}: {e}")
        return None
    finally:
        client.close()


def update_mongo_auth_status(
    exam_id: str | int,
    auth_state_desc: str,
    auth_state_sub_desc: str,
    auth_state_id: int,
    workflow_note: str,
    auth_number: str = "",
) -> int:
    """Write auth status back to Mongo/CosmosDB after portal submission.

    Updates the source record with authorization outcome so the RIS
    can pick up the status change.

    Fields updated on the Mongo document:
        status → auth_state_desc (e.g. "Auth Pending", "Authorized")
        payload.AuthStateDesc → auth_state_desc
        payload.AuthStateSubDesc → auth_state_sub_desc
        payload.LastStatusId → auth_state_id
        payload.LastAuthNote → workflow_note
        payload.PrimaryAuthTrackingNum → auth_number
    """
    if not settings.MONGO_URI or not exam_id:
        return 0

    client = MongoClient(settings.MONGO_URI, tlsAllowInvalidCertificates=True)
    try:
        db = client[settings.MONGO_DB]
        collection = db[settings.MONGO_COLLECTION]

        # Normalize exam_id to int if possible (Mongo stores as int)
        try:
            exam_id_query = int(exam_id)
        except (ValueError, TypeError):
            exam_id_query = exam_id

        update_fields = {
            "status": auth_state_desc,
            "payload.authstatedesc": auth_state_desc,
            "payload.AuthStateSubDesc": auth_state_sub_desc,
            "payload.LastStatusId": auth_state_id,
            "payload.LastAuthNote": workflow_note,
            "payload.AllAuthNotes": workflow_note,
            "payload.LastAuthActionDateTime": datetime.utcnow().strftime("%m/%d/%Y %H:%M:%S"),
        }
        if auth_number:
            update_fields["payload.PrimaryAuthTrackingNum"] = auth_number

        result = collection.update_one(
            {"payload.ExamId": exam_id_query},
            {"$set": update_fields},
        )

        logger.info(
            f"Mongo auth status updated for ExamId={exam_id}: "
            f"matched={result.matched_count}, modified={result.modified_count}, "
            f"state={auth_state_desc}"
        )
        return result.modified_count
    except Exception as e:
        logger.error(f"Failed to update Mongo auth status for ExamId={exam_id}: {e}")
        return 0
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _classify(case: dict) -> None:
    """Assign state and sort_priority based on available data."""
    has_required = all([
        case.get("first_name"),
        case.get("last_name"),
        case.get("dob"),
        case.get("policy_num"),
        case.get("center_npi"),
    ])

    if not has_required:
        missing = [f for f in ("first_name", "last_name", "dob", "policy_num", "center_npi")
                   if not case.get(f)]
        case["state"] = "HOLD"
        case["sort_priority"] = 10
        case["hold_reason"] = f"Missing required fields: {', '.join(missing)}"
    elif case.get("is_stat"):
        case["state"] = "PENDING_STAT"
        case["sort_priority"] = 1
    else:
        case["state"] = "PENDING_NOTES"
        has_attachment = case.get("clinical_blob_key") or (
            case.get("attachment_id") and case["attachment_id"] != "0"
        )
        case["sort_priority"] = 2 if has_attachment else 3


def _sort_key(case: dict):
    """Sort: priority ASC → scheduled_dt ASC (nulls last)."""
    priority = case.get("sort_priority", 99)
    sched = case.get("scheduled_dt")
    sched_key = sched if sched is not None else datetime.max
    return (priority, sched_key, case.get("id", ""))


# ---------------------------------------------------------------------------
# Cleaning helpers (standalone — no excel_parser dependency)
# ---------------------------------------------------------------------------

def _clean_id(val) -> str | None:
    """IDs as string — strip float suffixes."""
    if val in NULL_VALUES:
        return None
    s = str(val).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s if s else None


def _clean_name(val) -> str | None:
    """Normalize to Title Case."""
    if val in NULL_VALUES:
        return None
    return str(val).strip().title()


def _clean_str(val) -> str | None:
    if val in NULL_VALUES:
        return None
    s = str(val).strip()
    return s if s else None


def _clean_dob(val) -> str | None:
    """Normalize DOB to MM/DD/YYYY for portal submission."""
    if val in NULL_VALUES:
        return None
    if isinstance(val, datetime):
        return val.strftime("%m/%d/%Y")
    s = str(val).strip()
    if not s:
        return None
    # YYYY-MM-DD (ISO — common from API)
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return f"{int(m.group(2)):02d}/{int(m.group(3)):02d}/{m.group(1)}"
    # MM/DD/YYYY with optional time (e.g. "12/31/1981 00:00:00")
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return f"{int(m.group(1)):02d}/{int(m.group(2)):02d}/{m.group(3)}"
    return s  # Return as-is if unparseable


def _clean_bool(val) -> bool:
    if val in NULL_VALUES:
        return False
    return str(val).strip().upper() in ("YES", "TRUE", "1")


def _clean_datetime(val) -> datetime | None:
    if val in NULL_VALUES:
        return None
    if isinstance(val, datetime):
        return val
    try:
        from dateutil import parser as dtparse
        return dtparse.parse(str(val))
    except (ValueError, TypeError):
        return None
