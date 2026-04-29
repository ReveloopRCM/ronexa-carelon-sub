"""Restate invocation lifecycle helpers.

Provides utilities for managing Restate workflow invocations — specifically
killing/purging existing CaseWorkflow / SubmitWorkflow / OrderWorkflow /
AwaitingClinicalWorkflow invocations before re-dispatching a case. Without
this clear, Restate's workflow-key idempotency silently drops duplicate
`workflow_send` calls — the rep's requeue is a no-op and the case sits
stuck in CLAIMED forever.

Why this rewrite (Apr 29 2026): the previous implementation had two bugs.
1. It used `DELETE /invocations/{id}?mode=purge`, which doesn't match the
   admin API's actual shape in Restate 1.6+ (the real endpoint is
   `POST /invocations/{id}/purge`). The DELETE silently 4xx'd in production.
2. To find the invocation_id it POSTed `{Workflow}/{case_id}/run/send` with
   an empty body — a "send-to-discover" trick that *creates* a new empty
   invocation if none exists, crashing on missing required fields downstream.
   The workaround was to only purge `CaseWorkflow` and never SubmitWorkflow,
   which left completed `SubmitWorkflow/{case_id}/run` invocations alive in
   Restate, deduping every subsequent SUBMIT requeue's `workflow_send` to
   the prior completed result. The case never re-ran.

The new implementation queries Restate's `sys_invocation_status` table via
the admin SQL endpoint to find invocations by `(target_service_name,
target_service_key)`, then kills + purges each via the documented POST
endpoints. No send-to-discover; all four workflow types covered by default.
"""

import logging

import httpx

from app.core.settings import settings

logger = logging.getLogger(__name__)

# Restate admin API (port 9070). Lifecycle ops + SQL.
_ADMIN_URL = settings.RESTATE_ADMIN_URL      # e.g. http://restate:9070

# All workflow services we ever route case_ids through. Default for
# `purge_case_workflow` so a single call clears every potential duplicate.
_WORKFLOW_NAMES = (
    "CaseWorkflow",
    "SubmitWorkflow",
    "OrderWorkflow",
    "AwaitingClinicalWorkflow",
)


async def _list_invocations(
    client: httpx.AsyncClient, workflow: str, case_id: str
) -> list[dict]:
    """Find invocations targeting (workflow, case_id) via Restate admin SQL.

    The admin REST API does not expose a list-invocations endpoint with a
    service+key filter; the only correct way to find invocations by key is
    the SQL view `sys_invocation_status`. Returns a list of
    {id, target, status} dicts (possibly empty).
    """
    try:
        resp = await client.post(
            f"{_ADMIN_URL}/query",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={
                "query": (
                    "SELECT id, target, status FROM sys_invocation_status "
                    "WHERE target_service_name = $1 AND target_service_key = $2"
                ),
                "params": [workflow, case_id],
            },
        )
    except Exception as e:
        logger.warning(f"_list_invocations: {workflow}/{case_id} query failed: {e}")
        return []

    if resp.status_code != 200:
        # Some versions of Restate's SQL query don't accept parameterized
        # queries; fall back to literal string substitution. Safe here
        # because workflow names come from a fixed allowlist and case_ids
        # are UUIDs from our DB (no SQL-injection vector in practice).
        try:
            resp = await client.post(
                f"{_ADMIN_URL}/query",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json={
                    "query": (
                        f"SELECT id, target, status FROM sys_invocation_status "
                        f"WHERE target_service_name = '{workflow}' "
                        f"AND target_service_key = '{case_id}'"
                    ),
                },
            )
        except Exception as e:
            logger.warning(
                f"_list_invocations: {workflow}/{case_id} fallback query failed: {e}"
            )
            return []

    if resp.status_code != 200:
        logger.warning(
            f"_list_invocations: {workflow}/{case_id} returned "
            f"{resp.status_code}: {resp.text[:200]}"
        )
        return []

    try:
        return resp.json().get("rows", []) or []
    except Exception as e:
        logger.warning(f"_list_invocations: {workflow}/{case_id} parse failed: {e}")
        return []


async def purge_case_workflow(
    case_id: str,
    workflows: tuple[str, ...] | None = None,
) -> bool:
    """Cancel + purge any workflow invocations keyed by this case_id.

    Critical for re-dispatch: Restate workflows are idempotent on key. A
    `workflow_send` with a key that already has an invocation (RUNNING or
    COMPLETED) silently dedupes to that invocation — the new send doesn't
    execute. Before re-dispatching a case we MUST clear any prior invocation,
    otherwise the rep's requeue is a no-op and the case stays CLAIMED forever.

    Args:
        case_id: The case ID (workflow key).
        workflows: Which workflows to clear. Defaults to all four
            (_WORKFLOW_NAMES) so a single call covers SUBMIT requeues that
            need SubmitWorkflow killed alongside CaseWorkflow.

    Implementation:
      1. SQL-query Restate's sys_invocation_status to find matching invocations.
      2. POST /invocations/{id}/kill — works on running invocations (terminates
         them). Idempotent: 4xx on already-completed invocations is fine.
      3. POST /invocations/{id}/purge — removes the journal record so the
         next workflow_send creates a brand-new invocation. Required because
         even completed invocations are kept for journal_retention (1d) and
         would still dedupe new sends within that window.

    Returns True if everything succeeded (or nothing to clear), False on error.
    Non-fatal: errors are logged but never raised — case reset proceeds either way.
    """
    success = True
    target_workflows = workflows or _WORKFLOW_NAMES

    async with httpx.AsyncClient(timeout=10.0) as client:
        for workflow in target_workflows:
            try:
                invocations = await _list_invocations(client, workflow, case_id)
                if not invocations:
                    logger.debug(
                        f"purge_case_workflow: {workflow}/{case_id} no invocations"
                    )
                    continue

                for inv in invocations:
                    inv_id = inv.get("id")
                    inv_status = inv.get("status")
                    if not inv_id:
                        continue

                    # Kill first (no-op on already-terminal). Don't fail hard
                    # on 4xx — invocation may already be killed/completed.
                    try:
                        kill_resp = await client.post(
                            f"{_ADMIN_URL}/invocations/{inv_id}/kill",
                            timeout=10.0,
                        )
                        if kill_resp.status_code not in (200, 202, 204, 404, 409):
                            logger.warning(
                                f"purge_case_workflow: kill {workflow}/{case_id} "
                                f"(inv={inv_id}) returned {kill_resp.status_code}: "
                                f"{kill_resp.text[:200]}"
                            )
                    except Exception as ke:
                        logger.warning(
                            f"purge_case_workflow: kill {workflow}/{case_id} "
                            f"(inv={inv_id}) failed: {ke}"
                        )

                    # Purge to remove the journal entry so future sends
                    # create a fresh invocation (not deduped to this one).
                    try:
                        purge_resp = await client.post(
                            f"{_ADMIN_URL}/invocations/{inv_id}/purge",
                            timeout=10.0,
                        )
                        if purge_resp.status_code in (200, 202, 204):
                            logger.info(
                                f"purge_case_workflow: cleared {workflow}/{case_id} "
                                f"(inv={inv_id}, was={inv_status})"
                            )
                        else:
                            logger.warning(
                                f"purge_case_workflow: purge {workflow}/{case_id} "
                                f"(inv={inv_id}) returned {purge_resp.status_code}: "
                                f"{purge_resp.text[:200]}"
                            )
                            success = False
                    except Exception as pe:
                        logger.warning(
                            f"purge_case_workflow: purge {workflow}/{case_id} "
                            f"(inv={inv_id}) failed: {pe}"
                        )
                        success = False

            except Exception as e:
                logger.warning(
                    f"purge_case_workflow: {workflow}/{case_id} failed: {e}"
                )
                success = False

    return success
