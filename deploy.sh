#!/usr/bin/env bash
set -euo pipefail

# ─── Ronexa Production Deploy ───
#
# Single command to deploy code to production. Auto-detects what changed.
# Backend changes ALWAYS deploy to BOTH orchestrator AND workers.
#
# Usage:
#   ./deploy.sh              # Auto-detect changes, deploy only what changed
#   ./deploy.sh backend      # Force: backend containers + workers + Restate cleanup
#   ./deploy.sh frontend     # Force: frontend container only
#   ./deploy.sh workers      # Force: worker code only (no image rebuild)
#   ./deploy.sh all          # Force: everything
#   ./deploy.sh restart      # Restart all containers + refresh nginx (no rebuild)
#   ./deploy.sh status       # Health checks only, no deploy

# ════════════════════════════════════════════════════════════════════
# Section 1: Config
# ════════════════════════════════════════════════════════════════════

ACR="ronexaacr.azurecr.io"
RG="rg-ronexa-prod"
ORCH_VM="ronexa-orchestrator"
ORCH_IP="20.29.73.195"
WORKER_VMS=("ronexa-worker-a" "ronexa-worker-b" "ronexa-worker-c")
WORKER_IPS=("172.202.22.112" "74.249.202.85" "20.106.37.51")
WORKER_DIR="/home/ronexa/ronexa"
SSH_USER="ronexa"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=10"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEPLOY_STATE_FILE="${PROJECT_DIR}/.last-deploy"
COMPOSE_FILE="docker-compose.prod.yml"

# Workflow files that change Restate journal shape — must kill invocations before deploy
WORKFLOW_FILES=(
  "backend/app/workflow/worker_loop.py"
  "backend/app/workflow/worker_session.py"
  "backend/app/workflow/case_workflow.py"
  "backend/app/workflow/submit_workflow.py"
  "backend/app/workflow/order_workflow.py"
  "backend/app/workflow/awaiting_clinical_workflow.py"
  "backend/app/workflow/extraction_service.py"
)

TARGET="${1:-auto}"

# CI mode — set CI=true (or any non-empty CI env var) to:
#  - skip the uncommitted-changes prompt (CI has a clean checkout)
#  - auto-run `alembic upgrade head` on the orchestrator
#  - auto-restart WorkerLoops via Restate API after deploy
# Local human operators keep the existing interactive behavior by default.
CI="${CI:-false}"

# ════════════════════════════════════════════════════════════════════
# Section 2: Helpers
# ════════════════════════════════════════════════════════════════════

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log_step()  { echo -e "${CYAN}▸${NC} $*"; }
log_ok()    { echo -e "${GREEN}✓${NC} $*"; }
log_warn()  { echo -e "${YELLOW}⚠${NC} $*"; }
log_err()   { echo -e "${RED}✗${NC} $*"; }

# Run a command on the orchestrator VM, return clean stdout only
run_on_orch() {
  local scripts="$1"
  # v161 CI/CD: when CI=true (GitHub Actions runner), use SSH directly.
  # The runner has the deploy keypair + known_hosts secrets installed
  # and the AcrPush-only SP doesn't carry the VM run-command permission.
  # Interactive operators keep az vm run-command — works on any laptop
  # that's az logged in without needing an SSH config.
  if [ "${CI}" != "false" ]; then
    ssh ${SSH_OPTS} "${SSH_USER}@${ORCH_IP}" "${scripts}" 2>/dev/null || { log_err "Failed to reach ${ORCH_VM} via SSH"; return 1; }
    return
  fi
  local result
  result=$(az vm run-command invoke \
    --resource-group "${RG}" --name "${ORCH_VM}" \
    --command-id RunShellScript --scripts "${scripts}" \
    2>/dev/null) || { log_err "Failed to reach ${ORCH_VM}"; return 1; }
  # Extract stdout section from az vm run-command JSON output
  echo "${result}" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    msg = d['value'][0]['message']
    lines = msg.split('\n')
    in_stdout = False
    out = []
    for line in lines:
        if line.strip() == '[stdout]':
            in_stdout = True
            continue
        if line.strip() == '[stderr]':
            in_stdout = False
            continue
        if in_stdout:
            out.append(line)
    print('\n'.join(out).strip())
except Exception:
    print('')
" 2>/dev/null || true
}

# Run a command on a worker VM, return clean stdout only
run_on_worker() {
  local vm="$1"
  local scripts="$2"
  # v161 CI/CD: same SSH-in-CI rationale as run_on_orch above.
  if [ "${CI}" != "false" ]; then
    # Map vm-name → IP from the WORKER_VMS/WORKER_IPS arrays declared above.
    local ip=""
    local i
    for i in "${!WORKER_VMS[@]}"; do
      if [ "${WORKER_VMS[$i]}" = "${vm}" ]; then
        ip="${WORKER_IPS[$i]}"
        break
      fi
    done
    if [ -z "${ip}" ]; then
      log_err "No IP mapping found for worker VM ${vm}"; return 1
    fi
    ssh ${SSH_OPTS} "${SSH_USER}@${ip}" "${scripts}" 2>/dev/null || { log_err "Failed to reach ${vm} via SSH"; return 1; }
    return
  fi
  local result
  result=$(az vm run-command invoke \
    --resource-group "${RG}" --name "${vm}" \
    --command-id RunShellScript --scripts "${scripts}" \
    2>/dev/null) || { log_err "Failed to reach ${vm}"; return 1; }
  # Extract stdout section from az vm run-command JSON output
  echo "${result}" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    msg = d['value'][0]['message']
    lines = msg.split('\n')
    in_stdout = False
    out = []
    for line in lines:
        if line.strip() == '[stdout]':
            in_stdout = True
            continue
        if line.strip() == '[stderr]':
            in_stdout = False
            continue
        if in_stdout:
            out.append(line)
    print('\n'.join(out).strip())
except Exception:
    print('')
" 2>/dev/null || true
}

# ════════════════════════════════════════════════════════════════════
# Section 3: Versioning
# ════════════════════════════════════════════════════════════════════

# Find the current production tags by asking the orchestrator
get_current_tags() {
  local output
  output=$(run_on_orch "
BTAG=\$(docker inspect backend-api --format '{{.Config.Image}}' 2>/dev/null | grep -o 'v[0-9]*' || echo 'v0')
FTAG=\$(docker inspect frontend --format '{{.Config.Image}}' 2>/dev/null | grep -o 'v[0-9]*' || docker inspect ronexa-frontend --format '{{.Config.Image}}' 2>/dev/null | grep -o 'v[0-9]*' || docker inspect auth-ops-frontend --format '{{.Config.Image}}' 2>/dev/null | grep -o 'v[0-9]*' || echo 'v0')
echo \"BACKEND=\${BTAG}\"
echo \"FRONTEND=\${FTAG}\"
") || true
  CURRENT_BACKEND_TAG=$(echo "${output}" | grep 'BACKEND=' | sed 's/.*BACKEND=//' | tr -d '[:space:]' || true)
  CURRENT_FRONTEND_TAG=$(echo "${output}" | grep 'FRONTEND=' | sed 's/.*FRONTEND=//' | tr -d '[:space:]' || true)
  : "${CURRENT_BACKEND_TAG:=v0}"
  : "${CURRENT_FRONTEND_TAG:=v0}"
}

next_tag() {
  local current="$1"
  local num="${current#v}"
  echo "v$((num + 1))"
}

save_deploy_state() {
  git rev-parse HEAD > "${DEPLOY_STATE_FILE}"
  log_ok "Deploy state saved ($(git rev-parse --short HEAD))"
}

# ════════════════════════════════════════════════════════════════════
# Section 3.5: Pre-flight safety checks
# ════════════════════════════════════════════════════════════════════

preflight_checks() {
  local has_warnings=false

  # Check for uncommitted changes
  local uncommitted
  uncommitted=$(git status --porcelain 2>/dev/null | grep -c "^ M\|^M \|^??" || true)
  if [ "${uncommitted}" -gt 0 ]; then
    log_warn "UNCOMMITTED CHANGES DETECTED (${uncommitted} files)"
    log_warn "These changes will NOT be included in the Docker images!"
    git status --short | head -10
    echo ""
    has_warnings=true
  fi

  # Check for staged but uncommitted changes
  local staged
  staged=$(git diff --cached --name-only 2>/dev/null | wc -l | tr -d ' ')
  if [ "${staged}" -gt 0 ]; then
    log_warn "STAGED BUT UNCOMMITTED CHANGES (${staged} files)"
    log_warn "Run 'git commit' before deploying!"
    has_warnings=true
  fi

  # Check that HEAD matches what we're deploying
  local head_msg
  head_msg=$(git log --oneline -1 2>/dev/null || echo "unknown")
  log_step "Deploying commit: ${head_msg}"

  if [ "${has_warnings}" = true ]; then
    if [ "${CI}" != "false" ]; then
      log_err "Uncommitted changes detected in CI mode — refusing to deploy."
      exit 1
    fi
    echo ""
    read -p "Continue with deploy despite warnings? (y/N) " -n 1 -r
    echo ""
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
      log_err "Deploy aborted. Commit your changes first."
      exit 1
    fi
  fi
}

# ════════════════════════════════════════════════════════════════════
# Section 4: Change detection
# ════════════════════════════════════════════════════════════════════

BACKEND_CHANGED=false
FRONTEND_CHANGED=false
WORKFLOW_CHANGED=false

detect_changes() {
  if [ ! -f "${DEPLOY_STATE_FILE}" ]; then
    log_warn "No .last-deploy found — treating everything as changed"
    BACKEND_CHANGED=true
    FRONTEND_CHANGED=true
    WORKFLOW_CHANGED=true
    return
  fi

  local last_commit
  last_commit=$(cat "${DEPLOY_STATE_FILE}")

  # Check if the commit still exists in git history
  if ! git cat-file -t "${last_commit}" > /dev/null 2>&1; then
    log_warn "Last deploy commit ${last_commit} not found — treating everything as changed"
    BACKEND_CHANGED=true
    FRONTEND_CHANGED=true
    WORKFLOW_CHANGED=true
    return
  fi

  local changed_files
  changed_files=$(git diff --name-only "${last_commit}" HEAD 2>/dev/null || echo "")

  if [ -z "${changed_files}" ]; then
    log_ok "No changes since last deploy ($(echo "${last_commit}" | cut -c1-7))"
    return
  fi

  # Check backend changes
  if echo "${changed_files}" | grep -q "^backend/"; then
    BACKEND_CHANGED=true
  fi

  # Check frontend changes
  if echo "${changed_files}" | grep -q "^frontend/"; then
    FRONTEND_CHANGED=true
  fi

  # Check docker-compose or infra changes (affects orchestrator)
  if echo "${changed_files}" | grep -qE "docker-compose|infra/nginx"; then
    BACKEND_CHANGED=true
    FRONTEND_CHANGED=true
  fi

  # Check workflow files (need Restate cleanup)
  for wf in "${WORKFLOW_FILES[@]}"; do
    if echo "${changed_files}" | grep -q "${wf}"; then
      WORKFLOW_CHANGED=true
      break
    fi
  done

  echo ""
  log_step "Changes since last deploy ($(echo "${last_commit}" | cut -c1-7)):"
  [ "${BACKEND_CHANGED}" = true ]  && echo "  Backend:  changed"
  [ "${FRONTEND_CHANGED}" = true ] && echo "  Frontend: changed"
  [ "${WORKFLOW_CHANGED}" = true ] && echo "  Workflow: changed (will kill Restate invocations)"
  if [ "${BACKEND_CHANGED}" = false ] && [ "${FRONTEND_CHANGED}" = false ]; then
    echo "  (no deployable changes detected)"
  fi
  echo ""
}

# ════════════════════════════════════════════════════════════════════
# Section 5: Build + Push
# ════════════════════════════════════════════════════════════════════

build_backend() {
  local tag="$1"
  log_step "Building backend-api:${tag} ..."
  docker build --platform linux/amd64 \
    -t "${ACR}/backend-api:${tag}" \
    "${PROJECT_DIR}/backend" 2>&1 | tail -3
  log_step "Pushing backend-api:${tag} ..."
  docker push "${ACR}/backend-api:${tag}" 2>&1 | tail -3
  log_ok "Backend ${tag} pushed to ACR"
}

build_frontend() {
  local tag="$1"
  # Build under BOTH image names for backward compat
  # (some orchestrators have auth-ops-frontend, others have ronexa-frontend)
  log_step "Building frontend:${tag} ..."
  docker build --platform linux/amd64 \
    -t "${ACR}/auth-ops-frontend:${tag}" \
    -t "${ACR}/ronexa-frontend:${tag}" \
    "${PROJECT_DIR}/frontend" 2>&1 | tail -3
  log_step "Pushing frontend:${tag} ..."
  docker push "${ACR}/auth-ops-frontend:${tag}" 2>&1 | tail -3
  docker push "${ACR}/ronexa-frontend:${tag}" 2>&1 | tail -3
  log_ok "Frontend ${tag} pushed to ACR"
}

# ════════════════════════════════════════════════════════════════════
# Section 6: Restate cleanup
# ════════════════════════════════════════════════════════════════════

kill_restate_invocations() {
  log_step "Killing in-flight Restate invocations ..."
  local output
  output=$(run_on_orch "
python3 << 'PYEOF'
import urllib.request, json

# Query all running/suspended invocations
targets = ['WorkerLoop', 'WorkerSession', 'CaseWorkflow', 'SubmitWorkflow', 'AwaitingClinicalWorkflow', 'ExtractionService']
killed = 0

for svc in targets:
    try:
        req = urllib.request.Request(
            'http://localhost:9070/query',
            data=json.dumps({'query': f\"SELECT id FROM sys_invocation WHERE target LIKE '{svc}%' AND status IN ('running', 'suspended', 'backing-off')\"}).encode(),
            headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
        )
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read())
        for row in data.get('rows', []):
            inv_id = row.get('id', '')
            if inv_id:
                try:
                    kill_req = urllib.request.Request(
                        f'http://localhost:9070/invocations/{inv_id}',
                        method='DELETE',
                    )
                    urllib.request.urlopen(kill_req, timeout=5)
                    killed += 1
                except Exception as e:
                    print(f'  Failed to kill {inv_id}: {e}')
    except Exception as e:
        pass

print(f'Killed {killed} invocations')
PYEOF
") || true
  echo "  ${output}" | head -5
  log_ok "Restate invocations cleaned up"
}

# ════════════════════════════════════════════════════════════════════
# Section 7: Deploy orchestrator
# ════════════════════════════════════════════════════════════════════

sync_configs() {
  log_step "Syncing configs to orchestrator ..."

  # Sync nginx.conf (mounted by compose at ./nginx.conf)
  local nginx_conf
  nginx_conf=$(cat "${PROJECT_DIR}/infra/nginx/nginx.conf")
  run_on_orch "
cat > ${WORKER_DIR}/nginx.conf << 'NGEOF'
${nginx_conf}
NGEOF
echo 'nginx.conf synced'
" || { log_warn "Failed to sync nginx config"; }

  log_ok "Configs synced"
}

deploy_orch() {
  local backend_tag="$1"
  local frontend_tag="$2"

  log_step "Deploying orchestrator (backend=${backend_tag}, frontend=${frontend_tag}) ..."

  # Sync nginx config first so compose picks it up via volume mount
  sync_configs

  local output
  output=$(run_on_orch "
cd ${WORKER_DIR}

# Update image tags in compose file
sed -i 's|backend-api:v[0-9]*|backend-api:${backend_tag}|g' docker-compose.yml
sed -i 's|auth-ops-frontend:v[0-9]*|auth-ops-frontend:${frontend_tag}|g; s|ronexa-frontend:v[0-9]*|ronexa-frontend:${frontend_tag}|g' docker-compose.yml

# Pull new images
docker pull ${ACR}/backend-api:${backend_tag} 2>&1 | tail -1
docker pull ${ACR}/auth-ops-frontend:${frontend_tag} 2>&1 | tail -1

# Deploy all services via compose (nginx included — uses volume mount for config)
docker compose up -d 2>&1

# Wait for health
for i in \$(seq 1 20); do
  if curl -sf http://localhost:8000/api/health > /dev/null 2>&1; then
    echo 'Backend healthy'
    break
  fi
  [ \"\$i\" -eq 20 ] && echo 'Backend health timeout'
  sleep 3
done

# Verify nginx routing
sleep 2
curl -sf --max-time 5 http://localhost/api/health > /dev/null 2>&1 && echo 'Nginx routing OK' || echo 'Nginx routing FAILED'
") || { log_err "Orchestrator deploy failed"; return 1; }
  echo "  ${output}" | tail -5
  log_ok "Orchestrator deployed"
}

restart_orch() {
  log_step "Restarting orchestrator containers ..."

  # Sync nginx config
  sync_configs

  local output
  output=$(run_on_orch "
cd ${WORKER_DIR}

# Recreate all compose services (picks up nginx config changes via volume mount)
docker compose up -d --force-recreate 2>&1

# Wait for health
for i in \$(seq 1 20); do
  if curl -sf http://localhost:8000/api/health > /dev/null 2>&1; then
    echo 'Backend healthy'
    break
  fi
  [ \"\$i\" -eq 20 ] && echo 'Backend health timeout'
  sleep 3
done

# Verify nginx routing
sleep 2
curl -sf http://localhost/api/health > /dev/null 2>&1 && echo 'Nginx routing OK' || echo 'Nginx routing FAILED'
") || { log_err "Restart failed"; return 1; }
  echo "  ${output}" | tail -5
  log_ok "Orchestrator restarted"
}

# ════════════════════════════════════════════════════════════════════
# Section 8: Deploy workers
# ════════════════════════════════════════════════════════════════════

deploy_workers() {
  log_step "Deploying to worker VMs ..."

  cd "${PROJECT_DIR}"

  # Tar the ENTIRE backend/app/ directory + top-level worker files + portals
  local tar_path="/tmp/worker-deploy.tar.gz"
  tar czf "${tar_path}" \
    backend/app/ \
    backend/worker_http.py \
    backend/restate_worker.py \
    backend/main.py \
    backend/portals/ \
    2>/dev/null

  local tar_size
  tar_size=$(wc -c < "${tar_path}" | tr -d ' ')
  log_step "Worker tarball: $(( tar_size / 1024 ))KB"

  local i=0
  for vm in "${WORKER_VMS[@]}"; do
    local ip="${WORKER_IPS[$i]}"
    i=$((i + 1))
    log_step "  Deploying to ${vm} (${ip}) ..."

    # SCP + SSH — much more reliable than az vm run-command for large payloads
    scp ${SSH_OPTS} "${tar_path}" "${SSH_USER}@${ip}:~/deploy.tar.gz" 2>/dev/null || {
      log_warn "  ${vm}: SCP failed, falling back to az vm run-command"
      # Fallback: base64 via az vm run-command
      local b64_content
      b64_content=$(base64 -i "${tar_path}")
      run_on_worker "${vm}" "
echo '${b64_content}' | base64 -d > ~/deploy.tar.gz && \
cd ${WORKER_DIR} && tar xzf ~/deploy.tar.gz && \
sudo systemctl restart ronexa-worker ronexa-worker-http && \
rm -f ~/deploy.tar.gz && echo 'OK'" || { log_warn "  ${vm} fallback also failed"; continue; }
      continue
    }

    local output
    output=$(ssh ${SSH_OPTS} "${SSH_USER}@${ip}" \
      "cd ${WORKER_DIR} && tar xzf ~/deploy.tar.gz && sudo systemctl restart ronexa-worker ronexa-worker-http && rm -f ~/deploy.tar.gz && echo 'OK'" 2>&1) || {
      log_warn "  ${vm}: SSH command failed"
      continue
    }

    if echo "${output}" | grep -q "OK"; then
      log_ok "  ${vm} deployed"
    else
      log_warn "  ${vm}: ${output}"
    fi
  done

  rm -f "${tar_path}"
  log_ok "Workers deployed"
}

# ════════════════════════════════════════════════════════════════════
# Section 9: Re-register Restate
# ════════════════════════════════════════════════════════════════════

reregister_restate() {
  log_step "Re-registering Restate deployment ..."
  local output
  output=$(run_on_orch "
sleep 3
curl -sf -X POST http://localhost:9070/deployments \
  -H 'content-type: application/json' \
  -d '{\"uri\": \"http://restate-handler:9080\", \"force\": true}' 2>&1 | \
  python3 -c \"import sys,json; d=json.load(sys.stdin); print(f'ID: {d.get(\\\"id\\\", \\\"?\\\")}, services: {[s[\\\"name\\\"] for s in d.get(\\\"services\\\", [])]}')\" 2>&1
") || { log_warn "Restate registration may have failed"; return; }
  echo "  ${output}"
  log_ok "Restate registered"

  # Deregister any stale deployments that aren't the current one. Restate
  # keeps every deployment that ever registered, and in-flight invocations
  # stay pinned to their original deployment's service revision. After a
  # version bump, leftover old deployments cause RT0007/RT0010 errors when
  # the runtime tries to invoke rev-N services against rev-N+M code. We
  # extract the current deployment id from the registration output and
  # force-delete every other deployment in the registry.
  log_step "Pruning stale Restate deployments ..."
  local current_id
  current_id=$(printf '%s' "${output}" | sed -n 's/^ID: \([^,]*\),.*$/\1/p')
  if [[ -z "${current_id}" || "${current_id}" == "?" ]]; then
    log_warn "Could not parse current deployment id; skipping stale deployment cleanup"
    return
  fi
  local pruned
  pruned=$(run_on_orch "
curl -sf http://localhost:9070/deployments 2>/dev/null | python3 -c \"
import sys, json, urllib.request
data = json.load(sys.stdin)
current = '${current_id}'
n = 0
for d in data.get('deployments', []):
    did = d.get('id')
    if did and did != current:
        req = urllib.request.Request(f'http://localhost:9070/deployments/{did}?force=true', method='DELETE')
        try:
            urllib.request.urlopen(req, timeout=10)
            print(f'pruned {did} ({d.get(\\\"uri\\\", \\\"?\\\")})')
            n += 1
        except Exception as e:
            print(f'WARN: failed to prune {did}: {e}', file=sys.stderr)
print(f'pruned_total={n}')
\" 2>&1
") || { log_warn "Stale deployment prune failed"; return; }
  echo "${pruned}" | sed 's/^/  /'
  log_ok "Stale deployments pruned"
}

# ════════════════════════════════════════════════════════════════════
# Section 10: Health checks
# ════════════════════════════════════════════════════════════════════

health_check() {
  echo ""
  log_step "Running health checks ..."
  echo ""

  # Orchestrator
  log_step "Orchestrator (${ORCH_VM}):"
  local orch_output
  orch_output=$(run_on_orch "
echo \"  Backend:  \$(curl -sf http://localhost:8000/api/health 2>&1 || echo 'UNREACHABLE')\"
echo \"  Restate:  \$(curl -sf http://localhost:9070/health 2>&1 | head -c 50 || echo 'UNREACHABLE')\"
echo \"  Frontend: HTTP \$(curl -sf -o /dev/null -w '%{http_code}' http://localhost:3000 2>&1 || echo '000')\"
echo \"  Nginx→API: \$(curl -sf --max-time 5 http://localhost/api/health 2>&1 || echo 'BROKEN (502/unreachable)')\"
echo ''
echo '  Containers:'
docker ps --format '    {{.Names}}\t{{.Image}}\t{{.Status}}' | grep -E 'backend|restate|frontend|nginx' || echo '    (no matching containers)'
") || { log_err "Cannot reach orchestrator"; }
  echo "${orch_output}"
  echo ""

  # Workers
  log_step "Workers:"
  local i=0
  for vm in "${WORKER_VMS[@]}"; do
    local ip="${WORKER_IPS[$i]}"
    i=$((i + 1))
    local w_output
    w_output=$(ssh ${SSH_OPTS} "${SSH_USER}@${ip}" "
ACTIVE_W=\$(systemctl is-active ronexa-worker 2>/dev/null || echo 'inactive')
ACTIVE_H=\$(systemctl is-active ronexa-worker-http 2>/dev/null || echo 'inactive')
STATUS=\$(curl -sf http://localhost:9081/status 2>/dev/null || echo '{\"error\":\"unreachable\"}')
BROWSERS=\$(echo \"\$STATUS\" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get(\"browser_count\",\"?\"))' 2>/dev/null || echo '?')
echo \"  ${vm}: worker=\${ACTIVE_W} http=\${ACTIVE_H} browsers=\${BROWSERS}\"
" 2>/dev/null) || { log_warn "  ${vm}: unreachable"; continue; }
    echo "${w_output}"
  done

  echo ""
}

# ════════════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════════════

echo ""
echo "═══════════════════════════════════════"
echo "  Ronexa Deploy"
echo "  Mode: ${TARGET}"
echo "═══════════════════════════════════════"
echo ""

# Run preflight checks for any deploy mode that builds images
if [[ "${TARGET}" =~ ^(auto|backend|frontend|all)$ ]]; then
  preflight_checks
fi

case "${TARGET}" in

  # ── Auto: detect changes, deploy only what's needed ──
  auto)
    detect_changes

    if [ "${BACKEND_CHANGED}" = false ] && [ "${FRONTEND_CHANGED}" = false ]; then
      log_ok "Nothing to deploy"
      exit 0
    fi

    # Get current production tags
    get_current_tags
    log_step "Current production: backend=${CURRENT_BACKEND_TAG}, frontend=${CURRENT_FRONTEND_TAG}"

    # ACR login
    az acr login --name ronexaacr 2>/dev/null || { log_err "ACR login failed"; exit 1; }

    BTAG="${CURRENT_BACKEND_TAG}"
    FTAG="${CURRENT_FRONTEND_TAG}"

    if [ "${BACKEND_CHANGED}" = true ]; then
      BTAG=$(next_tag "${CURRENT_BACKEND_TAG}")
      build_backend "${BTAG}"
    fi

    if [ "${FRONTEND_CHANGED}" = true ]; then
      FTAG=$(next_tag "${CURRENT_FRONTEND_TAG}")
      build_frontend "${FTAG}"
    fi

    if [ "${WORKFLOW_CHANGED}" = true ]; then
      kill_restate_invocations
    fi

    if [ "${BACKEND_CHANGED}" = true ] || [ "${FRONTEND_CHANGED}" = true ]; then
      deploy_orch "${BTAG}" "${FTAG}"
    fi

    if [ "${BACKEND_CHANGED}" = true ]; then
      deploy_workers
      reregister_restate
    fi

    health_check
    save_deploy_state
    ;;

  # ── Force backend: containers + workers + Restate cleanup ──
  backend)
    get_current_tags
    az acr login --name ronexaacr 2>/dev/null || { log_err "ACR login failed"; exit 1; }

    BTAG=$(next_tag "${CURRENT_BACKEND_TAG}")
    FTAG="${CURRENT_FRONTEND_TAG}"

    build_backend "${BTAG}"
    kill_restate_invocations
    deploy_orch "${BTAG}" "${FTAG}"
    deploy_workers
    reregister_restate
    health_check
    save_deploy_state
    ;;

  # ── Force frontend only ──
  frontend)
    get_current_tags
    az acr login --name ronexaacr 2>/dev/null || { log_err "ACR login failed"; exit 1; }

    BTAG="${CURRENT_BACKEND_TAG}"
    FTAG=$(next_tag "${CURRENT_FRONTEND_TAG}")

    build_frontend "${FTAG}"
    deploy_orch "${BTAG}" "${FTAG}"
    health_check
    save_deploy_state
    ;;

  # ── Force workers only (no image rebuild) ──
  workers)
    deploy_workers
    health_check
    ;;

  # ── Force everything ──
  all)
    get_current_tags
    az acr login --name ronexaacr 2>/dev/null || { log_err "ACR login failed"; exit 1; }

    BTAG=$(next_tag "${CURRENT_BACKEND_TAG}")
    FTAG=$(next_tag "${CURRENT_FRONTEND_TAG}")

    build_backend "${BTAG}"
    build_frontend "${FTAG}"
    kill_restate_invocations
    deploy_orch "${BTAG}" "${FTAG}"
    deploy_workers
    reregister_restate
    health_check
    save_deploy_state
    ;;

  # ── Restart: quick fix — restart containers + refresh nginx config ──
  restart)
    restart_orch
    health_check
    ;;

  # ── Status only ──
  status)
    get_current_tags
    log_step "Production tags: backend=${CURRENT_BACKEND_TAG}, frontend=${CURRENT_FRONTEND_TAG}"
    if [ -f "${DEPLOY_STATE_FILE}" ]; then
      log_step "Last deploy: $(cut -c1-7 < "${DEPLOY_STATE_FILE}")"
    else
      log_warn "No .last-deploy file — unknown last deploy"
    fi
    health_check
    ;;

  *)
    echo "Usage: $0 [auto|backend|frontend|workers|all|restart|status]"
    echo ""
    echo "  auto      Auto-detect changes and deploy (default)"
    echo "  backend   Force: backend containers + workers + Restate cleanup"
    echo "  frontend  Force: frontend container only"
    echo "  workers   Force: worker code only (no image rebuild)"
    echo "  all       Force: everything"
    echo "  restart   Restart containers + refresh nginx (no rebuild)"
    echo "  status    Health checks only"
    exit 1
    ;;
esac

echo ""
echo "═══════════════════════════════════════"
echo "  Deploy complete"
echo "═══════════════════════════════════════"
echo ""

# CI mode — auto-run the post-deploy steps that human operators do manually.
# Skipped in interactive mode (you might want to inspect first / batch migrations).
if [[ "${TARGET}" =~ ^(auto|backend|all)$ ]] && [ "${CI}" != "false" ]; then
  log_step "CI mode — running post-deploy steps automatically ..."

  log_step "  Applying any pending alembic migrations ..."
  run_on_orch "docker exec backend-api alembic upgrade head 2>&1 | tail -8" || log_warn "  migration step returned non-zero"

  log_step "  Re-arming WorkerLoops via Restate API ..."
  run_on_orch "docker exec backend-api python3 -c \"
import asyncio, httpx
from sqlalchemy import select
from app.db.database import async_session_factory
from app.db.models import WorkerAccount
from app.core.settings import settings as env_settings

async def main():
    async with async_session_factory() as db:
        result = await db.execute(select(WorkerAccount).where(WorkerAccount.is_active == True))
        workers = result.scalars().all()
    async with httpx.AsyncClient(timeout=10.0) as client:
        for w in workers:
            r = await client.post(
                f'{env_settings.RESTATE_URL}/WorkerLoop/{w.container_id}/start/send',
                json={'max_cases': None, 'job_type': w.job_type or 'FIRST_PASS'},
                headers={'Content-Type': 'application/json'},
            )
            print(f'  {w.container_id} ({w.job_type}): {r.status_code}')
asyncio.run(main())
\"" || log_warn "  WorkerLoop restart returned non-zero"

  log_ok "Post-deploy auto-steps complete"
  echo ""
fi

# Post-deploy reminders (interactive mode only)
if [[ "${TARGET}" =~ ^(auto|backend|all)$ ]] && [ "${CI}" = "false" ]; then
  echo -e "${YELLOW}Post-deploy checklist:${NC}"
  echo "  1. Run migrations:  ssh ${SSH_USER}@${ORCH_IP} 'docker exec backend-api alembic upgrade head'"
  echo "  2. Start WorkerLoops if they were killed (check Restate admin)"
  echo "  3. Monitor first few cases in production logs"
  echo ""

  # Check for pending alembic migrations
  migration_count=$(find "${PROJECT_DIR}/backend/alembic/versions" -name "*.py" -newer "${DEPLOY_STATE_FILE}" 2>/dev/null | wc -l | tr -d ' ')
  if [ "${migration_count}" -gt 0 ]; then
    echo -e "${RED}⚠  ${migration_count} new migration(s) detected — run alembic upgrade head!${NC}"
    find "${PROJECT_DIR}/backend/alembic/versions" -name "*.py" -newer "${DEPLOY_STATE_FILE}" 2>/dev/null | while read f; do
      echo "    $(basename "$f")"
    done
    echo ""
  fi
fi
