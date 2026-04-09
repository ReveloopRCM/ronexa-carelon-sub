#!/bin/bash
set -euo pipefail

# ── Configuration ──
RESOURCE_GROUP="rg-ronexa-prod"
LOCATION="centralus"
ENVIRONMENT="ronexa-env"
REGISTRY="ronexaacr"
TAG="${TAG:-latest}"

echo "=== Ronexa Azure Deployment ==="
echo "Resource Group: $RESOURCE_GROUP"
echo "Location: $LOCATION"
echo "Registry: $REGISTRY.azurecr.io"
echo "Tag: $TAG"
echo ""

# ── Step 1: Create Resource Group ──
echo "→ Creating resource group..."
az group create \
  --name $RESOURCE_GROUP \
  --location $LOCATION \
  --output none

# ── Step 2: Create Container Registry ──
echo "→ Creating container registry..."
az acr create \
  --resource-group $RESOURCE_GROUP \
  --name $REGISTRY \
  --sku Basic \
  --admin-enabled true \
  --output none

ACR_LOGIN_SERVER=$(az acr show --name $REGISTRY --query loginServer -o tsv)
ACR_PASSWORD=$(az acr credential show --name $REGISTRY --query passwords[0].value -o tsv)

echo "→ Logging into ACR..."
az acr login --name $REGISTRY

# ── Step 3: Build and Push Images ──
echo "→ Building backend-api..."
docker build -t $ACR_LOGIN_SERVER/backend-api:$TAG -f backend/Dockerfile backend/
docker push $ACR_LOGIN_SERVER/backend-api:$TAG

echo "→ Building worker..."
docker build -t $ACR_LOGIN_SERVER/worker:$TAG -f backend/Dockerfile.worker backend/
docker push $ACR_LOGIN_SERVER/worker:$TAG

echo "→ Building auth-ops-frontend..."
docker build -t $ACR_LOGIN_SERVER/auth-ops-frontend:$TAG -f frontend/Dockerfile frontend/
docker push $ACR_LOGIN_SERVER/auth-ops-frontend:$TAG

echo "→ Building nginx-proxy..."
docker build -t $ACR_LOGIN_SERVER/nginx-proxy:$TAG -f infra/nginx/Dockerfile infra/nginx/
docker push $ACR_LOGIN_SERVER/nginx-proxy:$TAG

# ── Step 4: Create Container Apps Environment ──
echo "→ Creating Container Apps environment..."
az containerapp env create \
  --name $ENVIRONMENT \
  --resource-group $RESOURCE_GROUP \
  --location $LOCATION \
  --output none

# ── Step 5: Create PostgreSQL ──
echo "→ Creating PostgreSQL Flexible Server..."
PG_SERVER="ronexa-pg"
PG_PASSWORD=$(openssl rand -base64 24 | tr -dc 'a-zA-Z0-9' | head -c 24)

az postgres flexible-server create \
  --resource-group $RESOURCE_GROUP \
  --name $PG_SERVER \
  --location $LOCATION \
  --admin-user ronexa \
  --admin-password "$PG_PASSWORD" \
  --sku-name Standard_B1ms \
  --tier Burstable \
  --storage-size 32 \
  --version 16 \
  --public-access 0.0.0.0 \
  --output none

az postgres flexible-server db create \
  --resource-group $RESOURCE_GROUP \
  --server-name $PG_SERVER \
  --database-name ronexa \
  --output none

# Enable pgvector extension
az postgres flexible-server parameter set \
  --resource-group $RESOURCE_GROUP \
  --server-name $PG_SERVER \
  --name azure.extensions \
  --value vector \
  --output none

PG_HOST="${PG_SERVER}.postgres.database.azure.com"
DATABASE_URL="postgresql+asyncpg://ronexa:${PG_PASSWORD}@${PG_HOST}:5432/ronexa?ssl=require"

echo "→ PostgreSQL ready: $PG_HOST"

# ── Step 6: Create Redis ──
echo "→ Creating Azure Cache for Redis..."
REDIS_NAME="ronexa-redis"

az redis create \
  --resource-group $RESOURCE_GROUP \
  --name $REDIS_NAME \
  --location $LOCATION \
  --sku Basic \
  --vm-size C0 \
  --output none

REDIS_KEY=$(az redis list-keys --resource-group $RESOURCE_GROUP --name $REDIS_NAME --query primaryKey -o tsv)
REDIS_HOST="${REDIS_NAME}.redis.cache.windows.net"
REDIS_URL="rediss://:${REDIS_KEY}@${REDIS_HOST}:6380/0"

echo "→ Redis ready: $REDIS_HOST"

# ── Step 7: Deploy Container Apps ──

echo "→ Deploying backend-api..."
az containerapp create \
  --name backend-api \
  --resource-group $RESOURCE_GROUP \
  --environment $ENVIRONMENT \
  --image $ACR_LOGIN_SERVER/backend-api:$TAG \
  --registry-server $ACR_LOGIN_SERVER \
  --registry-username $REGISTRY \
  --registry-password "$ACR_PASSWORD" \
  --target-port 8000 \
  --ingress internal \
  --min-replicas 1 \
  --max-replicas 1 \
  --cpu 1 \
  --memory 2Gi \
  --env-vars \
    "DATABASE_URL=$DATABASE_URL" \
    "REDIS_URL=$REDIS_URL" \
    "RESTATE_URL=http://restate:8080" \
    "RESTATE_ADMIN_URL=http://restate:9070" \
    "ENVIRONMENT=production" \
  --output none

echo "→ Deploying restate..."
az containerapp create \
  --name restate \
  --resource-group $RESOURCE_GROUP \
  --environment $ENVIRONMENT \
  --image docker.io/restatedev/restate:1.1 \
  --target-port 8080 \
  --ingress internal \
  --min-replicas 1 \
  --max-replicas 1 \
  --cpu 1 \
  --memory 2Gi \
  --output none

echo "→ Deploying worker-1..."
az containerapp create \
  --name worker-1 \
  --resource-group $RESOURCE_GROUP \
  --environment $ENVIRONMENT \
  --image $ACR_LOGIN_SERVER/worker:$TAG \
  --registry-server $ACR_LOGIN_SERVER \
  --registry-username $REGISTRY \
  --registry-password "$ACR_PASSWORD" \
  --target-port 9080 \
  --ingress internal \
  --min-replicas 1 \
  --max-replicas 1 \
  --cpu 2 \
  --memory 4Gi \
  --env-vars \
    "DATABASE_URL=$DATABASE_URL" \
    "REDIS_URL=$REDIS_URL" \
    "RESTATE_URL=http://restate:8080" \
    "RESTATE_ADMIN_URL=http://restate:9070" \
    "WORKER_ID=worker-1" \
    "BROWSER_PROFILE_DIR=/data/browser-profiles" \
    "ENVIRONMENT=production" \
  --output none

echo "→ Deploying auth-ops-frontend..."
az containerapp create \
  --name auth-ops-frontend \
  --resource-group $RESOURCE_GROUP \
  --environment $ENVIRONMENT \
  --image $ACR_LOGIN_SERVER/auth-ops-frontend:$TAG \
  --registry-server $ACR_LOGIN_SERVER \
  --registry-username $REGISTRY \
  --registry-password "$ACR_PASSWORD" \
  --target-port 3000 \
  --ingress internal \
  --min-replicas 1 \
  --max-replicas 1 \
  --cpu 0.5 \
  --memory 1Gi \
  --env-vars \
    "API_URL=http://backend-api:8000" \
  --output none

echo "→ Deploying nginx-proxy (public-facing)..."
az containerapp create \
  --name nginx-proxy \
  --resource-group $RESOURCE_GROUP \
  --environment $ENVIRONMENT \
  --image $ACR_LOGIN_SERVER/nginx-proxy:$TAG \
  --registry-server $ACR_LOGIN_SERVER \
  --registry-username $REGISTRY \
  --registry-password "$ACR_PASSWORD" \
  --target-port 80 \
  --ingress external \
  --min-replicas 1 \
  --max-replicas 1 \
  --cpu 0.25 \
  --memory 0.5Gi \
  --output none

# ── Step 8: Get Public URL ──
FQDN=$(az containerapp show \
  --name nginx-proxy \
  --resource-group $RESOURCE_GROUP \
  --query properties.configuration.ingress.fqdn -o tsv)

echo ""
echo "=== Deployment Complete ==="
echo "Public URL: https://$FQDN"
echo ""
echo "Next steps:"
echo "  1. Set secrets (Carelon credentials, LLM keys) via Azure portal or:"
echo "     az containerapp secret set --name worker-1 --resource-group $RESOURCE_GROUP ..."
echo "  2. Register Restate worker: curl -X POST http://restate:9070/deployments -d '{\"uri\":\"http://worker-1:9080\"}'"
echo "  3. Test: curl https://$FQDN/api/health"
echo "  4. Navigate to: https://$FQDN/auth-ops/settings"
echo ""
echo "Credentials saved to: .deploy-creds (DO NOT COMMIT)"

cat > .deploy-creds << EOF
PG_HOST=$PG_HOST
PG_PASSWORD=$PG_PASSWORD
REDIS_HOST=$REDIS_HOST
REDIS_KEY=$REDIS_KEY
FQDN=$FQDN
EOF
