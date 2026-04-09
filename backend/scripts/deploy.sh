#!/bin/bash
set -e

# Ronexa Deploy Script
# Safely deploys code changes by killing Restate invocations first.
#
# Usage:
#   ./scripts/deploy.sh                  # Full deploy (orchestrator)
#   ./scripts/deploy.sh --worker-only    # Worker-only deploy (no Restate impact)
#   ./scripts/deploy.sh --kill-only      # Just kill invocations + reset cases

RESTATE_ADMIN="http://localhost:9070"
RESTATE_WORKER="http://localhost:9080"

echo "========================================="
echo "  Ronexa Deploy — $(date)"
echo "========================================="

# ── Step 1: Kill all in-flight Restate invocations ──

if [ "$1" != "--worker-only" ]; then
    echo ""
    echo "Step 1: Killing all Restate invocations..."

    # Get all invocation IDs from Restate logs
    INV_IDS=$(docker logs restate 2>&1 | grep -oP 'inv_[A-Za-z0-9]+' | sort -u)
    INV_COUNT=$(echo "$INV_IDS" | grep -c 'inv_' || true)

    if [ "$INV_COUNT" -gt 0 ]; then
        KILLED=0
        for inv in $INV_IDS; do
            CODE=$(curl -s -X DELETE "${RESTATE_ADMIN}/invocations/${inv}?mode=kill" -o /dev/null -w "%{http_code}")
            if [ "$CODE" = "202" ]; then
                KILLED=$((KILLED + 1))
            fi
        done
        echo "  Killed $KILLED / $INV_COUNT invocations"
    else
        echo "  No invocations found"
    fi

    # ── Step 2: Reset stuck cases ──

    echo ""
    echo "Step 2: Resetting stuck cases..."

    docker exec backend-api python3 -c "
import asyncio
from app.db.database import async_session_factory
from sqlalchemy import text

async def reset():
    async with async_session_factory() as db:
        r1 = await db.execute(text(\"UPDATE cases SET state = 'NOTES_UPLOADED' WHERE state = 'PROCESSING'\"))
        r2 = await db.execute(text(\"UPDATE submission_jobs SET status = 'QUEUED', claimed_by = NULL WHERE status = 'RUNNING'\"))
        await db.commit()
        print(f'  Reset {r1.rowcount} PROCESSING cases, {r2.rowcount} RUNNING jobs')

asyncio.run(reset())
" 2>&1

    if [ "$1" = "--kill-only" ]; then
        echo ""
        echo "Done (kill-only mode)."
        exit 0
    fi

    # ── Step 3: Rebuild and restart orchestrator services ──

    echo ""
    echo "Step 3: Rebuilding and restarting services..."
    docker compose up -d --force-recreate backend-api
    echo "  backend-api restarted"

    # Wait for API to be ready
    echo "  Waiting for API..."
    for i in $(seq 1 30); do
        if curl -s http://localhost:8000/api/health > /dev/null 2>&1; then
            echo "  API ready"
            break
        fi
        sleep 1
    done

    # ── Step 4: Re-register Restate deployment ──

    echo ""
    echo "Step 4: Re-registering Restate deployment..."
    RESP=$(curl -s -X POST "${RESTATE_ADMIN}/deployments" \
        -H 'Content-Type: application/json' \
        -d "{\"uri\":\"${RESTATE_WORKER}\",\"force\":true}" \
        -o /dev/null -w "%{http_code}")
    echo "  Registration response: $RESP"

else
    # Worker-only deploy — no Restate impact
    echo ""
    echo "Worker-only deploy — no Restate changes needed."
    echo "Workers are plain HTTP servers. Just restart the worker service:"
    echo "  ssh worker-a 'sudo systemctl restart ronexa-worker-http'"
    echo "  ssh worker-b 'sudo systemctl restart ronexa-worker-http'"
    echo "  ssh worker-c 'sudo systemctl restart ronexa-worker-http'"
fi

# ── Final: Show current state ──

echo ""
echo "========================================="
echo "  Deploy complete"
echo "========================================="

docker exec backend-api python3 -c "
import asyncio
from app.db.database import async_session_factory
from sqlalchemy import text

async def show():
    async with async_session_factory() as db:
        r = await db.execute(text(\"SELECT state, COUNT(*) FROM cases GROUP BY state ORDER BY COUNT(*) DESC\"))
        print('  Case states:')
        for row in r.all():
            print(f'    {row[0]}: {row[1]}')

asyncio.run(show())
" 2>&1

echo ""
