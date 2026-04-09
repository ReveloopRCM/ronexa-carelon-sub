# Ronexa VM Deployment Guide — Production

## Architecture

```
VM 1: Orchestrator (Standard D2s v3 — 2 CPU, 8GB, Ubuntu 22.04)
├── Docker Compose
│   ├── Restate server (ports 8080, 9070, 9071)
│   ├── Backend API (port 8000)
│   ├── Frontend (port 3000)
│   └── Nginx (port 80/443 — public)
├── PostgreSQL (Azure managed — already have)
├── Redis (Azure managed — already have)
└── Public IP + DNS

VM 2: Worker-A (Standard D4s v3 — 4 CPU, 16GB, Ubuntu 22.04 Desktop)
├── Playwright + Chrome (HEADED — visible browser)
├── Restate worker process (port 9080)
├── Carelon accounts: day shift + night shift
├── RDP via xrdp — ops monitors browser live
└── Registers with VM1's Restate

VM 3: Worker-B (same as VM 2 — add when scaling)
├── Different Carelon accounts
└── Same setup
```

## Step 1: Create VMs

### Via Azure CLI

```bash
# Variables
RG="rg-ronexa-prod"
LOCATION="centralus"
ADMIN_USER="ronexa"

# VM 1: Orchestrator
az vm create \
  --resource-group $RG \
  --name ronexa-orchestrator \
  --image Ubuntu2204 \
  --size Standard_D2s_v3 \
  --admin-username $ADMIN_USER \
  --generate-ssh-keys \
  --public-ip-sku Standard \
  --os-disk-size-gb 64 \
  --location $LOCATION

# VM 2: Worker-A (Desktop for RDP/browser visibility)
az vm create \
  --resource-group $RG \
  --name ronexa-worker-a \
  --image Ubuntu2204 \
  --size Standard_D4s_v3 \
  --admin-username $ADMIN_USER \
  --generate-ssh-keys \
  --public-ip-sku Standard \
  --os-disk-size-gb 128 \
  --location $LOCATION
```

### Open Ports

```bash
# Orchestrator: HTTP + SSH + Restate admin
az vm open-port --resource-group $RG --name ronexa-orchestrator --port 80 --priority 100
az vm open-port --resource-group $RG --name ronexa-orchestrator --port 443 --priority 101
az vm open-port --resource-group $RG --name ronexa-orchestrator --port 9070 --priority 102
az vm open-port --resource-group $RG --name ronexa-orchestrator --port 8080 --priority 103

# Worker-A: RDP + Restate worker
az vm open-port --resource-group $RG --name ronexa-worker-a --port 3389 --priority 100
az vm open-port --resource-group $RG --name ronexa-worker-a --port 9080 --priority 101
```

### Get Public IPs

```bash
az vm list-ip-addresses --resource-group $RG -o table
```

Save these IPs — you'll need them for Restate registration and DNS.

## Step 2: Setup Orchestrator VM

SSH into the orchestrator:

```bash
ORCH_IP=$(az vm list-ip-addresses --resource-group $RG --name ronexa-orchestrator --query "[0].virtualMachine.network.publicIpAddresses[0].ipAddress" -o tsv)
ssh ronexa@$ORCH_IP
```

### Install Docker + Docker Compose

```bash
# Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker

# Docker Compose (v2)
sudo apt-get install -y docker-compose-plugin

# Verify
docker --version
docker compose version
```

### Login to ACR

```bash
docker login ronexaacr.azurecr.io -u ronexaacr -p "{ACR_PASSWORD}"
```

### Create docker-compose.yml

```bash
mkdir -p ~/ronexa && cd ~/ronexa
cat > docker-compose.yml << 'COMPOSE'
version: "3.8"

services:
  restate:
    image: docker.restate.dev/restatedev/restate:1.3
    container_name: restate
    ports:
      - "8080:8080"   # Ingress (workflow invocations)
      - "9070:9070"   # UI
      - "9071:9071"   # Admin API
    volumes:
      - restate-data:/target
    environment:
      - RESTATE_OBSERVABILITY__LOG__FORMAT=Json
    restart: always

  backend-api:
    image: ronexaacr.azurecr.io/backend-api:v10
    container_name: backend-api
    ports:
      - "8000:8000"
    environment:
      - DATABASE_URL=${DATABASE_URL}
      - REDIS_URL=${REDIS_URL}
      - RESTATE_URL=http://restate:8080
      - RESTATE_ADMIN_URL=http://restate:9071
      - MONGO_URI=${MONGO_URI}
      - MONGO_DB=workflowdb
      - MONGO_COLLECTION=auth-submissions
      - AZURE_BLOB_CONNECTION_STRING=${AZURE_BLOB_CONN}
      - AZURE_BLOB_CONTAINER=carelon-attachments
      - AZURE_DOC_INTELLIGENCE_ENDPOINT=${DOC_INTEL_ENDPOINT}
      - AZURE_DOC_INTELLIGENCE_KEY=${DOC_INTEL_KEY}
      - GOOGLE_API_KEY=${GOOGLE_API_KEY}
      - ENVIRONMENT=production
    depends_on:
      - restate
    restart: always

  frontend:
    image: ronexaacr.azurecr.io/auth-ops-frontend:v8
    container_name: frontend
    ports:
      - "3000:3000"
    environment:
      - API_URL=http://backend-api:8000
    depends_on:
      - backend-api
    restart: always

  nginx:
    image: nginx:alpine
    container_name: nginx
    ports:
      - "80:80"
    volumes:
      - ./nginx.conf:/etc/nginx/conf.d/default.conf:ro
    depends_on:
      - frontend
      - backend-api
    restart: always

volumes:
  restate-data:
COMPOSE
```

### Create nginx.conf

```bash
cat > nginx.conf << 'NGINX'
server {
    listen 80;

    # Frontend
    location / {
        proxy_pass http://frontend:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # Backend API
    location /api/ {
        proxy_pass http://backend-api:8000/api/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_connect_timeout 120s;
        proxy_read_timeout 120s;
    }

    # Restate admin (for monitoring)
    location /restate/ {
        proxy_pass http://restate:9071/;
        proxy_set_header Host $host;
    }
}
NGINX
```

### Create .env file

```bash
cat > .env << 'ENV'
DATABASE_URL=postgresql+asyncpg://ronexa:mvXQqJOBvshxcCYOEYdv@ronexa-pg.postgres.database.azure.com:5432/ronexa?ssl=require
REDIS_URL=rediss://:vmxPepzOdvBB9YJkOoq7aSxpbjpusQ2FqAzCaAjAcik=@ronexa-redis.redis.cache.windows.net:6380/0
MONGO_URI=mongodb+srv://revadmin:doYRYD6DulhnNkAFRW33r66VWgET@reveloopcosdb-dev.global.mongocluster.cosmos.azure.com/?tls=true&authMechanism=SCRAM-SHA-256&retrywrites=false&maxIdleTimeMS=120000
AZURE_BLOB_CONN=DefaultEndpointsProtocol=https;AccountName=reveloopsapub;AccountKey=t78pMEQMWxyuBxcn5Bztk4QMcxkdeR7+Wbz7LIzNgvQ0ZU5arpugotZHsA8mzEg9BLEfi1HZr+wd+AStYkl6Ew==;EndpointSuffix=core.windows.net
DOC_INTEL_ENDPOINT=https://centralus.api.cognitive.microsoft.com/
DOC_INTEL_KEY=YOUR_DOC_INTEL_KEY
GOOGLE_API_KEY=YOUR_GEMINI_KEY
ENV
```

### Start everything

```bash
cd ~/ronexa
docker compose up -d

# Verify
docker compose ps
curl http://localhost/api/health
```

## Step 3: Setup Worker VM (with RDP + visible browser)

SSH into the worker:

```bash
WORKER_IP=$(az vm list-ip-addresses --resource-group $RG --name ronexa-worker-a --query "[0].virtualMachine.network.publicIpAddresses[0].ipAddress" -o tsv)
ssh ronexa@$WORKER_IP
```

### Install Desktop Environment + RDP

```bash
# Lightweight desktop (XFCE)
sudo apt-get update
sudo apt-get install -y xfce4 xfce4-goodies xrdp

# Configure xrdp to use XFCE
echo "xfce4-session" > ~/.xsession
sudo systemctl enable xrdp
sudo systemctl start xrdp

# Set password for RDP login
sudo passwd ronexa
# Enter a strong password — you'll use this for RDP
```

### Install Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker
docker login ronexaacr.azurecr.io -u ronexaacr -p "{ACR_PASSWORD}"
```

### Install Playwright Dependencies (for headed browser)

```bash
# Node.js (for Playwright)
curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -
sudo apt-get install -y nodejs

# Playwright browser + system deps
npx playwright install chromium
npx playwright install-deps chromium

# Additional display deps for headed mode
sudo apt-get install -y \
  libnss3 libatk-bridge2.0-0 libdrm2 libxcomposite1 \
  libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 \
  libcairo2 libasound2 libxshmfence1
```

### Install Python + Project Dependencies

```bash
sudo apt-get install -y python3 python3-pip python3-venv

# Clone or pull project code
mkdir -p ~/ronexa && cd ~/ronexa

# Option A: Pull Docker image and run worker in Docker (no browser visibility)
# Option B: Run worker natively for headed browser (RECOMMENDED)

# Pull code from your repo or SCP from local
# scp -r /Users/andrewntuyo/Desktop/ronexa-sub/backend ronexa@$WORKER_IP:~/ronexa/

# Install deps
cd ~/ronexa/backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Install Playwright for Python
pip install playwright
playwright install chromium
```

### Create Worker .env

```bash
cat > ~/ronexa/backend/.env << 'ENV'
DATABASE_URL=postgresql+asyncpg://ronexa:mvXQqJOBvshxcCYOEYdv@ronexa-pg.postgres.database.azure.com:5432/ronexa?ssl=require
REDIS_URL=rediss://:vmxPepzOdvBB9YJkOoq7aSxpbjpusQ2FqAzCaAjAcik=@ronexa-redis.redis.cache.windows.net:6380/0
RESTATE_URL=http://ORCHESTRATOR_IP:8080
RESTATE_ADMIN_URL=http://ORCHESTRATOR_IP:9071

# Carelon credentials
CARELON_USERNAME=antuyo@revelooptechsystems.com
CARELON_PASSWORD=42Revelop2use!
CARELON_BASE_URL=https://www.providerportal.com

# MFA
GRAPH_TENANT_ID=23bb5c1c-4989-4e70-b6d3-d43371ad4285
GRAPH_CLIENT_ID=5b62c9b8-058e-47d0-b6f4-348d554c39cf
GRAPH_CLIENT_SECRET=gZh8Q~~il.MyVBp.8wGeSTC0TvbWcTOc7Npu8b6G
GRAPH_MAILBOX=carelon-mfa@revelooptechsystems.com

# Google Gemini
GOOGLE_API_KEY=YOUR_GEMINI_KEY

# Worker config
WORKER_ID=worker-a
ENVIRONMENT=production
HEADLESS=false
BROWSER_PROFILES_DIR=./profiles
ENV
```

**Replace `ORCHESTRATOR_IP` with VM 1's private IP** (use `az vm list-ip-addresses` to find it, or use the private IP from the VNet).

### Start Worker (headed — visible in RDP)

```bash
cd ~/ronexa/backend
source .venv/bin/activate

# Must run inside a desktop session (RDP or VNC)
# The DISPLAY variable must be set for headed browser
export DISPLAY=:10

# Start the Restate worker
python3 restate_worker.py
```

**Important:** The worker must run inside an RDP session (or with a virtual display) for the browser to be visible. Start it from the XFCE desktop terminal after RDPing in.

### Auto-start Worker on Boot (systemd)

```bash
sudo cat > /etc/systemd/system/ronexa-worker.service << 'SERVICE'
[Unit]
Description=Ronexa Portal Worker
After=network.target

[Service]
Type=simple
User=ronexa
WorkingDirectory=/home/ronexa/ronexa/backend
Environment=DISPLAY=:10
EnvironmentFile=/home/ronexa/ronexa/backend/.env
ExecStart=/home/ronexa/ronexa/backend/.venv/bin/python3 restate_worker.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SERVICE

sudo systemctl daemon-reload
sudo systemctl enable ronexa-worker
sudo systemctl start ronexa-worker
```

## Step 4: Register Worker with Restate

From your local machine (or the orchestrator):

```bash
# Replace ORCHESTRATOR_IP with VM 1's public IP
curl -X POST http://ORCHESTRATOR_IP:9071/deployments \
  -H "Content-Type: application/json" \
  -d '{"uri": "http://WORKER_PRIVATE_IP:9080"}'
```

Use the **private IP** of the worker VM (they're in the same VNet). Check private IPs:

```bash
az vm list-ip-addresses --resource-group rg-ronexa-prod -o table
```

## Step 5: Verify

### Test from browser
- Dashboard: `http://ORCHESTRATOR_IP/`
- Settings: `http://ORCHESTRATOR_IP/settings`
- Health: `http://ORCHESTRATOR_IP/api/health`
- Restate admin: `http://ORCHESTRATOR_IP:9070`

### Test Restate
```bash
# Check deployments registered
curl http://ORCHESTRATOR_IP:9071/deployments

# Should show worker with PriorAuth, BrowserSession, UserWorker, ShiftManager
```

### RDP into Worker
1. Open Remote Desktop (Windows) or Microsoft Remote Desktop (Mac)
2. Connect to `WORKER_PUBLIC_IP:3389`
3. Login: `ronexa` / your password
4. Open terminal → you should see the worker process running
5. When a case is processed, Chrome opens and navigates Carelon portal — you watch it live

### Process a test case
1. Go to Dashboard → Cases → pick a case with NOTES_UPLOADED state
2. Click "Process Case"
3. RDP into worker → watch Chrome navigate the portal
4. Worker logs in terminal show each step

## Step 6: DNS + SSL (production)

```bash
# Point your domain to orchestrator IP
# ronexa.yourdomain.com → ORCHESTRATOR_IP

# Install Certbot for free SSL
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d ronexa.yourdomain.com
```

## Scaling

### Add Worker-B

1. Create another VM (same spec as Worker-A)
2. Same setup: Docker, Playwright, XFCE, xrdp
3. Different .env: `WORKER_ID=worker-b`, different Carelon accounts
4. Register with Restate: `curl -X POST http://ORCHESTRATOR_IP:9071/deployments -d '{"uri":"http://WORKER_B_PRIVATE_IP:9080"}'`

Each worker processes ~15 cases/hour = 360/day. Two workers = 720/day. Three workers = 1080/day.

## Cost Summary

| Resource | Spec | Monthly |
|----------|------|---------|
| VM 1: Orchestrator | D2s v3 (2 CPU, 8GB) | ~$70 |
| VM 2: Worker-A | D4s v3 (4 CPU, 16GB) | ~$140 |
| VM 3: Worker-B | D4s v3 (add later) | ~$140 |
| PostgreSQL | B1ms (managed) | ~$13 |
| Redis | Managed | ~$16 |
| ACR | Basic | ~$5 |
| Doc Intelligence | S0 | ~$2 |
| **Total (2 workers)** | | **~$246/mo** |
| **Total (3 workers)** | | **~$386/mo** |

## Monitoring

- **Restate admin UI**: `http://ORCHESTRATOR_IP:9070` — see all invocations, retries, state
- **Worker browser**: RDP into worker VMs — watch automation live
- **Backend logs**: `docker compose logs -f backend-api` on orchestrator
- **Worker logs**: `journalctl -u ronexa-worker -f` on worker VMs
- **Dashboard**: `http://ORCHESTRATOR_IP/` — queue depths, case states, sync history
