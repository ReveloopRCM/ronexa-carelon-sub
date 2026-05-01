"""Restate handler server — thin orchestration layer.

Runs on the ORCHESTRATOR VM only. All handlers are thin HTTP callers
that route work to worker VMs via plain HTTP.

Services:
  - WorkerSession (VO, keyed by worker_id) — thin HTTP caller to worker VMs
  - CaseWorkflow (Workflow, keyed by case_id) — first pass with clinical notes
  - SubmitWorkflow (Workflow, keyed by case_id) — submit with approved answers
  - OrderWorkflow (Workflow, keyed by case_id) — first pass with order form only
  - WorkerLoop (VO, keyed by worker_id) — queue polling + dispatch
  - ExtractionService (Service, stateless) — parallel OCR extraction
  - Reaper (VO, single key 'main') — periodic stale-claim/processing sweep
"""
import asyncio
import logging

import restate

from app.workflow.worker_session import worker_session
from app.workflow.case_workflow import case_workflow
from app.workflow.submit_workflow import submit_workflow
from app.workflow.awaiting_clinical_workflow import awaiting_clinical_workflow
from app.workflow.order_workflow import order_workflow
from app.workflow.extraction_service import extraction_service
from app.workflow.worker_loop import worker_loop
from app.workflow.reaper import reaper

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = restate.app(services=[
    worker_session,
    case_workflow,
    submit_workflow,
    awaiting_clinical_workflow,
    order_workflow,
    worker_loop,
    extraction_service,
    reaper,
])

if __name__ == "__main__":
    import hypercorn.asyncio
    import hypercorn.config

    config = hypercorn.config.Config()
    config.bind = ["0.0.0.0:9080"]
    logger.info("Starting Restate handler server on port 9080")
    asyncio.run(hypercorn.asyncio.serve(app, config))
