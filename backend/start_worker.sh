#!/bin/bash
# Start both the Restate handler and the Worker HTTP server,
# then self-register with the Restate runtime (force=true).
#
# This ensures new/changed services are always discovered after restart.
# Without force=true, Restate keeps stale service definitions and
# new services (like OrderWorkflow) are silently missing.

python worker_http.py &

python restate_worker.py &
HANDLER_PID=$!

# Wait for handler to be ready (up to 30s)
echo "Waiting for Restate handler on port 9080..."
for i in $(seq 1 30); do
    if curl -sf http://localhost:9080/discover > /dev/null 2>&1; then
        echo "Restate handler ready after ${i}s"
        break
    fi
    sleep 1
done

# Self-register with Restate admin (force=true to update service definitions)
ADMIN_URL="${RESTATE_ADMIN_URL:-http://restate:9070}"
# Use the Docker service name so Restate can route back to this container
SELF_URI="${RESTATE_SELF_URI:-http://restate-handler:9080/}"

echo "Registering deployment at ${ADMIN_URL} → ${SELF_URI} (force=true)"
RESP=$(curl -sf -X POST "${ADMIN_URL}/deployments" \
    -H "Content-Type: application/json" \
    -d "{\"uri\": \"${SELF_URI}\", \"force\": true}" 2>&1)

if [ $? -eq 0 ]; then
    echo "Restate registration successful"
else
    echo "WARNING: Restate registration failed (will retry via backend-api): ${RESP}"
fi

# Keep foreground on the handler process
wait $HANDLER_PID
