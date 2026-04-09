"""Restate invocation lifecycle helpers.

Provides utilities for managing Restate workflow invocations — specifically
purging completed CaseWorkflow/SubmitWorkflow invocations before re-dispatching
a case.  Without purging, Restate's workflow idempotency silently drops
duplicate sends for the same key, causing re-runs to get stuck.
"""

import logging

import httpx

from app.core.settings import settings

logger = logging.getLogger(__name__)

# Restate endpoints — the ingress (8080) accepts workflow sends,
# the admin API (9070) handles invocation lifecycle (purge/cancel).
_INGRESS_URL = settings.RESTATE_URL          # e.g. http://172.18.1.10:8080
_ADMIN_URL = settings.RESTATE_ADMIN_URL      # e.g. http://172.18.1.10:9070

_WORKFLOW_NAMES = ("CaseWorkflow", "SubmitWorkflow")


async def purge_case_workflow(
    case_id: str,
    workflows: tuple[str, ...] | None = None,
) -> bool:
    """Purge completed workflow invocations for a case.

    Must be called before re-dispatching a case through WorkerLoop,
    otherwise Restate's workflow idempotency will silently drop the send.

    Args:
        case_id: The case ID (workflow key).
        workflows: Which workflows to purge. Defaults to all (_WORKFLOW_NAMES).
            Pass a subset to avoid accidentally creating new invocations for
            workflows that haven't run yet (the send-to-discover approach
            creates a real invocation if none exists).

    Strategy:
      1. POST /{Workflow}/{case_id}/run/send → Restate returns the existing
         invocation_id (status=PreviouslyAccepted) or creates a new one
         (status=Accepted).
      2. DELETE /invocations/{invocation_id}?mode=purge → removes completed
         invocations. In-flight invocations are left untouched.

    Returns True if all purges succeeded (or nothing to purge), False on error.
    Non-fatal: errors are logged but never raised — case reset proceeds either way.
    """
    success = True
    target_workflows = workflows or _WORKFLOW_NAMES

    async with httpx.AsyncClient(timeout=10.0) as client:
        for workflow in target_workflows:
            try:
                # Step 1: Get invocation ID via send (idempotent)
                send_resp = await client.post(
                    f"{_INGRESS_URL}/{workflow}/{case_id}/run/send",
                    json={},
                    headers={"Content-Type": "application/json"},
                )

                if send_resp.status_code not in (200, 202):
                    # No workflow registered or other issue — skip
                    logger.debug(
                        f"purge_case_workflow: {workflow}/{case_id} send returned "
                        f"{send_resp.status_code}, skipping"
                    )
                    continue

                data = send_resp.json()
                inv_id = data.get("invocationId")
                status = data.get("status")

                if not inv_id:
                    logger.debug(
                        f"purge_case_workflow: {workflow}/{case_id} no invocation_id"
                    )
                    continue

                # Step 2: Purge the invocation via admin API
                purge_resp = await client.delete(
                    f"{_ADMIN_URL}/invocations/{inv_id}",
                    params={"mode": "purge"},
                )

                if purge_resp.status_code == 202:
                    logger.info(
                        f"purge_case_workflow: purged {workflow}/{case_id} "
                        f"(inv={inv_id}, was={status})"
                    )
                else:
                    logger.warning(
                        f"purge_case_workflow: {workflow}/{case_id} purge returned "
                        f"{purge_resp.status_code}: {purge_resp.text[:200]}"
                    )
                    success = False

            except Exception as e:
                logger.warning(
                    f"purge_case_workflow: {workflow}/{case_id} failed: {e}"
                )
                success = False

    return success
