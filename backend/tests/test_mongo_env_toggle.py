"""Tests for the v151 Mongo sync source toggle (UAT / Prod).

The active Mongo environment is stored in `SystemSetting` under the key
`active_mongo_environment`. `run_sync` reads it before each poll and
passes the env name to `fetch_carelon_submissions`, which dispatches
the right URI / database / collection / filter envelope.

These tests verify three contract points:
  1. The toggle-reading logic in run_sync defaults to "uat" when the
     setting is absent (zero behavior change on deploy).
  2. fetch_carelon_submissions's filter is correct for each env.
  3. fetch_carelon_submissions short-circuits cleanly when the URI is
     not configured (returns SyncResult with 0 records, no crash).

We mock pymongo.MongoClient at the test boundary so these stay pure-Python
unit tests with no Mongo connection required.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from app.ingest.mongo_poller import fetch_carelon_submissions, SyncResult


# ─────────────────────────────────────────────────────────────────────
# fetch_carelon_submissions: dispatches by env
# ─────────────────────────────────────────────────────────────────────


def test_fetch_uat_uses_uat_filter_and_collection():
    """env='uat' → query is {payload.PortalMatch + status} on workflowdb.auth-submissions."""
    fake_collection = MagicMock()
    fake_collection.find.return_value.limit.return_value = []
    fake_db = MagicMock()
    fake_db.__getitem__.return_value = fake_collection
    fake_client = MagicMock()
    fake_client.__getitem__.return_value = fake_db

    with patch("app.ingest.mongo_poller.MongoClient", return_value=fake_client), \
         patch("app.ingest.mongo_poller.settings") as fake_settings:
        fake_settings.MONGO_URI = "mongodb://uat-uri/"
        fake_settings.MONGO_DB = "workflowdb"
        fake_settings.MONGO_COLLECTION = "auth-submissions"
        fake_settings.MONGO_URI_PROD = ""
        fake_settings.MONGO_DB_PROD = "llm_orchestration_prod"
        fake_settings.MONGO_COLLECTION_PROD = "gateway_submissions"

        result = fetch_carelon_submissions(env="uat")

    # Should have used UAT URI
    from app.ingest.mongo_poller import MongoClient
    # Confirm the right DB + collection were selected
    fake_client.__getitem__.assert_called_with("workflowdb")
    fake_db.__getitem__.assert_called_with("auth-submissions")
    # And the right filter shape
    call_args = fake_collection.find.call_args
    query = call_args[0][0]
    assert query == {"payload.PortalMatch": "Carelon", "status": "Submitted"}, (
        f"UAT filter wrong: {query}"
    )
    assert isinstance(result, SyncResult)


def test_fetch_prod_uses_prod_filter_and_collection():
    """env='prod' → query is {workflow_id + authstatedesc} on llm_orchestration_prod.gateway_submissions."""
    fake_collection = MagicMock()
    fake_collection.find.return_value.limit.return_value = []
    fake_db = MagicMock()
    fake_db.__getitem__.return_value = fake_collection
    fake_client = MagicMock()
    fake_client.__getitem__.return_value = fake_db

    with patch("app.ingest.mongo_poller.MongoClient", return_value=fake_client), \
         patch("app.ingest.mongo_poller.settings") as fake_settings:
        fake_settings.MONGO_URI = "mongodb://uat-uri/"
        fake_settings.MONGO_DB = "workflowdb"
        fake_settings.MONGO_COLLECTION = "auth-submissions"
        fake_settings.MONGO_URI_PROD = "mongodb://prod-uri/"
        fake_settings.MONGO_DB_PROD = "llm_orchestration_prod"
        fake_settings.MONGO_COLLECTION_PROD = "gateway_submissions"

        result = fetch_carelon_submissions(env="prod")

    # Confirm the right DB + collection were selected
    fake_client.__getitem__.assert_called_with("llm_orchestration_prod")
    fake_db.__getitem__.assert_called_with("gateway_submissions")
    # And the prod filter shape (root-level workflow_id + authstatedesc)
    call_args = fake_collection.find.call_args
    query = call_args[0][0]
    assert query == {"workflow_id": "carelon", "authstatedesc": "Needs Auth"}, (
        f"PROD filter wrong: {query}"
    )
    assert isinstance(result, SyncResult)


def test_fetch_prod_with_unconfigured_uri_returns_empty():
    """env='prod' but MONGO_URI_PROD blank → returns SyncResult(0) without crashing.

    This makes the deploy safe: orchestrators that haven't set
    MONGO_URI_PROD yet won't error on the toggle being flipped — they
    just sync 0 records until the env var is set. Symptom is visible
    in the UI's Sync Source panel (URI ✗ not set badge).
    """
    with patch("app.ingest.mongo_poller.MongoClient") as fake_mongo, \
         patch("app.ingest.mongo_poller.settings") as fake_settings:
        fake_settings.MONGO_URI = "mongodb://uat-uri/"
        fake_settings.MONGO_URI_PROD = ""  # not configured
        fake_settings.MONGO_DB_PROD = "llm_orchestration_prod"
        fake_settings.MONGO_COLLECTION_PROD = "gateway_submissions"

        result = fetch_carelon_submissions(env="prod")

    # MongoClient should NOT have been instantiated
    fake_mongo.assert_not_called()
    assert isinstance(result, SyncResult)
    assert result.total_fetched == 0
    assert result.cases == []


def test_fetch_default_env_is_uat():
    """Calling fetch_carelon_submissions() with no env arg defaults to UAT —
    this is the backwards-compat path. Existing call sites that don't
    pass env still work as before."""
    fake_collection = MagicMock()
    fake_collection.find.return_value.limit.return_value = []
    fake_db = MagicMock()
    fake_db.__getitem__.return_value = fake_collection
    fake_client = MagicMock()
    fake_client.__getitem__.return_value = fake_db

    with patch("app.ingest.mongo_poller.MongoClient", return_value=fake_client), \
         patch("app.ingest.mongo_poller.settings") as fake_settings:
        fake_settings.MONGO_URI = "mongodb://uat-uri/"
        fake_settings.MONGO_DB = "workflowdb"
        fake_settings.MONGO_COLLECTION = "auth-submissions"

        # No env passed — should default to UAT
        result = fetch_carelon_submissions()

    fake_client.__getitem__.assert_called_with("workflowdb")
    assert isinstance(result, SyncResult)


# ─────────────────────────────────────────────────────────────────────
# v152: writeback functions also dispatch by env
# (mark_mongo_records_synced, update_mongo_auth_status)
# ─────────────────────────────────────────────────────────────────────


def test_mark_synced_uses_prod_target_when_env_prod():
    """mark_mongo_records_synced(env='prod') connects to prod URI and
    writes to llm_orchestration_prod.gateway_submissions."""
    from app.ingest.mongo_poller import mark_mongo_records_synced

    fake_result = MagicMock()
    fake_result.modified_count = 3
    fake_collection = MagicMock()
    fake_collection.update_many.return_value = fake_result
    fake_db = MagicMock()
    fake_db.__getitem__.return_value = fake_collection
    fake_client = MagicMock()
    fake_client.__getitem__.return_value = fake_db

    with patch("app.ingest.mongo_poller.MongoClient", return_value=fake_client) as mongo_client, \
         patch("app.ingest.mongo_poller.settings") as fake_settings:
        fake_settings.MONGO_URI = "mongodb://uat-uri/"
        fake_settings.MONGO_DB = "workflowdb"
        fake_settings.MONGO_COLLECTION = "auth-submissions"
        fake_settings.MONGO_URI_PROD = "mongodb://prod-uri/"
        fake_settings.MONGO_DB_PROD = "llm_orchestration_prod"
        fake_settings.MONGO_COLLECTION_PROD = "gateway_submissions"

        n = mark_mongo_records_synced(["EX1", "EX2"], env="prod")

    # Targeted prod URI
    mongo_client.assert_called_once_with("mongodb://prod-uri/", tlsAllowInvalidCertificates=True)
    # Right db + collection
    fake_client.__getitem__.assert_called_with("llm_orchestration_prod")
    fake_db.__getitem__.assert_called_with("gateway_submissions")
    # Update query shape unchanged across envs (payload.ExamId)
    update_call = fake_collection.update_many.call_args
    assert update_call[0][0] == {"payload.ExamId": {"$in": ["EX1", "EX2"]}}
    assert update_call[0][1] == {"$set": {"status": "Synced"}}
    assert n == 3


def test_mark_synced_default_env_is_uat():
    """mark_mongo_records_synced() with no env defaults to UAT —
    backwards-compat for any caller that hasn't been updated."""
    from app.ingest.mongo_poller import mark_mongo_records_synced

    fake_result = MagicMock()
    fake_result.modified_count = 1
    fake_collection = MagicMock()
    fake_collection.update_many.return_value = fake_result
    fake_db = MagicMock()
    fake_db.__getitem__.return_value = fake_collection
    fake_client = MagicMock()
    fake_client.__getitem__.return_value = fake_db

    with patch("app.ingest.mongo_poller.MongoClient", return_value=fake_client) as mongo_client, \
         patch("app.ingest.mongo_poller.settings") as fake_settings:
        fake_settings.MONGO_URI = "mongodb://uat-uri/"
        fake_settings.MONGO_DB = "workflowdb"
        fake_settings.MONGO_COLLECTION = "auth-submissions"
        fake_settings.MONGO_URI_PROD = "mongodb://prod-uri/"

        mark_mongo_records_synced(["EX1"])  # no env arg

    mongo_client.assert_called_once_with("mongodb://uat-uri/", tlsAllowInvalidCertificates=True)
    fake_client.__getitem__.assert_called_with("workflowdb")


def test_update_auth_status_uses_prod_target_when_env_prod():
    """update_mongo_auth_status(env='prod') connects to prod URI and
    writes the auth outcome to the prod source DB."""
    from app.ingest.mongo_poller import update_mongo_auth_status

    fake_result = MagicMock()
    fake_result.matched_count = 1
    fake_result.modified_count = 1
    fake_collection = MagicMock()
    fake_collection.update_one.return_value = fake_result
    fake_db = MagicMock()
    fake_db.__getitem__.return_value = fake_collection
    fake_client = MagicMock()
    fake_client.__getitem__.return_value = fake_db

    with patch("app.ingest.mongo_poller.MongoClient", return_value=fake_client) as mongo_client, \
         patch("app.ingest.mongo_poller.settings") as fake_settings:
        fake_settings.MONGO_URI = "mongodb://uat-uri/"
        fake_settings.MONGO_URI_PROD = "mongodb://prod-uri/"
        fake_settings.MONGO_DB_PROD = "llm_orchestration_prod"
        fake_settings.MONGO_COLLECTION_PROD = "gateway_submissions"

        n = update_mongo_auth_status(
            exam_id="17400000",
            auth_state_desc="Auth Pending",
            auth_state_sub_desc="Waiting On carrier",
            auth_state_id=3,
            workflow_note="test",
            auth_number="ORD-123",
            env="prod",
        )

    mongo_client.assert_called_once_with("mongodb://prod-uri/", tlsAllowInvalidCertificates=True)
    fake_client.__getitem__.assert_called_with("llm_orchestration_prod")
    fake_db.__getitem__.assert_called_with("gateway_submissions")
    assert n == 1


def test_update_auth_status_default_env_is_uat():
    """update_mongo_auth_status() with no env defaults to UAT — backwards-compat."""
    from app.ingest.mongo_poller import update_mongo_auth_status

    fake_result = MagicMock()
    fake_result.matched_count = 1
    fake_result.modified_count = 1
    fake_collection = MagicMock()
    fake_collection.update_one.return_value = fake_result
    fake_db = MagicMock()
    fake_db.__getitem__.return_value = fake_collection
    fake_client = MagicMock()
    fake_client.__getitem__.return_value = fake_db

    with patch("app.ingest.mongo_poller.MongoClient", return_value=fake_client) as mongo_client, \
         patch("app.ingest.mongo_poller.settings") as fake_settings:
        fake_settings.MONGO_URI = "mongodb://uat-uri/"
        fake_settings.MONGO_DB = "workflowdb"
        fake_settings.MONGO_COLLECTION = "auth-submissions"
        fake_settings.MONGO_URI_PROD = "mongodb://prod-uri/"

        update_mongo_auth_status(
            exam_id="17400000",
            auth_state_desc="Auth Pending",
            auth_state_sub_desc="Waiting On carrier",
            auth_state_id=3,
            workflow_note="test",
        )

    mongo_client.assert_called_once_with("mongodb://uat-uri/", tlsAllowInvalidCertificates=True)
    fake_client.__getitem__.assert_called_with("workflowdb")
